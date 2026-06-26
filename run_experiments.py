"""
Leakage-safe 8-experiment comparison:
  T1 = realized-friction penalty inside the CEM objective
       (this is NOT a live entry gate because this non-RF runner has no
        point-in-time expected-return estimate);
  T2 = rolling, train-only CEM evaluation windows;
  T3 = half-Kelly position sizing from realised, fully net historical trades.

Key corrections relative to the original runner:
  * CEM never reads the test split.
  * Validation candidates are evaluated after training and before the test
    boundary. They can be used to choose an experiment family; test remains the
    untouched final holdout.
  * T2 windows are built only from the train split.
  * Every CEM window is valuated at its own end date; it cannot use prices
    after that window.
  * Finite-horizon simulations truncate price/probability paths before trade
    generation, so entry/exit decisions cannot inspect later observations.
  * OOS metrics come from a separate, frozen-policy portfolio simulation on
    test candidates only, with one fixed OOS end date across all ablations.
    They are actual portfolio returns, not summed trade P&L.
  * Trade P&L is fully net of all modeled rotation costs:
      benchmark sell + asset buy + asset sell + benchmark rebuy.
  * The CEM objective uses daily portfolio-equity Sharpe, not a small sample
    of trade returns.
  * Each experiment starts CEM from the same benchmark-specific random seed,
    so an ablation is not confounded by a different initial population.
  * Kelly uses fully net realised returns and reports actual realised sizing.

Usage:
    python -u run_experiments.py

This file deliberately writes new output names so the old contaminated result
CSV is preserved:
    data/experiment_results_clean.csv
    data/experiment_trade_logs_clean/
    data/experiment_equity_logs_clean/
"""
from __future__ import annotations

import asyncio
import csv
import json
import math
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from pipeline.strategy import DEFAULT_POLICY, simulate_one


PROJECT = Path(__file__).resolve().parent
REL_COL = "feat_connection_strength"

RESULTS_CSV = PROJECT / "data" / "experiment_results_clean.csv"
TRADE_LOG_DIR = PROJECT / "data" / "experiment_trade_logs_clean"
EQUITY_LOG_DIR = PROJECT / "data" / "experiment_equity_logs_clean"

INITIAL_CAPITAL = 100_000.0

# ── Portfolio parameter space ────────────────────────────────────────────────

PORTFOLIO_BOUNDS = dict(
    atr_mult=(1.5, 4.0),
    lock_activate=(0.02, 0.10),
    theta_out=(0.45, 0.60),
    enter_strong=(0.60, 0.85),
    enter_floor=(0.55, 0.80),
    hold_days=(1, 5),
    max_prob_surge=(0.20, 0.80),
    max_price_runup=(0.02, 0.20),
    position_size_pct=(0.03, 0.20),
    max_concurrent=(3, 15),
)
PORT_DEFAULT = {**DEFAULT_POLICY, "position_size_pct": 0.10, "max_concurrent": 10}

# ── Experiment controls ──────────────────────────────────────────────────────

HURDLE_MULT = 3.0
HURDLE_PENALTY = 2.0
# T1 is a realised friction penalty used only in the CEM fitness. A true,
# live pre-entry hurdle needs a point-in-time expected-return estimate.

WF_EVAL_MON = 3
WF_STEP_MON = 3
WF_BUFFER_DAYS = 7
WF_MIN_CANDS = 8

# The train simulation is marked to market / liquidated before the test split.
# The buffer lets the longest policy hold (currently bounded to 5 days) settle
# without reading any test-period price or probability observation.
TRAIN_SETTLEMENT_BUFFER_DAYS = 7

KELLY_MIN_N = 10
KELLY_LOOKBACK_N = 30
KELLY_MIN_SZ = 0.03
KELLY_MAX_SZ = 0.15

CEM_ITERS = 6
CEM_POP = 20
CEM_ELITE_FRAC = 0.25
CEM_BASE_SEED = 42
# Every ablation gets the identical initial CEM population for a given benchmark.
# SPY and QQQ have separate but fixed populations.
BENCHMARK_SEED_OFFSET = {"SPY": 0, "QQQ": 10_000}

MIN_TRADES_FOR_REWARD = 3
MIN_DAILY_RETURNS_FOR_SHARPE = 20
DD_PENALTY = 0.30
INVALID_SCORE = -1e9
EVAL_TRADE_WARNING_N = 80

EXPERIMENTS = [
    {"id": 0, "label": "Baseline", "hurdle": False, "wf": False, "kelly": False},
    {"id": 1, "label": "T1 FrictionPenalty", "hurdle": True, "wf": False, "kelly": False},
    {"id": 2, "label": "T2 TrainWindows", "hurdle": False, "wf": True, "kelly": False},
    {"id": 3, "label": "T3 Kelly", "hurdle": False, "wf": False, "kelly": True},
    {"id": 4, "label": "T1+T2", "hurdle": True, "wf": True, "kelly": False},
    {"id": 5, "label": "T1+T3", "hurdle": True, "wf": False, "kelly": True},
    {"id": 6, "label": "T2+T3", "hurdle": False, "wf": True, "kelly": True},
    {"id": 7, "label": "T1+T2+T3", "hurdle": True, "wf": True, "kelly": True},
]


# ── Time / price helpers ─────────────────────────────────────────────────────

_CLOSE_CACHE: dict[tuple[int, str], tuple[pd.DatetimeIndex, np.ndarray]] = {}
_PATH_CUTOFF_CACHE: dict[
    tuple[int, int, str],
    tuple[dict[str, list[tuple]], dict[str, list[tuple]]],
] = {}


def as_utc_day(value: Any) -> pd.Timestamp:
    """Convert a date-like value to a normalized UTC Timestamp."""
    ts = pd.Timestamp(value)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.normalize()


def ib_cost(shares: int, price: float, is_sell: bool) -> float:
    """IB-style commission + SEC fee on sales + fixed 5 bp slippage."""
    if shares <= 0 or price <= 0:
        return 0.0
    trade_value = shares * price
    commission = max(0.35, min(shares * 0.0035, trade_value * 0.01))
    sec = trade_value * 0.0000278 if is_sell else 0.0
    return commission + sec + trade_value * 0.0005


