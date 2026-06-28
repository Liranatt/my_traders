"""
Portfolio simulator driven by the RL policy.

This mirrors run_experiments.sim_opp_cost's benchmark-rotation accounting (cash
sits in SPY/QQQ; opening a trade sells benchmark to fund the asset buy, closing
rebuys benchmark) but takes per-day HOLD/ENTER/EXIT decisions from a policy
instead of pre-computing trades. It also provides:
  * a long-only-to-resolution baseline (is_baseline_long_only),
  * a fixed-replay mode (fixed_trades) used by the parity test to reproduce
    sim_opp_cost exactly for a fixed policy.

stats are computed with the same formulas as sim_opp_cost, so excess return vs
the passive SPY/QQQ benchmark, Sharpe, and max drawdown are directly comparable.
"""
from __future__ import annotations

import bisect
import math
from typing import Any

import numpy as np
import pandas as pd
import torch

from rl.config import (
    ACTION_DIM,
    ACTION_EXIT,
    ACTION_HOLD,
    ENTER_ACTIONS,
    is_enter_action,
    entry_params_for_action,
)
from rl.exits import bar_on, evaluate_hard_exit, update_peak_ret
from rl.features import (
    build_observation,
    compute_market_state,
    compute_position_state,
    entry_signal_day,
    static_features_from_row,
)
from rl.shared import (
    DEFAULT_POLICY,
    INITIAL_CAPITAL,
    MIN_DAILY_RETURNS_FOR_SHARPE,
    as_utc_day,
    ib_cost,
    kelly_size,
    _affordable_buy_qty,
    _calendar_dates,
    _close_on,
    truncate_paths,
)

_DAY = pd.Timedelta(days=1)


def _greedy_action(policy, obs: np.ndarray, mask: np.ndarray) -> int:
    obs_t = torch.as_tensor(obs, dtype=torch.float32).unsqueeze(0)
    mask_t = torch.as_tensor(mask, dtype=torch.bool).unsqueeze(0)
    with torch.no_grad():
        logits, _ = policy.forward(obs_t)
        logits = logits.masked_fill(~mask_t, float("-inf"))
        return int(torch.argmax(logits, dim=-1).item())


def _masked_action_probs(policy, obs: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, torch.Tensor]:
    obs_t = torch.as_tensor(obs, dtype=torch.float32).unsqueeze(0)
    mask_t = torch.as_tensor(mask, dtype=torch.bool).unsqueeze(0)
    with torch.no_grad():
        logits, value = policy.forward(obs_t)
        logits = logits.masked_fill(~mask_t, float("-inf"))
        probs = torch.softmax(logits, dim=-1)[0].cpu().numpy()
    return probs, value


def _frac(cal_values: list[int], day) -> float:
    if len(cal_values) <= 1:
        return 0.0
    i = bisect.bisect_right(cal_values, day.value) - 1
    i = max(0, min(i, len(cal_values) - 1))
    return i / (len(cal_values) - 1)