def _close_on(prices: dict, symbol: str, date: Any) -> float | None:
    """Latest known daily close on or before date, cached for CEM speed."""
    key = (id(prices), symbol)
    cached = _CLOSE_CACHE.get(key)
    if cached is None:
        bars = prices.get(symbol, [])
        if not bars:
            return None
        idx = pd.DatetimeIndex([as_utc_day(t) for t, *_ in bars])
        values = np.asarray([float(close) for *_rest, close in bars], dtype=float)
        cached = (idx, values)
        _CLOSE_CACHE[key] = cached

    idx, values = cached
    loc = idx.searchsorted(as_utc_day(date), side="right") - 1
    if loc < 0:
        return None
    return float(values[loc])


def _calendar_dates(prices: dict, bench_sym: str, start_date: Any, end_date: Any) -> list[pd.Timestamp]:
    start = as_utc_day(start_date)
    end = as_utc_day(end_date)
    dates = sorted({as_utc_day(t) for t, *_ in prices.get(bench_sym, [])})
    return [d for d in dates if start <= d <= end]


def truncate_paths(prices: dict, probs: dict, end_date: Any | None) -> tuple[dict, dict]:
    """Return price/probability paths clipped to an evaluation horizon."""
    if end_date is None:
        return prices, probs

    cutoff = as_utc_day(end_date)
    key = (id(prices), id(probs), str(cutoff.date()))
    cached = _PATH_CUTOFF_CACHE.get(key)
    if cached is not None:
        return cached

    truncated_prices = {
        symbol: [bar for bar in bars if as_utc_day(bar[0]) <= cutoff]
        for symbol, bars in prices.items()
    }
    truncated_probs = {
        market_id: [point for point in points if as_utc_day(point[0]) <= cutoff]
        for market_id, points in probs.items()
    }
    cached = (truncated_prices, truncated_probs)
    _PATH_CUTOFF_CACHE[key] = cached
    return cached


def _affordable_buy_qty(cash_available: float, price: float) -> int:
    """Largest integer share count whose buy price plus modeled buy cost fits cash."""
    if cash_available <= 0 or price <= 0:
        return 0
    qty = int(cash_available / price)
    while qty > 0 and qty * price + ib_cost(qty, price, False) > cash_available + 1e-9:
        qty -= 1
    return qty


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


# ── Policy / Kelly helpers ───────────────────────────────────────────────────

def port_policy_from_vec(vec: np.ndarray) -> dict[str, float | int]:
    names = list(PORTFOLIO_BOUNDS.keys())
    policy: dict[str, float | int] = {}
    for i, name in enumerate(names):
        lo, hi = PORTFOLIO_BOUNDS[name]
        policy[name] = float(np.clip(vec[i], lo, hi))

    policy["hold_days"] = int(round(float(policy["hold_days"])))
    policy["max_concurrent"] = int(round(float(policy["max_concurrent"])))
    if float(policy["enter_strong"]) < float(policy["enter_floor"]):
        policy["enter_strong"] = policy["enter_floor"]
    return policy


def kelly_size(completed_history: list[dict], base: float) -> float:
    """Half-Kelly from the latest fully net realised trades."""
    if len(completed_history) < KELLY_MIN_N:
        return base

    recent = completed_history[-KELLY_LOOKBACK_N:]
    wins = [float(t["pnl_pct"]) for t in recent if float(t.get("pnl_pct", 0.0)) > 0]
    losses = [float(t["pnl_pct"]) for t in recent if float(t.get("pnl_pct", 0.0)) <= 0]

    if not wins or not losses:
        return base

    win_probability = len(wins) / len(recent)
    payoff_ratio = abs(float(np.mean(wins))) / abs(float(np.mean(losses)))
    if payoff_ratio <= 0 or not np.isfinite(payoff_ratio):
        return base

    full_kelly = (win_probability * payoff_ratio - (1.0 - win_probability)) / payoff_ratio
    half_kelly = max(0.0, full_kelly / 2.0)
    return float(np.clip(half_kelly, KELLY_MIN_SZ, KELLY_MAX_SZ))


# ── Train-only rolling evaluation windows ────────────────────────────────────

def _frame_bounds(df: pd.DataFrame) -> tuple[pd.Timestamp, pd.Timestamp]:
    ts = pd.to_datetime(df["t_theta"], utc=True)
    return as_utc_day(ts.min()), as_utc_day(ts.max())


def create_wf_windows(train_df: pd.DataFrame, train_eval_end: pd.Timestamp) -> list[dict[str, Any]]:
    """
    Build rolling evaluation windows using TRAIN rows only.

    Candidates near a window boundary are excluded so a position has a short
    settlement interval before the portfolio is valuated at the window end.
    """
    if train_df.empty:
        return []

    ts = pd.to_datetime(train_df["t_theta"], utc=True)
    start = as_utc_day(ts.min())
    last_candidate_day = as_utc_day(ts.max())
    end = min(as_utc_day(train_eval_end), last_candidate_day)

    windows: list[dict[str, Any]] = []
    cur = start
    buffer = pd.Timedelta(days=WF_BUFFER_DAYS)

    while cur + pd.DateOffset(months=WF_EVAL_MON) <= end + pd.Timedelta(days=15):
        window_end = min(
            as_utc_day(cur + pd.DateOffset(months=WF_EVAL_MON)),
            as_utc_day(train_eval_end),
        )
        mask = (ts >= cur + buffer) & (ts < window_end - buffer)
        frame = train_df.loc[mask].copy()
        if len(frame) >= WF_MIN_CANDS:
            windows.append({"df": frame, "start": cur, "end": window_end})
        cur = as_utc_day(cur + pd.DateOffset(months=WF_STEP_MON))

    if len(windows) >= 2:
        return windows

    train_start, _ = _frame_bounds(train_df)
    return [{"df": train_df.copy(), "start": train_start, "end": as_utc_day(train_eval_end)}]


# ── Core portfolio simulator ─────────────────────────────────────────────────

def sim_opp_cost(
    df: pd.DataFrame,
    prices: dict,
    probs: dict,
    policy: dict,
    *,
    bench_sym: str = "SPY",
    initial: float = INITIAL_CAPITAL,
    use_kelly: bool = False,
    start_date: Any | None = None,
    end_date: Any | None = None,
    initial_kelly_history: list[dict] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any], dict]:
    """
    Simulate a benchmark-rotation portfolio.

    When end_date is supplied, open positions are liquidated at that date's
    available daily close. This is essential for train windows: no score can
    depend on price/probability data after its own evaluation horizon.

    The returned per-trade `pnl` is fully net of all modeled costs associated
    with rotating benchmark capital into and out of the asset.
    """
    base_ps = float(policy.get("position_size_pct", 0.10))
    max_concurrent = int(policy.get("max_concurrent", 10))

    empty_stats: dict[str, Any] = {
        "initial": initial,
        "final": initial,
        "total_return": 0.0,
        "benchmark_return": 0.0,
        "excess_return": 0.0,
        "max_dd": 0.0,
        "n_trades": 0,
        "win_rate": 0.0,
        "avg_pnl": 0.0,
        "avg_gross_pnl": 0.0,
        "gross_trade_pnl": 0.0,
        "net_trade_pnl": 0.0,
        "total_txn_cost": 0.0,
        "trade_txn_cost": 0.0,
        "friction_fail_rate": 0.0,
        "avg_position_size": 0.0,
        "median_position_size": 0.0,
        "min_position_size": 0.0,
        "max_position_size": 0.0,
        "start_date": None,
        "end_date": None,
        "n_equity_days": 0,
    }
    if df.empty:
        return pd.DataFrame(), pd.DataFrame(), empty_stats, policy

    sim_prices, sim_probs = truncate_paths(prices, probs, end_date)

    all_trades: list[dict] = []
    for _, row in df.sort_values("t_theta").iterrows():
        trade = simulate_one(row, sim_prices, sim_probs, policy)
        if trade is None:
            continue
        trade = dict(trade)
        trade["_entry_ts"] = as_utc_day(trade["entry_date"])
        trade["_exit_ts"] = as_utc_day(trade["exit_date"])
        candidate_theta = as_utc_day(row["t_theta"])
        if trade["_entry_ts"] < candidate_theta:
            raise ValueError(
                f"{trade['symbol']} entered on {trade['_entry_ts'].date()} before "
                f"candidate t_theta {candidate_theta.date()}."
            )
        trade["candidate_t_theta"] = str(candidate_theta.date())
        trade["candidate_t_e"] = str(as_utc_day(row["t_e"]).date())
        all_trades.append(trade)
    all_trades.sort(key=lambda trade: trade["_entry_ts"])

    candidate_start, candidate_end = _frame_bounds(df)
    eval_start = as_utc_day(start_date) if start_date is not None else (
        min((trade["_entry_ts"] for trade in all_trades), default=candidate_start)
    )
    if end_date is not None:
        eval_end = as_utc_day(end_date)
    else:
        eval_end = max((trade["_exit_ts"] for trade in all_trades), default=candidate_end)

    if end_date is not None:
        future_exits = [trade for trade in all_trades if trade["_exit_ts"] > eval_end]
        if future_exits:
            raise ValueError(
                f"{len(future_exits)} generated trades exit after evaluation end "
                f"{eval_end.date()}."
            )

    bench_bars = sim_prices.get(bench_sym, [])
    if not bench_bars:
        raise ValueError(f"No daily bars available for benchmark {bench_sym}.")

    calendar = _calendar_dates(sim_prices, bench_sym, eval_start, eval_end)
    if not calendar:
        raise ValueError(
            f"No {bench_sym} daily bars overlap requested evaluation range "
            f"{eval_start.date()} through {eval_end.date()}."
        )

    first_day = calendar[0]
    last_day = calendar[-1]
    first_bench_close = _close_on(sim_prices, bench_sym, first_day)
    last_bench_close = _close_on(sim_prices, bench_sym, last_day)
    if first_bench_close is None or last_bench_close is None:
        raise ValueError(f"Unable to price {bench_sym} across evaluation dates.")

    # Establish the portfolio and the passive benchmark using exactly the same
    # initial execution model.
    initial_bench_shares = _affordable_buy_qty(initial, first_bench_close)
    initial_cost = ib_cost(initial_bench_shares, first_bench_close, False)
    initial_cash = initial - initial_bench_shares * first_bench_close - initial_cost

    bench_shares = initial_bench_shares
    cash = initial_cash
    total_txn_cost = initial_cost

    open_positions: list[dict] = []
    completed: list[dict] = []
    kelly_history = [dict(item) for item in (initial_kelly_history or [])]
    equity_rows: list[dict[str, Any]] = []
    trade_idx = 0

    def close_position(pos: dict, close_day: pd.Timestamp, exit_price: float, exit_reason: str) -> None:
        """Close an asset position, rotate proceeds back to benchmark, and record net P&L."""
        nonlocal cash, bench_shares, total_txn_cost

        qty = int(pos["_qty"])
        entry_price = float(pos["entry_price"])
        asset_sell_cost = ib_cost(qty, exit_price, True)
        sale_proceeds = qty * exit_price - asset_sell_cost

        rebuy_qty = _affordable_buy_qty(sale_proceeds, float(_close_on(sim_prices, bench_sym, close_day) or 0.0))
        bench_close = float(_close_on(sim_prices, bench_sym, close_day) or 0.0)
        rebuy_cost = ib_cost(rebuy_qty, bench_close, False)

        cash += sale_proceeds - rebuy_qty * bench_close - rebuy_cost
        bench_shares += rebuy_qty
        total_txn_cost += asset_sell_cost + rebuy_cost

        direct_cost = (
            float(pos["_benchmark_sell_cost"])
            + float(pos["_asset_buy_cost"])
            + asset_sell_cost
            + rebuy_cost
        )
        gross_pnl = qty * (exit_price - entry_price)
        net_pnl = gross_pnl - direct_cost
        exposure = max(float(pos["_asset_entry_notional"]), 1e-12)

        pos["exit_price"] = round(float(exit_price), 6)
        pos["exit_date"] = str(close_day.date())
        pos["realized_exit_reason"] = exit_reason
        pos["gross_pnl"] = round(gross_pnl, 2)
        pos["pnl"] = round(net_pnl, 2)
        pos["pnl_pct"] = round(net_pnl / exposure * 100.0, 4)
        pos["txn_cost"] = round(direct_cost, 2)
        pos["exit_value"] = round(qty * exit_price, 2)
        pos["benchmark_rebuy_qty"] = rebuy_qty

        completed.append(pos)
        kelly_history.append(pos)

    for day in calendar:
        bench_close = _close_on(sim_prices, bench_sym, day)
        if bench_close is None:
            continue

        # Close trades whose planned exit is known by the current day.
        still_open: list[dict] = []
        for pos in open_positions:
            if pos["_exit_ts"] <= day:
                exit_reason = str(pos.get("exit_reason", "strategy_exit"))
                if end_date is not None and exit_reason == "end_of_window":
                    exit_reason = "evaluation_end_liquidation"
                close_position(
                    pos,
                    close_day=day,
                    exit_price=float(pos["exit_price"]),
                    exit_reason=exit_reason,
                )
            else:
                still_open.append(pos)
        open_positions = still_open

        # Open candidates exactly on their configured entry day.
        while trade_idx < len(all_trades):
            trade = all_trades[trade_idx]
            if trade["_entry_ts"] > day:
                break
            trade_idx += 1

            if trade["_entry_ts"] < day:
                continue
            if len(open_positions) >= max_concurrent:
                continue
            if any(pos["symbol"] == trade["symbol"] for pos in open_positions):
                continue

            position_size = kelly_size(kelly_history, base_ps) if use_kelly else base_ps
            marked_open_value = sum(
                int(pos["_qty"]) * float(_close_on(sim_prices, pos["symbol"], day) or pos["entry_price"])
                for pos in open_positions
            )
            current_equity = bench_shares * bench_close + marked_open_value + cash
            desired_allocation = current_equity * position_size
            entry_price = float(trade["entry_price"])
            if entry_price <= 0 or desired_allocation < entry_price:
                continue

            benchmark_sell_qty = min(int(desired_allocation / bench_close), bench_shares)
            if benchmark_sell_qty < 1:
                continue

            benchmark_sell_cost = ib_cost(benchmark_sell_qty, bench_close, True)
            available_for_asset = benchmark_sell_qty * bench_close - benchmark_sell_cost
            asset_qty = _affordable_buy_qty(available_for_asset, entry_price)
            if asset_qty < 1:
                continue

            asset_buy_cost = ib_cost(asset_qty, entry_price, False)
            asset_cash_needed = asset_qty * entry_price + asset_buy_cost
            if asset_cash_needed > available_for_asset + 1e-9:
                # Defensive guard; _affordable_buy_qty should make this unreachable.
                continue

            bench_shares -= benchmark_sell_qty
            cash += available_for_asset - asset_cash_needed
            total_txn_cost += benchmark_sell_cost + asset_buy_cost

            open_positions.append(
                {
                    **trade,
                    "_qty": asset_qty,
                    "_position_size_pct": position_size,
                    "_asset_entry_notional": round(asset_qty * entry_price, 2),
                    "_benchmark_sell_cost": round(benchmark_sell_cost, 2),
                    "_asset_buy_cost": round(asset_buy_cost, 2),
                    "_entry_ts": trade["_entry_ts"],
                    "_exit_ts": trade["_exit_ts"],
                }
            )

        open_value = sum(
            int(pos["_qty"]) * float(_close_on(sim_prices, pos["symbol"], day) or pos["entry_price"])
            for pos in open_positions
        )
        equity = bench_shares * bench_close + open_value + cash
        passive_benchmark_equity = initial_bench_shares * bench_close + initial_cash
        equity_rows.append(
            {
                "date": str(day.date()),
                "equity": round(equity, 2),
                "benchmark_equity": round(passive_benchmark_equity, 2),
                "cash": round(cash, 2),
                "benchmark_shares": bench_shares,
                "open_positions": len(open_positions),
            }
        )

    # If a finite evaluation horizon was supplied, mark positions to market and
    # liquidate on that horizon. This prevents CEM train windows from using
    # observations after their end date.
    if open_positions:
        for pos in list(open_positions):
            forced_price = _close_on(sim_prices, pos["symbol"], last_day)
            if forced_price is None:
                # This should not happen when the candidate universe and price
                # loader are consistent. Falling back to entry price is safer
                # than inventing a future close.
                forced_price = float(pos["entry_price"])
            close_position(
                pos,
                close_day=last_day,
                exit_price=float(forced_price),
                exit_reason="evaluation_end_liquidation",
            )
        open_positions = []

    final_equity = bench_shares * last_bench_close + cash
    final_passive_equity = initial_bench_shares * last_bench_close + initial_cash

    # The final forced liquidation costs occur after the final intraday mark.
    # Update the final row so drawdown and reported final equity agree.
    if equity_rows:
        equity_rows[-1]["equity"] = round(final_equity, 2)
        equity_rows[-1]["benchmark_equity"] = round(final_passive_equity, 2)
        equity_rows[-1]["cash"] = round(cash, 2)
        equity_rows[-1]["benchmark_shares"] = bench_shares
        equity_rows[-1]["open_positions"] = 0
    else:
        equity_rows.append(
            {
                "date": str(last_day.date()),
                "equity": round(final_equity, 2),
                "benchmark_equity": round(final_passive_equity, 2),
                "cash": round(cash, 2),
                "benchmark_shares": bench_shares,
                "open_positions": 0,
            }
        )

    equity_df = pd.DataFrame(equity_rows)
    trade_df = pd.DataFrame(completed)

    equity_values = equity_df["equity"].astype(float).to_numpy()
    peaks = np.maximum.accumulate(equity_values)
    drawdowns = np.where(peaks > 0, equity_values / peaks - 1.0, 0.0)
    max_dd = float(np.min(drawdowns) * 100.0) if len(drawdowns) else 0.0

    if trade_df.empty:
        gross_trade_pnl = net_trade_pnl = trade_txn_cost = 0.0
        win_rate = avg_pnl = avg_gross_pnl = friction_fail_rate = 0.0
        position_sizes = np.asarray([], dtype=float)
    else:
        gross_trade_pnl = float(trade_df["gross_pnl"].sum())
        net_trade_pnl = float(trade_df["pnl"].sum())
        trade_txn_cost = float(trade_df["txn_cost"].sum())
        win_rate = float((trade_df["pnl"] > 0).mean() * 100.0)
        avg_pnl = float(trade_df["pnl"].mean())
        avg_gross_pnl = float(trade_df["gross_pnl"].mean())
        friction_fail_rate = float(
            (trade_df["gross_pnl"] < HURDLE_MULT * trade_df["txn_cost"]).mean() * 100.0
        )
        position_sizes = trade_df["_position_size_pct"].astype(float).to_numpy()

    stats = {
        "initial": round(initial, 2),
        "final": round(final_equity, 2),
        "total_return": round((final_equity / initial - 1.0) * 100.0, 4),
        "benchmark_return": round((final_passive_equity / initial - 1.0) * 100.0, 4),
        "excess_return": round((final_equity - final_passive_equity) / initial * 100.0, 4),
        "max_dd": round(max_dd, 4),
        "n_trades": int(len(trade_df)),
        "win_rate": round(win_rate, 4),
        "avg_pnl": round(avg_pnl, 4),
        "avg_gross_pnl": round(avg_gross_pnl, 4),
        "gross_trade_pnl": round(gross_trade_pnl, 2),
        "net_trade_pnl": round(net_trade_pnl, 2),
        "total_txn_cost": round(total_txn_cost, 2),
        "trade_txn_cost": round(trade_txn_cost, 2),
        "friction_fail_rate": round(friction_fail_rate, 4),
        "avg_position_size": round(float(position_sizes.mean() * 100.0), 4) if len(position_sizes) else 0.0,
        "median_position_size": round(float(np.median(position_sizes) * 100.0), 4) if len(position_sizes) else 0.0,
        "min_position_size": round(float(position_sizes.min() * 100.0), 4) if len(position_sizes) else 0.0,
        "max_position_size": round(float(position_sizes.max() * 100.0), 4) if len(position_sizes) else 0.0,
        "start_date": str(first_day.date()),
        "end_date": str(last_day.date()),
        "n_equity_days": int(len(equity_df)),
    }
    return trade_df, equity_df, stats, policy