def sim_with_policy(
    df: pd.DataFrame,
    prices: dict,
    probs: dict,
    policy: Any = None,
    *,
    scaler: dict | None = None,
    bench_sym: str = "SPY",
    initial: float = INITIAL_CAPITAL,
    use_kelly: bool = False,
    base_ps: float = 0.10,
    max_concurrent: int = 10,
    start_date: Any | None = None,
    end_date: Any | None = None,
    initial_kelly_history: list[dict] | None = None,
    is_baseline_long_only: bool = False,
    fixed_trades: dict | None = None,
    entry_policy: dict | None = None,
    exit_threshold: float = 0.50,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """Returns (trade_df, equity_df, stats) — same order as sim_opp_cost."""
    entry_policy = entry_policy or DEFAULT_POLICY
    empty_stats = _empty_stats(initial)
    if df.empty:
        return pd.DataFrame(), pd.DataFrame(), empty_stats

    sim_prices, sim_probs = truncate_paths(prices, probs, end_date)

    # Per-candidate point-in-time metadata (entry-band day + own trading calendar).
    cand_info: dict[Any, dict] = {}
    for cid, row in df.iterrows():
        sig = (
            None
            if fixed_trades is not None
            else entry_signal_day(sim_probs, row["market_id"], row["t_theta"], entry_policy)
        )
        t_e = as_utc_day(row["t_e"])
        start = sig if sig is not None else as_utc_day(row["t_theta"])
        cal = _calendar_dates(sim_prices, bench_sym, start, t_e)
        resolution_cut = t_e - _DAY
        cap_days = [d for d in cal if d <= resolution_cut]
        cand_info[cid] = {
            "row": row,
            "static": static_features_from_row(row),
            "symbol": str(row["symbol"]),
            "entry_sig": sig,
            "t_e": t_e,
            "resolution_exit_day": cap_days[-1] if cap_days else None,
            "cal_values": [d.value for d in cal],
            "fixed": (fixed_trades or {}).get(cid),
        }

    candidate_start = as_utc_day(df["t_theta"].min())
    candidate_end = as_utc_day(df["t_e"].max())
    eval_start = as_utc_day(start_date) if start_date is not None else candidate_start
    eval_end = as_utc_day(end_date) if end_date is not None else candidate_end

    calendar = _calendar_dates(sim_prices, bench_sym, eval_start, eval_end)
    if not calendar:
        return pd.DataFrame(), pd.DataFrame(), empty_stats

    first_day, last_day = calendar[0], calendar[-1]
    first_bench_close = _close_on(sim_prices, bench_sym, first_day)
    last_bench_close = _close_on(sim_prices, bench_sym, last_day)
    if not first_bench_close or not last_bench_close:
        return pd.DataFrame(), pd.DataFrame(), empty_stats

    initial_bench_shares = _affordable_buy_qty(initial, first_bench_close)
    initial_cost = ib_cost(initial_bench_shares, first_bench_close, False)
    initial_cash = initial - initial_bench_shares * first_bench_close - initial_cost

    bench_shares = initial_bench_shares
    cash = float(initial_cash)
    total_txn_cost = float(initial_cost)

    open_positions: list[dict] = []
    completed: list[dict] = []
    kelly_history = [dict(item) for item in (initial_kelly_history or [])]
    equity_rows: list[dict] = []

    ordered = sorted(df.index, key=lambda cid: cand_info[cid]["row"]["t_theta"])
    cand_idx = 0
    opened_ids: set = set()
    pending: list = []  # eligible-but-not-yet-entered candidates (kept until entered/expired)

    def close_position(pos: dict, day, exit_price: float, reason: str) -> None:
        nonlocal cash, bench_shares, total_txn_cost
        qty = int(pos["qty"])
        entry_price = float(pos["entry_price"])
        asset_sell_cost = ib_cost(qty, exit_price, True)
        sale_proceeds = qty * exit_price - asset_sell_cost
        bench_close = float(_close_on(sim_prices, bench_sym, day) or 0.0)
        rebuy_qty = _affordable_buy_qty(sale_proceeds, bench_close)
        rebuy_cost = ib_cost(rebuy_qty, bench_close, False)
        cash += sale_proceeds - rebuy_qty * bench_close - rebuy_cost
        bench_shares += rebuy_qty
        total_txn_cost += asset_sell_cost + rebuy_cost
        direct_cost = pos["bench_sell_cost"] + pos["asset_buy_cost"] + asset_sell_cost + rebuy_cost
        gross = qty * (exit_price - entry_price)
        net = gross - direct_cost
        notional = max(float(pos["entry_notional"]), 1e-12)
        info = cand_info[pos["cid"]]
        entry_day = as_utc_day(pos["entry_day"])
        exit_day = as_utc_day(day)
        entry_sig = info["entry_sig"]
        trade = {
            "candidate_id": str(pos["cid"]),
            "market_id": str(info["row"]["market_id"]),
            "symbol": pos["symbol"],
            "candidate_t_theta": str(as_utc_day(info["row"]["t_theta"]).date()),
            "candidate_t_e": str(pos["t_e"].date()),
            "entry_signal_date": str(as_utc_day(entry_sig).date()) if entry_sig is not None else None,
            "entry_date": str(entry_day.date()),
            "exit_date": str(exit_day.date()),
            "entry_price": round(entry_price, 4),
            "exit_price": round(float(exit_price), 4),
            "qty": qty,
            "gross_pnl": round(gross, 2),
            "pnl": round(net, 2),
            "pnl_pct": round(net / notional * 100.0, 4),
            "txn_cost": round(direct_cost, 2),
            "realized_exit_reason": reason,
            "hold_days_calendar": int((exit_day - entry_day).days),
            "entry_delay_days": int((entry_day - as_utc_day(entry_sig)).days) if entry_sig is not None else None,
            "_position_size_pct": float(pos["position_size_pct"]),
        }
        completed.append(trade)
        kelly_history.append(trade)

    for day in calendar:
        bench_close = _close_on(sim_prices, bench_sym, day)
        if bench_close is None:
            continue

        # 1. Exits first.
        still_open = []
        for pos in open_positions:
            info = cand_info[pos["cid"]]
            asset_bar = bar_on(sim_prices, pos["symbol"], day)
            asset_price = asset_bar.close if asset_bar is not None else None
            resolution_day = info["resolution_exit_day"]
            resolution = resolution_day is not None and day >= resolution_day
            do_exit = False
            exit_price = float(asset_price if asset_price else pos["entry_price"])
            reason = "policy_exit"

            if info["fixed"] is not None:
                do_exit = as_utc_day(info["fixed"]["exit_date"]) <= day
                if do_exit:
                    exit_price = float(info["fixed"]["exit_price"])
                    reason = "fixed_exit"
            elif is_baseline_long_only:
                do_exit = resolution or day >= last_day or asset_price is None
                reason = "resolution-1d" if resolution else ("end_liquidation" if day >= last_day else "missing_price")
            else:
                hard_exit = evaluate_hard_exit(
                    prices=sim_prices,
                    probs=sim_probs,
                    symbol=pos["symbol"],
                    market_id=info["row"]["market_id"],
                    day=day,
                    entry_day=pos["entry_day"],
                    entry_price=pos["entry_price"],
                    t_e=info["t_e"],
                    resolution_exit_day=info["resolution_exit_day"],
                    expected_return=max(0.001, abs(float(info["row"].get("feat_llm_expected_return", 0.01)))),
                    peak_ret=pos["peak_ret"],
                )
                if hard_exit is not None:
                    do_exit = True
                    exit_price = float(hard_exit.exit_price)
                    reason = hard_exit.reason
                elif day >= last_day or asset_price is None:
                    do_exit = True
                    reason = "end_liquidation" if day >= last_day else "missing_price"
                else:
                    frac = _frac(info["cal_values"], day)
                    market = compute_market_state(sim_prices, sim_probs, pos["symbol"], info["row"]["market_id"], bench_sym, day)
                    position = compute_position_state(
                        is_long=True, entry_price=pos["entry_price"], asset_price=asset_price,
                        bench_entry_price=float(pos.get("bench_entry_price", 0.0)),
                        bench_price=bench_close,
                        peak_ret=pos["peak_ret"], window_fraction=frac,
                        position_size_pct=float(pos["position_size_pct"]),
                    )
                    obs = build_observation(info["static"], position, market, scaler)
                    mask = np.zeros(ACTION_DIM, dtype=bool)
                    mask[ACTION_HOLD] = True
                    mask[ACTION_EXIT] = True

                    action_probs, value = _masked_action_probs(policy, obs, mask)
                    p_hold = float(action_probs[ACTION_HOLD])
                    p_exit = float(action_probs[ACTION_EXIT])
                    with open("rl_brain_dump.log", "a") as log_f:
                        log_f.write(
                            f"[{day.date()}] {pos['symbol']} | Peak: {pos['peak_ret']:.3f} "
                            f"Unr: {position['unrealized_ret']:.3f} | V(s): {value.item():.2f} | "
                            f"P(HOLD): {p_hold:.1%} P(EXIT): {p_exit:.1%} "
                            f"exit_th={exit_threshold:.2f}\n"
                        )

                    do_exit = p_exit >= float(exit_threshold)
                    reason = "policy_exit"

            if do_exit:
                close_position(pos, day, exit_price, reason)
            else:
                pos["peak_ret"] = update_peak_ret(sim_prices, pos["symbol"], day, pos["entry_price"], pos["peak_ret"])
                still_open.append(pos)
        open_positions = still_open

        # 2. Entries. Move newly-eligible candidates into `pending`, then let the
        # policy decide each pending candidate every day until it enters/expires
        # (so HOLD-now-enter-later is possible; nothing is skipped permanently).
        while cand_idx < len(ordered) and as_utc_day(cand_info[ordered[cand_idx]]["row"]["t_theta"]) <= day:
            pending.append(ordered[cand_idx])
            cand_idx += 1

        next_pending: list = []
        for cid in pending:
            info = cand_info[cid]
            if cid in opened_ids:
                continue
            if day > info["t_e"]:
                continue  # window passed; drop
            resolution_day = info["resolution_exit_day"]
            if info["fixed"] is None and (resolution_day is None or day >= resolution_day):
                continue  # no tradable day remains before the hard resolution cap
            if info["entry_sig"] is None and info["fixed"] is None:
                continue  # band never fires; drop

            entry_price = _close_on(sim_prices, info["symbol"], day)
            chosen_position_size: float | None = None
            if info["fixed"] is not None:
                fixed_entry = as_utc_day(info["fixed"]["entry_date"])
                if day < fixed_entry:
                    next_pending.append(cid)
                    continue
                entry_price = float(info["fixed"]["entry_price"])
                want_enter = day == fixed_entry
                if want_enter and "_position_size_pct" in info["fixed"]:
                    chosen_position_size = float(info["fixed"]["_position_size_pct"])
            else:
                if day < info["entry_sig"] or not entry_price:
                    next_pending.append(cid)  # not eligible yet
                    continue
                if is_baseline_long_only:
                    want_enter = True
                else:
                    frac = _frac(info["cal_values"], day)
                    market = compute_market_state(sim_prices, sim_probs, info["symbol"], info["row"]["market_id"], bench_sym, day)
                    position = compute_position_state(
                        is_long=False, entry_price=0.0, asset_price=None, peak_ret=0.0,
                        window_fraction=frac,
                        position_size_pct=0.0,
                    )
                    obs = build_observation(info["static"], position, market, scaler)
                    mask = np.zeros(ACTION_DIM, dtype=bool)
                    mask[ACTION_HOLD] = True
                    mask[list(ENTER_ACTIONS)] = True
                    action = _greedy_action(policy, obs, mask)
                    want_enter = is_enter_action(action)
                    if want_enter:
                        params = entry_params_for_action(action)
                        chosen_position_size = params.position_size_pct

            blocked = (
                len(open_positions) >= max_concurrent
                or any(p["symbol"] == info["symbol"] for p in open_positions)
            )
            if not want_enter or blocked or not entry_price or entry_price <= 0:
                next_pending.append(cid)  # reconsider next day
                continue

            currently_deployed = sum(p["position_size_pct"] for p in open_positions)
            max_allowed = min(
                base_ps,
                max(0.0, 1.0 - currently_deployed)  # never let total exceed 100%
            )
            position_size = (
                chosen_position_size
                if chosen_position_size is not None
                else min(kelly_size(kelly_history, base_ps), max_allowed) if use_kelly else base_ps
            )
            if position_size <= 0.0:
                next_pending.append(cid)
                continue
            open_value = sum(int(p["qty"]) * float(_close_on(sim_prices, p["symbol"], day) or p["entry_price"]) for p in open_positions)
            current_equity = bench_shares * bench_close + open_value + cash
            desired_allocation = current_equity * position_size
            if desired_allocation < entry_price:
                next_pending.append(cid)
                continue
            bench_sell_qty = min(int(desired_allocation / bench_close), bench_shares)
            if bench_sell_qty < 1:
                next_pending.append(cid)
                continue
            bench_sell_cost = ib_cost(bench_sell_qty, bench_close, True)
            available = bench_sell_qty * bench_close - bench_sell_cost
            asset_qty = _affordable_buy_qty(available, entry_price)
            if asset_qty < 1:
                next_pending.append(cid)
                continue
            asset_buy_cost = ib_cost(asset_qty, entry_price, False)
            need = asset_qty * entry_price + asset_buy_cost
            if need > available + 1e-9:
                next_pending.append(cid)
                continue

            bench_shares -= bench_sell_qty
            cash += available - need
            total_txn_cost += bench_sell_cost + asset_buy_cost
            opened_ids.add(cid)
            open_positions.append({
                "cid": cid,
                "symbol": info["symbol"],
                "qty": asset_qty,
                "entry_price": float(entry_price),
                "bench_entry_price": float(bench_close),
                "entry_day": day,
                "entry_notional": asset_qty * entry_price,
                "bench_sell_cost": bench_sell_cost,
                "asset_buy_cost": asset_buy_cost,
                "position_size_pct": float(position_size),
                "peak_ret": 0.0,
                "t_e": info["t_e"],
                "is_earnings": "earnings" in str(info["row"].get("feat_archetype", "")).lower(),
            })
        pending = next_pending

        # 3. Mark equity.
        open_value = sum(int(p["qty"]) * float(_close_on(sim_prices, p["symbol"], day) or p["entry_price"]) for p in open_positions)
        equity = bench_shares * bench_close + open_value + cash
        passive = initial_bench_shares * bench_close + initial_cash
        equity_rows.append({
            "date": str(as_utc_day(day).date()),
            "equity": round(equity, 2),
            "benchmark_equity": round(passive, 2),
            "cash": round(cash, 2),
            "open_positions": len(open_positions),
        })

    # Liquidate remaining positions at the last day.
    for pos in list(open_positions):
        px = _close_on(sim_prices, pos["symbol"], last_day) or pos["entry_price"]
        close_position(pos, last_day, float(px), "end_liquidation")
    open_positions = []

    final_equity = bench_shares * last_bench_close + cash
    final_passive = initial_bench_shares * last_bench_close + initial_cash
    if equity_rows:
        equity_rows[-1]["equity"] = round(final_equity, 2)
        equity_rows[-1]["benchmark_equity"] = round(final_passive, 2)
        equity_rows[-1]["cash"] = round(cash, 2)
        equity_rows[-1]["open_positions"] = 0

    equity_df = pd.DataFrame(equity_rows)
    trade_df = pd.DataFrame(completed)
    stats = _compute_stats(
        equity_df, trade_df, initial, final_equity, final_passive,
        total_txn_cost, first_day, last_day,
    )
    return trade_df, equity_df, stats


def _empty_stats(initial: float) -> dict:
    return {
        "initial": initial, "final": initial, "total_return": 0.0, "benchmark_return": 0.0,
        "excess_return": 0.0, "max_dd": 0.0, "sharpe": 0.0, "n_trades": 0, "win_rate": 0.0,
        "avg_pnl": 0.0, "net_trade_pnl": 0.0, "trade_txn_cost": 0.0, "total_txn_cost": 0.0,
        "avg_position_size": 0.0, "start_date": None, "end_date": None, "n_equity_days": 0,
        "sharpe_daily_returns": 0,
        "min_daily_returns_for_sharpe": MIN_DAILY_RETURNS_FOR_SHARPE,
        "sharpe_is_defined": False,
    }


def _compute_stats(equity_df, trade_df, initial, final_equity, final_passive, total_txn_cost, first_day, last_day) -> dict:
    stats = _empty_stats(initial)
    if equity_df.empty:
        return stats
    eq = equity_df["equity"].astype(float).to_numpy()
    peaks = np.maximum.accumulate(eq)
    dd = np.where(peaks > 0, eq / peaks - 1.0, 0.0)
    max_dd = float(np.min(dd) * 100.0) if len(dd) else 0.0

    daily_ret = pd.Series(eq).pct_change().dropna()
    sharpe = 0.0
    sharpe_is_defined = len(daily_ret) >= MIN_DAILY_RETURNS_FOR_SHARPE
    if sharpe_is_defined and daily_ret.std(ddof=1) > 1e-12:
        sharpe = float(daily_ret.mean() / daily_ret.std(ddof=1) * math.sqrt(252.0))

    stats.update({
        "final": round(final_equity, 2),
        "total_return": round((final_equity / initial - 1.0) * 100.0, 4),
        "benchmark_return": round((final_passive / initial - 1.0) * 100.0, 4),
        "excess_return": round((final_equity - final_passive) / initial * 100.0, 4),
        "max_dd": round(max_dd, 4),
        "sharpe": round(sharpe, 4),
        "total_txn_cost": round(float(total_txn_cost), 2),
        "start_date": str(as_utc_day(first_day).date()),
        "end_date": str(as_utc_day(last_day).date()),
        "n_equity_days": int(len(equity_df)),
        "sharpe_daily_returns": int(len(daily_ret)),
        "min_daily_returns_for_sharpe": MIN_DAILY_RETURNS_FOR_SHARPE,
        "sharpe_is_defined": bool(sharpe_is_defined),
    })
    if not trade_df.empty:
        stats.update({
            "n_trades": int(len(trade_df)),
            "win_rate": round(float((trade_df["pnl"] > 0).mean() * 100.0), 4),
            "avg_pnl": round(float(trade_df["pnl"].mean()), 4),
            "net_trade_pnl": round(float(trade_df["pnl"].sum()), 2),
            "trade_txn_cost": round(float(trade_df["txn_cost"].sum()), 2),
            "avg_position_size": round(float(trade_df["_position_size_pct"].mean() * 100.0), 4),
        })
    return stats