# ── CEM objective ────────────────────────────────────────────────────────────

def daily_equity_sharpe(equity_df: pd.DataFrame) -> float | None:
    """Annualized Sharpe from portfolio daily equity returns."""
    if equity_df.empty or len(equity_df) < MIN_DAILY_RETURNS_FOR_SHARPE + 1:
        return None

    daily_returns = equity_df["equity"].astype(float).pct_change().dropna()
    if len(daily_returns) < MIN_DAILY_RETURNS_FOR_SHARPE:
        return None

    std = float(daily_returns.std(ddof=1))
    if not np.isfinite(std) or std <= 1e-12:
        return 0.0

    sharpe = float(daily_returns.mean() / std * math.sqrt(252.0))
    return sharpe if np.isfinite(sharpe) else None


def cem_reward(trades: pd.DataFrame, equity_df: pd.DataFrame, stats: dict, use_hurdle: bool) -> float:
    """Cost-aware daily-equity objective for CEM."""
    if trades.empty or stats["n_trades"] < MIN_TRADES_FOR_REWARD:
        return INVALID_SCORE

    sharpe = daily_equity_sharpe(equity_df)
    if sharpe is None:
        return INVALID_SCORE

    score = sharpe - DD_PENALTY * abs(float(stats["max_dd"]))

    if use_hurdle:
        # Fully realised cost-aware penalty. This deliberately does NOT claim
        # to be a live entry gate; it selects policies whose realised trades
        # more often clear the friction multiple.
        failed = (trades["gross_pnl"] < HURDLE_MULT * trades["txn_cost"]).mean()
        score -= float(failed) * HURDLE_PENALTY

    return float(score)


def cem_search(
    train_df: pd.DataFrame,
    prices: dict,
    probs: dict,
    *,
    bench_sym: str,
    use_hurdle: bool,
    use_wf: bool,
    use_kelly: bool,
    train_eval_end: pd.Timestamp,
    n_iter: int = CEM_ITERS,
    pop: int = CEM_POP,
    seed: int = CEM_BASE_SEED,
) -> tuple[dict, float, list[dict[str, Any]]]:
    """
    Tune one frozen policy using train data only.

    T2 uses several train-only rolling windows. Every window ends at its own
    date, so both its reward and its equity path remain isolated from test.
    """
    if train_df.empty:
        raise ValueError("CEM received an empty train frame.")

    rng = np.random.default_rng(seed)
    names = list(PORTFOLIO_BOUNDS.keys())
    dim = len(names)
    elite_count = max(2, int(pop * CEM_ELITE_FRAC))
    mean = np.array([PORT_DEFAULT[name] for name in names], dtype=float)
    std = np.array(
        [(PORTFOLIO_BOUNDS[name][1] - PORTFOLIO_BOUNDS[name][0]) / 4.0 for name in names],
        dtype=float,
    )

    if use_wf:
        windows = create_wf_windows(train_df, train_eval_end)
        mode_tag = f"[TrainWindows:{len(windows)}]"
    else:
        train_start, _ = _frame_bounds(train_df)
        windows = [{"df": train_df, "start": train_start, "end": train_eval_end}]
        mode_tag = "[TrainFull]"

    tags = (
        (f"[Friction={HURDLE_MULT:.0f}x]" if use_hurdle else "")
        + mode_tag
        + ("[Kelly]" if use_kelly else "")
    )

    best_score = -np.inf
    best_policy: dict | None = None

    for iteration in range(n_iter):
        samples = rng.normal(mean, std, size=(pop, dim))
        policies = [port_policy_from_vec(sample) for sample in samples]
        scores: list[float] = []

        for policy in policies:
            window_scores: list[float] = []
            for window in windows:
                trades, equity, stats, _ = sim_opp_cost(
                    window["df"],
                    prices,
                    probs,
                    policy,
                    bench_sym=bench_sym,
                    initial=INITIAL_CAPITAL,
                    use_kelly=use_kelly,
                    start_date=window["start"],
                    end_date=window["end"],
                )
                window_scores.append(cem_reward(trades, equity, stats, use_hurdle))

            # Equal-length windows receive equal weight. Any invalid window
            # invalidates the policy rather than allowing a single easy slice
            # to hide a non-trading / sparse-trading failure elsewhere.
            score = INVALID_SCORE if any(s <= INVALID_SCORE / 2 for s in window_scores) else float(np.mean(window_scores))
            scores.append(score)

        score_array = np.asarray(scores, dtype=float)
        elite_idx = np.argsort(score_array)[-elite_count:]
        elite = samples[elite_idx]
        mean = elite.mean(axis=0)
        std = elite.std(axis=0) + 1e-4

        iteration_best_idx = int(np.argmax(score_array))
        iteration_best_score = float(score_array[iteration_best_idx])
        if iteration_best_score > best_score:
            best_score = iteration_best_score
            best_policy = policies[iteration_best_idx]

        print(
            f"    {bench_sym}|Objective{tags} iter {iteration + 1}/{n_iter}  "
            f"best={iteration_best_score:+.3f}  global={best_score:+.3f}",
            flush=True,
        )

    if best_policy is None or best_score <= INVALID_SCORE / 2:
        raise RuntimeError(
            f"No valid CEM policy for {bench_sym}. "
            "Increase available train data, reduce window strictness, or inspect candidate generation."
        )

    return best_policy, float(best_score), windows


# ── Database loading ─────────────────────────────────────────────────────────

async def load_paths(df: pd.DataFrame) -> tuple[dict, dict]:
    conn = await connect()
    try:
        symbols = sorted(set(df["symbol"].astype(str).unique()) | {"SPY", "QQQ"})
        markets = sorted(df["market_id"].astype(str).unique())

        bars = await conn.fetch(
            f"""
            SELECT symbol, ts, high, low, close
            FROM {SCHEMA}.historical_price_bars
            WHERE resolution = '1d'
              AND symbol = ANY($1::text[])
            ORDER BY symbol, ts
            """,
            symbols,
        )
        probability_rows = await conn.fetch(
            f"""
            SELECT DISTINCT ON (market_id, (hour_ts AT TIME ZONE 'UTC')::date)
                   market_id,
                   (hour_ts AT TIME ZONE 'UTC')::date AS d,
                   probability
            FROM {SCHEMA}.historical_probability_points
            WHERE market_id = ANY($1::text[])
              AND EXTRACT(HOUR FROM hour_ts AT TIME ZONE 'UTC') <= 20
            ORDER BY market_id, (hour_ts AT TIME ZONE 'UTC')::date, hour_ts DESC
            """,
            markets,
        )
    finally:
        await conn.close()

    prices: dict[str, list[tuple[pd.Timestamp, float, float, float]]] = {}
    for bar in bars:
        prices.setdefault(bar["symbol"], []).append(
            (
                as_utc_day(bar["ts"]),
                float(bar["high"]),
                float(bar["low"]),
                float(bar["close"]),
            )
        )

    probs: dict[str, list[tuple[pd.Timestamp, float]]] = {}
    for row in probability_rows:
        probs.setdefault(row["market_id"], []).append(
            (as_utc_day(row["d"]), float(row["probability"]))
        )

    for data in (prices, probs):
        for key in data:
            data[key].sort(key=lambda item: item[0])

    _CLOSE_CACHE.clear()
    _PATH_CUTOFF_CACHE.clear()
    return prices, probs


# ── Split, reporting, and audit output ───────────────────────────────────────

def split_train_val_test(
    df: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Timestamp, pd.Timestamp]:
    if "split" not in df.columns:
        raise ValueError("candidates.parquet must contain a chronological 'split' column.")

    split = df["split"].astype(str).str.lower().str.strip()
    train_df = df.loc[split == "train"].copy()
    val_df = df.loc[split == "val"].copy()
    test_df = df.loc[split == "test"].copy()

    if train_df.empty or val_df.empty or test_df.empty:
        raise ValueError(
            "Expected non-empty train/val/test splits; got "
            f"train={len(train_df)}, val={len(val_df)}, test={len(test_df)}."
        )

    val_start = as_utc_day(pd.to_datetime(val_df["t_theta"], utc=True).min())
    test_start = as_utc_day(pd.to_datetime(test_df["t_theta"], utc=True).min())
    overlapping_train = train_df[pd.to_datetime(train_df["t_theta"], utc=True) >= val_start]
    overlapping_val = val_df[pd.to_datetime(val_df["t_theta"], utc=True) >= test_start]
    if not overlapping_train.empty:
        raise ValueError(
            "The split is not chronological: some train candidates start on/after "
            "the first validation candidate. Rebuild candidates.parquet before trusting results."
        )
    if not overlapping_val.empty:
        raise ValueError(
            "The split is not chronological: some validation candidates start on/after "
            "the first test candidate. Rebuild candidates.parquet before trusting results."
        )

    return train_df, val_df, test_df, val_start, test_start


def save_audit_logs(
    *,
    experiment_label: str,
    benchmark: str,
    stage: str,
    trade_df: pd.DataFrame,
    equity_df: pd.DataFrame,
) -> None:
    TRADE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    EQUITY_LOG_DIR.mkdir(parents=True, exist_ok=True)

    stem = f"{benchmark.lower()}_{_slug(experiment_label)}_{_slug(stage)}"
    trade_df.to_csv(TRADE_LOG_DIR / f"{stem}.csv", index=False)
    equity_df.to_csv(EQUITY_LOG_DIR / f"{stem}.csv", index=False)


def _print_windows(windows: list[dict[str, Any]]) -> None:
    print(f"\n  Train-only rolling windows: {len(windows)}", flush=True)
    for i, window in enumerate(windows, start=1):
        print(
            f"    W{i}: {window['start'].date()} — {window['end'].date()}  "
            f"({len(window['df'])} candidates)",
            flush=True,
        )


def _print_table(results: list[dict[str, Any]], *, prefix: str, label: str) -> None:
    header = (
        f"  {'Experiment':<20} {label + ' Ret':>9} {'B&H':>9} {'Excess':>9} {label + ' DD':>9} "
        f"{'Trades':>7} {'Win%':>7} {'AvgNet$':>10} {'TradeCost$':>12} "
        f"{'AvgPos':>8} {'MaxConc':>8} {'Sample':>8}"
    )
    print(header)
    print(f"  {'-' * 137}")

    for row in results:
        sample = "thin" if row[f"{prefix}_trades"] < EVAL_TRADE_WARNING_N else "ok"
        print(
            f"  {row['experiment']:<20} "
            f"{row[f'{prefix}_return_pct']:>+8.2f}% "
            f"{row[f'{prefix}_benchmark_return_pct']:>+8.2f}% "
            f"{row[f'{prefix}_excess_return_pct']:>+8.2f}% "
            f"{row[f'{prefix}_max_dd_pct']:>+8.2f}% "
            f"{row[f'{prefix}_trades']:>6} "
            f"{row[f'{prefix}_win_rate_pct']:>6.1f}% "
            f"${row[f'{prefix}_avg_net_pnl']:>9.2f} "
            f"${row[f'{prefix}_trade_txn_cost']:>11.0f} "
            f"{row[f'{prefix}_avg_position_size_pct']:>7.1f}% "
            f"{row['policy_max_concurrent']:>7} "
            f"{sample:>8}",
            flush=True,
        )


def _stage_metrics(prefix: str, stats: dict[str, Any]) -> dict[str, Any]:
    return {
        f"{prefix}_return_pct": stats["total_return"],
        f"{prefix}_benchmark_return_pct": stats["benchmark_return"],
        f"{prefix}_excess_return_pct": stats["excess_return"],
        f"{prefix}_max_dd_pct": stats["max_dd"],
        f"{prefix}_start_date": stats["start_date"],
        f"{prefix}_end_date": stats["end_date"],
        f"{prefix}_equity_days": stats["n_equity_days"],
        f"{prefix}_trades": stats["n_trades"],
        f"{prefix}_win_rate_pct": stats["win_rate"],
        f"{prefix}_avg_net_pnl": stats["avg_pnl"],
        f"{prefix}_avg_gross_pnl": stats["avg_gross_pnl"],
        f"{prefix}_gross_trade_pnl": stats["gross_trade_pnl"],
        f"{prefix}_net_trade_pnl": stats["net_trade_pnl"],
        f"{prefix}_total_txn_cost": stats["total_txn_cost"],
        f"{prefix}_trade_txn_cost": stats["trade_txn_cost"],
        f"{prefix}_friction_fail_rate_pct": stats["friction_fail_rate"],
        f"{prefix}_avg_position_size_pct": stats["avg_position_size"],
        f"{prefix}_median_position_size_pct": stats["median_position_size"],
        f"{prefix}_min_position_size_pct": stats["min_position_size"],
        f"{prefix}_max_position_size_pct": stats["max_position_size"],
        f"{prefix}_thin_sample": stats["n_trades"] < EVAL_TRADE_WARNING_N,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    started = time.time()

    print("=" * 78)
    print("  CLEAN 8-EXPERIMENT COMPARISON")
    print("  Train-only CEM | Validation + frozen-policy test | Fully net trade costs")
    print("=" * 78)

    candidates_path = PROJECT / "data" / "candidates.parquet"
    df = pd.read_parquet(candidates_path)

    required_columns = {"symbol", "market_id", "t_theta", "t_e", "split", REL_COL}
    missing = sorted(required_columns - set(df.columns))
    if missing:
        raise ValueError(f"{candidates_path} is missing required columns: {missing}")

    df = df[df[REL_COL].astype(float) > 0.5].copy()
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True)
    df["t_e"] = pd.to_datetime(df["t_e"], utc=True)

    print(f"\n  {len(df)} relevance-filtered candidates loaded", flush=True)
    train_df, val_df, test_df, val_start, test_start = split_train_val_test(df)

    train_max_day = as_utc_day(train_df["t_theta"].max())
    train_eval_end = min(
        val_start - pd.Timedelta(days=1),
        train_max_day + pd.Timedelta(days=TRAIN_SETTLEMENT_BUFFER_DAYS),
    )
    if train_eval_end < as_utc_day(train_df["t_theta"].min()):
        raise ValueError("Train evaluation horizon ends before the first train candidate.")

    print(
        f"  train={len(train_df)}  val={len(val_df)}  test={len(test_df)}  "
        f"val starts={val_start.date()}  test starts={test_start.date()}  "
        f"train CEM ends={train_eval_end.date()}",
        flush=True,
    )

    prices, probs = asyncio.run(load_paths(df))
    print(f"  {len(prices)} symbols, {len(probs)} markets loaded", flush=True)

    common_benchmark_end = min(
        max(as_utc_day(t) for t, *_ in prices[benchmark])
        for benchmark in ("SPY", "QQQ")
    )
    val_eval_end = min(
        test_start - pd.Timedelta(days=1),
        as_utc_day(val_df["t_e"].max()),
        common_benchmark_end,
    )
    test_eval_end = min(as_utc_day(test_df["t_e"].max()), common_benchmark_end)
    if val_eval_end < val_start:
        raise ValueError(
            f"Validation evaluation horizon {val_eval_end.date()} is before "
            f"validation start {val_start.date()}."
        )
    if test_eval_end < test_start:
        raise ValueError(
            f"Test evaluation horizon {test_eval_end.date()} is before test start {test_start.date()}."
        )
    print(
        f"  fixed validation eval ends={val_eval_end.date()}  "
        f"fixed test eval ends={test_eval_end.date()}",
        flush=True,
    )

    preview_windows = create_wf_windows(train_df, train_eval_end)
    _print_windows(preview_windows)

    all_results: list[dict[str, Any]] = []

    for experiment_number, experiment in enumerate(EXPERIMENTS, start=1):
        label = experiment["label"]
        use_hurdle = bool(experiment["hurdle"])
        use_wf = bool(experiment["wf"])
        use_kelly = bool(experiment["kelly"])

        print(f"\n{'=' * 78}")
        print(f"  EXPERIMENT {experiment_number}/8: {label}", flush=True)
        flags: list[str] = []
        if use_hurdle:
            flags.append(f"realised friction penalty={HURDLE_MULT:.0f}x")
        if use_wf:
            flags.append(f"train-only windows={len(preview_windows)}")
        if use_kelly:
            flags.append("half-Kelly sizing")
        print(f"  Techniques: {', '.join(flags) if flags else 'none'}", flush=True)
        print(f"{'=' * 78}", flush=True)

        for benchmark in ("SPY", "QQQ"):
            print(f"\n  [Train CEM search — {benchmark}]", flush=True)
            policy, objective, used_windows = cem_search(
                train_df,
                prices,
                probs,
                bench_sym=benchmark,
                use_hurdle=use_hurdle,
                use_wf=use_wf,
                use_kelly=use_kelly,
                train_eval_end=train_eval_end,
                n_iter=CEM_ITERS,
                pop=CEM_POP,
                seed=CEM_BASE_SEED + BENCHMARK_SEED_OFFSET[benchmark],
            )

            # This is only a training diagnostic. It is never combined with OOS.
            train_trades, train_equity, train_stats, _ = sim_opp_cost(
                train_df,
                prices,
                probs,
                policy,
                bench_sym=benchmark,
                initial=INITIAL_CAPITAL,
                use_kelly=use_kelly,
                start_date=as_utc_day(train_df["t_theta"].min()),
                end_date=train_eval_end,
            )

            # Validation/test simulations use fresh capital. Kelly can use only
            # completed history available before each evaluation stage starts.
            kelly_train_history = (
                train_trades.loc[
                    train_trades["realized_exit_reason"] != "evaluation_end_liquidation"
                ].to_dict("records")
                if use_kelly and not train_trades.empty
                else None
            )

            print(f"\n  [Frozen-policy validation sim — {benchmark}]", flush=True)
            val_trades, val_equity, val_stats, _ = sim_opp_cost(
                val_df,
                prices,
                probs,
                policy,
                bench_sym=benchmark,
                initial=INITIAL_CAPITAL,
                use_kelly=use_kelly,
                start_date=val_start,
                end_date=val_eval_end,
                initial_kelly_history=kelly_train_history,
            )

            kelly_test_history = None
            if use_kelly:
                kelly_test_history = list(kelly_train_history or [])
                if not val_trades.empty:
                    kelly_test_history.extend(
                        val_trades.loc[
                            val_trades["realized_exit_reason"] != "evaluation_end_liquidation"
                        ].to_dict("records")
                    )

            print(f"\n  [Frozen-policy test sim — {benchmark}]", flush=True)
            oos_trades, oos_equity, oos_stats, _ = sim_opp_cost(
                test_df,
                prices,
                probs,
                policy,
                bench_sym=benchmark,
                initial=INITIAL_CAPITAL,
                use_kelly=use_kelly,
                start_date=test_start,
                end_date=test_eval_end,
                initial_kelly_history=kelly_test_history,
            )

            save_audit_logs(
                experiment_label=label,
                benchmark=benchmark,
                stage="validation",
                trade_df=val_trades,
                equity_df=val_equity,
            )
            save_audit_logs(
                experiment_label=label,
                benchmark=benchmark,
                stage="test",
                trade_df=oos_trades,
                equity_df=oos_equity,
            )

            result = {
                "experiment": label,
                "benchmark": benchmark,
                "hurdle_realized_fitness_penalty": use_hurdle,
                "train_windows": use_wf,
                "kelly": use_kelly,
                "cem_objective": round(objective, 6),
                "train_return_pct": train_stats["total_return"],
                "train_benchmark_return_pct": train_stats["benchmark_return"],
                "train_excess_return_pct": train_stats["excess_return"],
                "train_max_dd_pct": train_stats["max_dd"],
                "train_trades": train_stats["n_trades"],
                "policy_base_position_size_pct": round(float(policy["position_size_pct"]) * 100.0, 4),
                "policy_max_concurrent": int(policy["max_concurrent"]),
                "policy_json": json.dumps(policy, sort_keys=True),
            }
            result.update(_stage_metrics("val", val_stats))
            result.update(_stage_metrics("oos", oos_stats))
            all_results.append(result)

            print(
                f"    → VAL={val_stats['total_return']:+.2f}%  "
                f"B&H={val_stats['benchmark_return']:+.2f}%  "
                f"excess={val_stats['excess_return']:+.2f}%  "
                f"max_dd={val_stats['max_dd']:+.2f}%  "
                f"trades={val_stats['n_trades']}  "
                f"sample={'thin' if val_stats['n_trades'] < EVAL_TRADE_WARNING_N else 'ok'}",
                flush=True,
            )
            print(
                f"    → TEST={oos_stats['total_return']:+.2f}%  "
                f"B&H={oos_stats['benchmark_return']:+.2f}%  "
                f"excess={oos_stats['excess_return']:+.2f}%  "
                f"max_dd={oos_stats['max_dd']:+.2f}%  "
                f"trades={oos_stats['n_trades']}  "
                f"win%={oos_stats['win_rate']:.1f}%  "
                f"trade_cost=${oos_stats['trade_txn_cost']:.0f}  "
                f"avg_pos={oos_stats['avg_position_size']:.1f}%  "
                f"max_conc={int(policy['max_concurrent'])}  "
                f"sample={'thin' if oos_stats['n_trades'] < EVAL_TRADE_WARNING_N else 'ok'}",
                flush=True,
            )

    print(f"\n\n{'=' * 78}")
    print("  VALIDATION RESULTS — SPY benchmark")
    print(f"{'=' * 78}")
    _print_table([row for row in all_results if row["benchmark"] == "SPY"], prefix="val", label="Val")

    print(f"\n{'=' * 78}")
    print("  VALIDATION RESULTS — QQQ benchmark")
    print(f"{'=' * 78}")
    _print_table([row for row in all_results if row["benchmark"] == "QQQ"], prefix="val", label="Val")

    print(f"\n{'=' * 78}")
    print("  FINAL TEST RESULTS — SPY benchmark")
    print(f"{'=' * 78}")
    _print_table([row for row in all_results if row["benchmark"] == "SPY"], prefix="oos", label="Test")

    print(f"\n{'=' * 78}")
    print("  FINAL TEST RESULTS — QQQ benchmark")
    print(f"{'=' * 78}")
    _print_table([row for row in all_results if row["benchmark"] == "QQQ"], prefix="oos", label="Test")

    RESULTS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS_CSV, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(all_results[0].keys()))
        writer.writeheader()
        writer.writerows(all_results)

    elapsed = time.time() - started
    print(f"\n  Clean results saved to: {RESULTS_CSV}")
    print(f"  Validation/test trade logs saved to: {TRADE_LOG_DIR}")
    print(f"  Validation/test equity logs saved to: {EQUITY_LOG_DIR}")
    print(f"  Total elapsed: {elapsed / 60.0:.1f} min")


if __name__ == "__main__":
    main()
