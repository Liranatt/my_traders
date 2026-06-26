"""
Leakage-safe RF experiment runner.

This runner separates four different evaluation roles:

1. CEM training diagnostic:
   CEM only sees causally available training labels and train-period paths.
2. Static OOS validation:
   One RF + one CEM policy are frozen at the start of the pre-terminal test
   period and evaluated without any retraining.
3. Expanding online walk-forward diagnostic (T2 family by default):
   At each evaluation window, RF and CEM are re-fit using only labels that
   were available before that window.
4. Final terminal holdout:
   A final chronological segment of the original test split is never used by
   RF fitting, CEM, policy selection, or online walk-forward refitting.

Corrections implemented relative to the prior RF runner:
  * No test-period CEM leakage.
  * Causal expanding, purged OOF RF predictions; no KFold/cross_val_predict.
  * The label-availability timestamp controls training eligibility.
  * Fully net per-trade P&L: benchmark sell + asset buy + asset sell +
    benchmark rebuy costs.
  * T1 is an ENTRY-ONLY ex-ante friction hurdle. It is not also a realised
    CEM penalty, so the ablation is identifiable.
  * The CEM objective uses daily portfolio-equity Sharpe, not trade Sharpe.
  * Dynamic WFO policies control actual entry/exit policy, position size, and
    max-concurrency at each candidate entry.
  * Benchmark return is calculated on the exact same portfolio dates.
  * Outer CEM seeds are repeated and reported separately and in aggregate.
  * Every OOS stage writes trade and equity audit logs.

Important modelling assumption:
  LABEL_AVAILABLE_COL must be the first timestamp at which TARGET is fully
  observable. It defaults to ``t_e`` because that is the field supplied in the
  original runner. Change it if t_e is not the true target-completion time.

Usage:
    python -u run_experiments_rf_fixed.py

Output files are written under data/rf_experiment_* and do not overwrite the
legacy experiment CSV.
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
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline as SkPipeline

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from pipeline.data_loader import NUM_FEATURES_LEAN, TARGET
from pipeline.strategy import DEFAULT_POLICY, simulate_one


# ── Paths ────────────────────────────────────────────────────────────────────

PROJECT = Path(__file__).resolve().parent
CANDIDATES_PATH = PROJECT / "data" / "candidates.parquet"

RESULT_DIR = PROJECT / "data"
STATIC_RESULTS_CSV = RESULT_DIR / "rf_experiment_static_oos.csv"
WFO_RESULTS_CSV = RESULT_DIR / "rf_experiment_online_wfo.csv"
TERMINAL_RESULTS_CSV = RESULT_DIR / "rf_experiment_terminal_holdout.csv"
SUMMARY_RESULTS_CSV = RESULT_DIR / "rf_experiment_summary.csv"
POLICY_LOG_DIR = RESULT_DIR / "rf_experiment_policy_logs"
TRADE_LOG_DIR = RESULT_DIR / "rf_experiment_trade_logs"
EQUITY_LOG_DIR = RESULT_DIR / "rf_experiment_equity_logs"


# ── Data / split assumptions ─────────────────────────────────────────────────

REL_COL = "feat_connection_strength"
LABEL_AVAILABLE_COL = "t_e"

# The final terminal block is removed from the original split=='test' data
# before ANY validation read, WFO refit, RF fit, or CEM search.
FINAL_HOLDOUT_MONTHS = 6
FINAL_HOLDOUT_FRACTION_FALLBACK = 0.30
MIN_PRETERMINAL_TEST_CANDIDATES = 30
MIN_TERMINAL_TEST_CANDIDATES = 30

# A small temporal gap protects train labels / candidate outcomes from leaking
# across a train-evaluation or WFO boundary.
PURGE_DAYS = 7
SETTLEMENT_BUFFER_DAYS = 7

# "auto" is practical for the current project. For a target encoded as 8.0 for
# +8%, the code uses percent. For 0.08 for +8%, it uses decimal. Set explicitly
# to "percent" or "decimal" after checking your target definition.
TARGET_RETURN_UNIT: str = "auto"  # one of: auto, percent, decimal

# Optional benchmark-specific excess-return targets. If one exists in the
# parquet file it is used for that benchmark. Otherwise the code falls back to
# TARGET and prints a warning that the hurdle is based on raw asset return.
BENCHMARK_EXCESS_TARGET_CANDIDATES: dict[str, tuple[str, ...]] = {
    "SPY": ("target_excess_spy", "excess_return_spy", "asset_excess_return_spy"),
    "QQQ": ("target_excess_qqq", "excess_return_qqq", "asset_excess_return_qqq"),
}


# ── Portfolio and experiment controls ────────────────────────────────────────

INITIAL_CAPITAL = 100_000.0

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
PORT_DEFAULT: dict[str, Any] = {
    **DEFAULT_POLICY,
    "position_size_pct": 0.10,
    "max_concurrent": 10,
}

# T1: Trade must have predicted gross edge >= HURDLE_MULT * estimated all-in
# benchmark rotation costs. This is applied at entry in simulation and CEM.
HURDLE_MULT = 3.0

# T2: internal train-only rolling CEM evaluation windows.
WF_EVAL_MON = 3
WF_STEP_MON = 3
WF_MIN_CANDS = 8

# Online prequential evaluation windows. They do not overlap by default.
ONLINE_WFO_EVAL_MON = 3
ONLINE_WFO_STEP_MON = 3
ONLINE_WFO_MIN_TRAIN_ROWS = 80
RUN_ONLINE_WFO_FOR_ALL_EXPERIMENTS = False

# T3: half-Kelly implementation.
KELLY_MIN_N = 10
KELLY_LOOKBACK_N = 30
KELLY_MIN_SZ = 0.03
KELLY_MAX_SZ = 0.15

# RF and causal OOF settings.
RF_N_ESTIMATORS = 100
RF_MAX_DEPTH = 6
RF_MIN_SAMPLES_LEAF = 15
CAUSAL_OOF_FOLDS = 5
CAUSAL_OOF_MIN_TRAIN_ROWS = 80
MIN_CEM_RF_CANDIDATES = 25

# CEM settings. Every ablation receives the same benchmark-specific initial
# random stream for each outer seed.
CEM_ITERS = 6
CEM_POP = 20
CEM_ELITE_FRAC = 0.25
OUTER_SEEDS = (42, 43, 44)
BENCHMARK_SEED_OFFSET = {"SPY": 0, "QQQ": 10_000}

# Daily-equity objective settings.
MIN_TRADES_FOR_REWARD = 8
MIN_DAILY_RETURNS_FOR_SHARPE = 20
DD_PENALTY = 0.30
INVALID_SCORE = -1e12

EXPERIMENTS = [
    {"id": 0, "label": "Baseline", "hurdle": False, "wf": False, "kelly": False},
    {"id": 1, "label": "T1 FrictionHurdle", "hurdle": True, "wf": False, "kelly": False},
    {"id": 2, "label": "T2 TrainWindows", "hurdle": False, "wf": True, "kelly": False},
    {"id": 3, "label": "T3 Kelly", "hurdle": False, "wf": False, "kelly": True},
    {"id": 4, "label": "T1+T2", "hurdle": True, "wf": True, "kelly": False},
    {"id": 5, "label": "T1+T3", "hurdle": True, "wf": False, "kelly": True},
    {"id": 6, "label": "T2+T3", "hurdle": False, "wf": True, "kelly": True},
    {"id": 7, "label": "T1+T2+T3", "hurdle": True, "wf": True, "kelly": True},
]


# ── Time / price helpers ─────────────────────────────────────────────────────

_CLOSE_CACHE: dict[tuple[int, str], tuple[pd.DatetimeIndex, np.ndarray]] = {}
_PATH_CUTOFF_CACHE: dict[tuple[int, int, str], tuple[dict, dict]] = {}


def as_utc_day(value: Any) -> pd.Timestamp:
    """Convert any date-like value to a normalized UTC timestamp."""
    ts = pd.Timestamp(value)
    if ts.tz is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.normalize()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).lower()).strip("_")


def ib_cost(shares: int, price: float, is_sell: bool) -> float:
    """IB-style commission + SEC sales fee + 5 bp slippage."""
    if shares <= 0 or price <= 0:
        return 0.0
    trade_value = float(shares) * float(price)
    commission = max(0.35, min(shares * 0.0035, trade_value * 0.01))
    sec = trade_value * 0.0000278 if is_sell else 0.0
    return float(commission + sec + trade_value * 0.0005)


def _close_on(prices: dict, symbol: str, date: Any) -> float | None:
    """Most recent daily close at or before date, cached by price object."""
    key = (id(prices), symbol)
    cached = _CLOSE_CACHE.get(key)
    if cached is None:
        bars = prices.get(symbol, [])
        if not bars:
            return None
        dates = pd.DatetimeIndex([as_utc_day(t) for t, *_ in bars])
        values = np.asarray([float(close) for _t, _h, _l, close in bars], dtype=float)
        cached = (dates, values)
        _CLOSE_CACHE[key] = cached

    dates, values = cached
    loc = int(dates.searchsorted(as_utc_day(date), side="right") - 1)
    if loc < 0:
        return None
    return float(values[loc])


def _calendar_dates(prices: dict, benchmark: str, start: Any, end: Any) -> list[pd.Timestamp]:
    start_day = as_utc_day(start)
    end_day = as_utc_day(end)
    return [
        as_utc_day(ts)
        for ts, *_ in prices.get(benchmark, [])
        if start_day <= as_utc_day(ts) <= end_day
    ]


def _affordable_buy_qty(cash: float, price: float) -> int:
    """Largest integer buy quantity including modeled buy-side execution cost."""
    if cash <= 0 or price <= 0:
        return 0
    qty = int(cash / price)
    while qty > 0 and qty * price + ib_cost(qty, price, False) > cash + 1e-9:
        qty -= 1
    return qty


def truncate_paths(prices: dict, probs: dict, end_date: Any | None) -> tuple[dict, dict]:
    """Return paths truncated at end_date so CEM cannot inspect future paths."""
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
    _PATH_CUTOFF_CACHE[key] = (truncated_prices, truncated_probs)
    return truncated_prices, truncated_probs


# ── Policy and RF helpers ────────────────────────────────────────────────────


def port_policy_from_vec(vec: np.ndarray) -> dict[str, Any]:
    """Overlay optimized dimensions onto the complete DEFAULT_POLICY."""
    policy: dict[str, Any] = dict(PORT_DEFAULT)
    for i, name in enumerate(PORTFOLIO_BOUNDS):
        lo, hi = PORTFOLIO_BOUNDS[name]
        policy[name] = float(np.clip(float(vec[i]), lo, hi))

    policy["hold_days"] = int(round(float(policy["hold_days"])))
    policy["max_concurrent"] = int(round(float(policy["max_concurrent"])))
    if float(policy["enter_strong"]) < float(policy["enter_floor"]):
        policy["enter_strong"] = float(policy["enter_floor"])
    return policy


def build_rf(seed: int = 42) -> SkPipeline:
    return SkPipeline(
        [
            ("imp", SimpleImputer(strategy="median")),
            (
                "rf",
                RandomForestRegressor(
                    n_estimators=RF_N_ESTIMATORS,
                    max_depth=RF_MAX_DEPTH,
                    min_samples_leaf=RF_MIN_SAMPLES_LEAF,
                    random_state=seed,
                    n_jobs=-1,
                ),
            ),
        ]
    )


def resolve_target_column(df: pd.DataFrame, benchmark: str) -> tuple[str, bool]:
    """Prefer benchmark-specific excess-return labels where available."""
    for column in BENCHMARK_EXCESS_TARGET_CANDIDATES.get(benchmark, ()):
        if column in df.columns:
            return column, True
    if TARGET not in df.columns:
        raise ValueError(f"Configured pipeline TARGET={TARGET!r} is absent from candidates parquet.")
    return TARGET, False


def infer_return_unit(values: pd.Series) -> str:
    """Infer target storage scale; explicit TARGET_RETURN_UNIT overrides this."""
    if TARGET_RETURN_UNIT in {"decimal", "percent"}:
        return TARGET_RETURN_UNIT
    clean = pd.to_numeric(values, errors="coerce").dropna().abs()
    if clean.empty:
        raise ValueError("Cannot infer return unit from an empty RF target.")
    # A 95th percentile above 1.0 is normally a percentage-point encoding.
    return "percent" if float(clean.quantile(0.95)) > 1.0 else "decimal"


def prediction_to_decimal(prediction: float, unit: str) -> float:
    value = float(prediction)
    return value / 100.0 if unit == "percent" else value


def kelly_size(completed_history: list[dict], base_size: float) -> float:
    """Half-Kelly based only on fully net, realised trade returns."""
    if len(completed_history) < KELLY_MIN_N:
        return float(base_size)

    recent = completed_history[-KELLY_LOOKBACK_N:]
    returns = [float(t.get("pnl_pct", np.nan)) for t in recent]
    returns = [value for value in returns if np.isfinite(value)]
    wins = [value for value in returns if value > 0]
    losses = [value for value in returns if value <= 0]
    if not wins or not losses:
        return float(base_size)

    p_win = len(wins) / len(returns)
    payoff_ratio = abs(float(np.mean(wins))) / abs(float(np.mean(losses)))
    if payoff_ratio <= 0 or not np.isfinite(payoff_ratio):
        return float(base_size)

    full_kelly = (p_win * payoff_ratio - (1.0 - p_win)) / payoff_ratio
    if full_kelly <= 0:
        return 0.0
    return float(np.clip(full_kelly / 2.0, KELLY_MIN_SZ, KELLY_MAX_SZ))


# ── Causal, purged OOF RF predictions ────────────────────────────────────────


def known_label_mask(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.Series:
    """Rows whose target was fully observable before the decision timestamp."""
    return (
        (df["t_theta"] < as_of)
        & (df["label_available_ts"] < as_of - pd.Timedelta(days=PURGE_DAYS))
    )


def causal_purged_oof_predictions(
    frame: pd.DataFrame,
    *,
    target_col: str,
    as_of: pd.Timestamp,
    seed: int,
) -> pd.Series:
    """
    Generate chronological OOF predictions without later rows training earlier rows.

    Each validation block is predicted by a model trained only on rows whose
    labels had become available before that block began, less the purge gap.
    Initial warm-up rows intentionally remain NaN and are excluded from CEM.
    """
    predictions = pd.Series(np.nan, index=frame.index, dtype=float)
    eligible = frame.loc[
        known_label_mask(frame, as_of) & pd.to_numeric(frame[target_col], errors="coerce").notna()
    ].copy()
    if eligible.empty:
        return predictions

    eligible = eligible.sort_values("t_theta")
    eligible["_entry_day"] = eligible["t_theta"].map(as_utc_day)
    entry_days = list(pd.Index(eligible["_entry_day"].unique()).sort_values())
    if len(entry_days) < 2:
        return predictions

    first_validation_idx: int | None = None
    for i, day in enumerate(entry_days):
        train_mask = eligible["label_available_ts"] < day - pd.Timedelta(days=PURGE_DAYS)
        if int(train_mask.sum()) >= CAUSAL_OOF_MIN_TRAIN_ROWS:
            first_validation_idx = i
            break
    if first_validation_idx is None:
        return predictions

    validation_days = entry_days[first_validation_idx:]
    if not validation_days:
        return predictions
    blocks = [block for block in np.array_split(np.asarray(validation_days, dtype=object), min(CAUSAL_OOF_FOLDS, len(validation_days))) if len(block)]

    for fold_idx, block in enumerate(blocks):
        block_start = as_utc_day(block[0])
        train_rows = eligible.loc[
            eligible["label_available_ts"] < block_start - pd.Timedelta(days=PURGE_DAYS)
        ].copy()
        validation_rows = eligible.loc[eligible["_entry_day"].isin(list(block))].copy()
        if len(train_rows) < CAUSAL_OOF_MIN_TRAIN_ROWS or validation_rows.empty:
            continue

        model = build_rf(seed + fold_idx)
        model.fit(train_rows[NUM_FEATURES_LEAN], train_rows[target_col])
        predictions.loc[validation_rows.index] = model.predict(validation_rows[NUM_FEATURES_LEAN])

    return predictions


def fit_predict_as_of(
    train_pool: pd.DataFrame,
    eval_df: pd.DataFrame,
    *,
    target_col: str,
    as_of: pd.Timestamp,
    seed: int,
) -> tuple[pd.Series, int]:
    """Fit one final RF using only labels available before as_of, then predict eval_df."""
    train_rows = train_pool.loc[
        known_label_mask(train_pool, as_of)
        & pd.to_numeric(train_pool[target_col], errors="coerce").notna()
    ].copy()
    if len(train_rows) < CAUSAL_OOF_MIN_TRAIN_ROWS:
        raise ValueError(
            f"Only {len(train_rows)} causally available labels before {as_of.date()}; "
            f"need at least {CAUSAL_OOF_MIN_TRAIN_ROWS}."
        )
    if eval_df.empty:
        return pd.Series(dtype=float, index=eval_df.index), len(train_rows)

    model = build_rf(seed)
    model.fit(train_rows[NUM_FEATURES_LEAN], train_rows[target_col])
    return pd.Series(model.predict(eval_df[NUM_FEATURES_LEAN]), index=eval_df.index, dtype=float), len(train_rows)


def make_filtered_candidates(
    frame: pd.DataFrame,
    predictions: pd.Series,
    *,
    target_unit: str,
) -> pd.DataFrame:
    """Attach raw + decimal RF predictions and retain positive predicted edge candidates."""
    out = frame.copy()
    out["pred"] = predictions.reindex(out.index)
    out = out[np.isfinite(out["pred"].astype(float))].copy()
    out["pred_decimal"] = out["pred"].astype(float).map(lambda value: prediction_to_decimal(value, target_unit))
    return out[out["pred_decimal"] > 0.0].copy()


# ── Train-only CEM windows ───────────────────────────────────────────────────


def create_train_windows(train_df: pd.DataFrame, train_eval_end: pd.Timestamp) -> list[dict[str, Any]]:
    """Create rolling internal CEM evaluation windows entirely before train_eval_end."""
    if train_df.empty:
        return []

    ts = train_df["t_theta"].map(as_utc_day)
    start = as_utc_day(ts.min())
    latest = min(as_utc_day(ts.max()), as_utc_day(train_eval_end))
    buffer = pd.Timedelta(days=SETTLEMENT_BUFFER_DAYS)
    windows: list[dict[str, Any]] = []
    cur = start

    while cur < latest:
        # The last window may be shorter than WF_EVAL_MON, but it must never
        # extend past train_eval_end; otherwise its CEM score could see OOS data.
        end = min(as_utc_day(cur + pd.DateOffset(months=WF_EVAL_MON)), latest)
        if end - cur <= 2 * buffer:
            break
        mask = (ts >= cur + buffer) & (ts < end - buffer)
        window_df = train_df.loc[mask].copy()
        if len(window_df) >= WF_MIN_CANDS:
            windows.append({"start": cur, "end": end, "df": window_df})
        cur = as_utc_day(cur + pd.DateOffset(months=WF_STEP_MON))

    if len(windows) >= 2:
        return windows

    return [{"start": start, "end": as_utc_day(train_eval_end), "df": train_df.copy()}]


# ── Core portfolio simulation ────────────────────────────────────────────────


def sim_opp_cost(
    df: pd.DataFrame,
    prices: dict,
    probs: dict,
    policy: dict[str, Any] | None,
    *,
    dynamic_policies: dict[str, dict[str, Any]] | None = None,
    bench_sym: str = "SPY",
    initial: float = INITIAL_CAPITAL,
    use_kelly: bool = False,
    use_hurdle: bool = False,
    start_date: Any | None = None,
    end_date: Any | None = None,
    initial_kelly_history: list[dict] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    """
    Simulate a benchmark-rotation portfolio.

    policy is the static policy. With dynamic_policies, each candidate uses its
    own frozen WFO policy at entry for entry/exit behavior, sizing, and
    max-concurrency. ``end_date`` truncates inputs before ``simulate_one`` is
    invoked, which prevents CEM scoring from reading later prices/probabilities.
    """
    default_policy = dict(policy or PORT_DEFAULT)

    empty_stats: dict[str, Any] = {
        "initial": float(initial), "final": float(initial), "total_return": 0.0,
        "benchmark_return": 0.0, "excess_return": 0.0, "max_dd": 0.0,
        "n_trades": 0, "win_rate": 0.0, "avg_pnl": 0.0, "avg_gross_pnl": 0.0,
        "gross_trade_pnl": 0.0, "net_trade_pnl": 0.0, "total_txn_cost": 0.0,
        "trade_txn_cost": 0.0, "friction_rejects": 0, "avg_position_size": 0.0,
        "median_position_size": 0.0, "min_position_size": 0.0,
        "max_position_size": 0.0, "start_date": None, "end_date": None,
        "n_equity_days": 0,
    }
    if df.empty:
        return pd.DataFrame(), pd.DataFrame(), empty_stats

    sim_prices, sim_probs = truncate_paths(prices, probs, end_date)

    all_trades: list[dict[str, Any]] = []
    for _, row in df.sort_values("t_theta").iterrows():
        candidate_id = str(row["_candidate_id"])
        active_policy = dict((dynamic_policies or {}).get(candidate_id, default_policy))
        trade_raw = simulate_one(row, sim_prices, sim_probs, active_policy)
        if trade_raw is None:
            continue
        trade = dict(trade_raw)
        trade["_candidate_id"] = candidate_id
        trade["_entry_ts"] = as_utc_day(trade["entry_date"])
        trade["_exit_ts"] = as_utc_day(trade["exit_date"])
        trade["_active_policy"] = active_policy
        trade["pred"] = float(row.get("pred", np.nan))
        trade["pred_decimal"] = float(row.get("pred_decimal", np.nan))
        all_trades.append(trade)
    all_trades.sort(key=lambda row: row["_entry_ts"])

    candidate_start = as_utc_day(df["t_theta"].min())
    candidate_end = as_utc_day(df["t_theta"].max())
    eval_start = as_utc_day(start_date) if start_date is not None else min(
        (trade["_entry_ts"] for trade in all_trades), default=candidate_start
    )
    eval_end = as_utc_day(end_date) if end_date is not None else max(
        (trade["_exit_ts"] for trade in all_trades), default=candidate_end
    )

    calendar = _calendar_dates(sim_prices, bench_sym, eval_start, eval_end)
    if not calendar:
        raise ValueError(
            f"No {bench_sym} bars overlap simulation range {eval_start.date()} to {eval_end.date()}."
        )

    first_day, last_day = calendar[0], calendar[-1]
    first_benchmark_close = _close_on(sim_prices, bench_sym, first_day)
    last_benchmark_close = _close_on(sim_prices, bench_sym, last_day)
    if first_benchmark_close is None or last_benchmark_close is None:
        raise ValueError(f"Cannot price benchmark {bench_sym} for simulation.")

    # Both strategy and passive benchmark start with the same executable initial
    # benchmark purchase, making date-matched benchmark results comparable.
    initial_benchmark_shares = _affordable_buy_qty(initial, first_benchmark_close)
    initial_benchmark_cost = ib_cost(initial_benchmark_shares, first_benchmark_close, False)
    initial_cash = initial - initial_benchmark_shares * first_benchmark_close - initial_benchmark_cost

    benchmark_shares = initial_benchmark_shares
    cash = float(initial_cash)
    total_txn_cost = float(initial_benchmark_cost)

    open_positions: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []
    kelly_history: list[dict[str, Any]] = [dict(item) for item in (initial_kelly_history or [])]
    equity_rows: list[dict[str, Any]] = []
    trade_idx = 0
    friction_rejects = 0

    def close_position(position: dict[str, Any], close_day: pd.Timestamp, exit_price: float, exit_reason: str) -> None:
        nonlocal cash, benchmark_shares, total_txn_cost

        qty = int(position["_qty"])
        entry_price = float(position["entry_price"])
        asset_sell_cost = ib_cost(qty, exit_price, True)
        asset_sale_proceeds = qty * exit_price - asset_sell_cost

        benchmark_close = float(_close_on(sim_prices, bench_sym, close_day) or 0.0)
        rebuy_qty = _affordable_buy_qty(asset_sale_proceeds, benchmark_close)
        benchmark_rebuy_cost = ib_cost(rebuy_qty, benchmark_close, False)

        cash += asset_sale_proceeds - rebuy_qty * benchmark_close - benchmark_rebuy_cost
        benchmark_shares += rebuy_qty
        total_txn_cost += asset_sell_cost + benchmark_rebuy_cost

        direct_cost = (
            float(position["_benchmark_sell_cost"])
            + float(position["_asset_buy_cost"])
            + asset_sell_cost
            + benchmark_rebuy_cost
        )
        gross_pnl = qty * (exit_price - entry_price)
        net_pnl = gross_pnl - direct_cost
        entry_notional = max(float(position["_asset_entry_notional"]), 1e-12)

        position["exit_price"] = round(float(exit_price), 6)
        position["exit_date"] = str(close_day.date())
        position["realized_exit_reason"] = exit_reason
        position["gross_pnl"] = round(gross_pnl, 2)
        position["pnl"] = round(net_pnl, 2)
        position["pnl_pct"] = round(net_pnl / entry_notional * 100.0, 6)
        position["txn_cost"] = round(direct_cost, 2)
        position["exit_value"] = round(qty * exit_price, 2)
        position["benchmark_rebuy_qty"] = int(rebuy_qty)
        position["active_position_size_pct"] = round(float(position["_position_size_pct"]) * 100.0, 6)
        position["active_max_concurrent"] = int(position["_max_concurrent"])
        position.pop("_active_policy", None)

        completed.append(position)
        kelly_history.append(position)

    for day in calendar:
        benchmark_close = _close_on(sim_prices, bench_sym, day)
        if benchmark_close is None:
            continue
        benchmark_close = float(benchmark_close)

        # Exit existing positions before entries. Their original entry policy
        # governs exit behavior because simulate_one produced the exit path.
        still_open: list[dict[str, Any]] = []
        for position in open_positions:
            if position["_exit_ts"] <= day:
                close_position(
                    position,
                    close_day=day,
                    exit_price=float(position["exit_price"]),
                    exit_reason=str(position.get("exit_reason", "strategy_exit")),
                )
            else:
                still_open.append(position)
        open_positions = still_open

        # Enter candidates on their configured entry day.
        while trade_idx < len(all_trades):
            trade = all_trades[trade_idx]
            if trade["_entry_ts"] > day:
                break
            trade_idx += 1
            if trade["_entry_ts"] < day:
                continue

            active_policy = dict(trade["_active_policy"])
            max_concurrent = int(active_policy.get("max_concurrent", PORT_DEFAULT["max_concurrent"]))
            if len(open_positions) >= max_concurrent:
                continue
            if any(position["symbol"] == trade["symbol"] for position in open_positions):
                continue

            base_position_size = float(active_policy.get("position_size_pct", PORT_DEFAULT["position_size_pct"]))
            position_size = kelly_size(kelly_history, base_position_size) if use_kelly else base_position_size
            if position_size <= 0.0:
                continue

            open_value = sum(
                int(position["_qty"]) * float(_close_on(sim_prices, position["symbol"], day) or position["entry_price"])
                for position in open_positions
            )
            current_equity = benchmark_shares * benchmark_close + open_value + cash
            desired_allocation = current_equity * position_size
            entry_price = float(trade["entry_price"])
            if entry_price <= 0.0 or desired_allocation < entry_price:
                continue

            benchmark_sell_qty = min(int(desired_allocation / benchmark_close), benchmark_shares)
            if benchmark_sell_qty < 1:
                continue
            benchmark_sell_cost = ib_cost(benchmark_sell_qty, benchmark_close, True)
            available_for_asset = benchmark_sell_qty * benchmark_close - benchmark_sell_cost

            asset_qty = _affordable_buy_qty(available_for_asset, entry_price)
            if asset_qty < 1:
                continue
            asset_buy_cost = ib_cost(asset_qty, entry_price, False)
            asset_cash_needed = asset_qty * entry_price + asset_buy_cost
            if asset_cash_needed > available_for_asset + 1e-9:
                continue

            # T1 is deliberately an ex-ante entry rule. It has no realised
            # reward penalty elsewhere, so the ablation isolates this behavior.
            estimated_round_trip_cost = np.nan
            expected_edge_dollars = np.nan
            if use_hurdle:
                pred_decimal = float(trade.get("pred_decimal", np.nan))
                if not np.isfinite(pred_decimal) or pred_decimal <= 0.0:
                    friction_rejects += 1
                    continue

                estimated_asset_sell_cost = ib_cost(asset_qty, entry_price, True)
                estimated_asset_sale = asset_qty * entry_price - estimated_asset_sell_cost
                estimated_rebuy_qty = _affordable_buy_qty(estimated_asset_sale, benchmark_close)
                estimated_rebuy_cost = ib_cost(estimated_rebuy_qty, benchmark_close, False)
                estimated_round_trip_cost = (
                    benchmark_sell_cost
                    + asset_buy_cost
                    + estimated_asset_sell_cost
                    + estimated_rebuy_cost
                )
                expected_edge_dollars = asset_qty * entry_price * pred_decimal
                if expected_edge_dollars < HURDLE_MULT * estimated_round_trip_cost:
                    friction_rejects += 1
                    continue

            benchmark_shares -= benchmark_sell_qty
            cash += available_for_asset - asset_cash_needed
            total_txn_cost += benchmark_sell_cost + asset_buy_cost

            open_positions.append(
                {
                    **trade,
                    "_qty": int(asset_qty),
                    "_position_size_pct": float(position_size),
                    "_max_concurrent": int(max_concurrent),
                    "_asset_entry_notional": round(asset_qty * entry_price, 2),
                    "_benchmark_sell_cost": round(benchmark_sell_cost, 2),
                    "_asset_buy_cost": round(asset_buy_cost, 2),
                    "estimated_round_trip_cost": round(float(estimated_round_trip_cost), 4)
                    if np.isfinite(estimated_round_trip_cost) else np.nan,
                    "expected_edge_dollars": round(float(expected_edge_dollars), 4)
                    if np.isfinite(expected_edge_dollars) else np.nan,
                }
            )

        marked_open_value = sum(
            int(position["_qty"]) * float(_close_on(sim_prices, position["symbol"], day) or position["entry_price"])
            for position in open_positions
        )
        equity = benchmark_shares * benchmark_close + marked_open_value + cash
        passive_benchmark_equity = initial_benchmark_shares * benchmark_close + initial_cash
        equity_rows.append(
            {
                "date": str(day.date()),
                "equity": round(float(equity), 2),
                "benchmark_equity": round(float(passive_benchmark_equity), 2),
                "cash": round(float(cash), 2),
                "benchmark_shares": int(benchmark_shares),
                "open_positions": int(len(open_positions)),
            }
        )

    # CEM train windows and pre-terminal validation are force-liquidated at the
    # known evaluation boundary. That avoids using prices/probabilities after
    # the boundary while keeping equity and drawdown internally consistent.
    for position in list(open_positions):
        forced_exit = _close_on(sim_prices, position["symbol"], last_day)
        close_position(
            position,
            close_day=last_day,
            exit_price=float(forced_exit if forced_exit is not None else position["entry_price"]),
            exit_reason="evaluation_end_liquidation",
        )
    open_positions = []

    final_equity = benchmark_shares * float(last_benchmark_close) + cash
    final_passive_equity = initial_benchmark_shares * float(last_benchmark_close) + initial_cash

    if equity_rows:
        equity_rows[-1].update(
            {
                "equity": round(float(final_equity), 2),
                "benchmark_equity": round(float(final_passive_equity), 2),
                "cash": round(float(cash), 2),
                "benchmark_shares": int(benchmark_shares),
                "open_positions": 0,
            }
        )
    else:
        equity_rows.append(
            {
                "date": str(last_day.date()),
                "equity": round(float(final_equity), 2),
                "benchmark_equity": round(float(final_passive_equity), 2),
                "cash": round(float(cash), 2),
                "benchmark_shares": int(benchmark_shares),
                "open_positions": 0,
            }
        )

    trade_df = pd.DataFrame(completed)
    equity_df = pd.DataFrame(equity_rows)

    equity_values = equity_df["equity"].astype(float).to_numpy()
    peaks = np.maximum.accumulate(equity_values) if len(equity_values) else np.asarray([])
    drawdowns = np.where(peaks > 0, equity_values / peaks - 1.0, 0.0) if len(peaks) else np.asarray([])
    max_dd = float(drawdowns.min() * 100.0) if len(drawdowns) else 0.0

    if trade_df.empty:
        gross_trade_pnl = net_trade_pnl = trade_txn_cost = 0.0
        win_rate = avg_pnl = avg_gross_pnl = 0.0
        position_sizes = np.asarray([], dtype=float)
    else:
        gross_trade_pnl = float(trade_df["gross_pnl"].sum())
        net_trade_pnl = float(trade_df["pnl"].sum())
        trade_txn_cost = float(trade_df["txn_cost"].sum())
        win_rate = float((trade_df["pnl"] > 0.0).mean() * 100.0)
        avg_pnl = float(trade_df["pnl"].mean())
        avg_gross_pnl = float(trade_df["gross_pnl"].mean())
        position_sizes = trade_df["_position_size_pct"].astype(float).to_numpy()

    stats = {
        "initial": round(float(initial), 2),
        "final": round(float(final_equity), 2),
        "total_return": round((final_equity / initial - 1.0) * 100.0, 6),
        "benchmark_return": round((final_passive_equity / initial - 1.0) * 100.0, 6),
        "excess_return": round((final_equity - final_passive_equity) / initial * 100.0, 6),
        "max_dd": round(max_dd, 6),
        "n_trades": int(len(trade_df)),
        "win_rate": round(win_rate, 6),
        "avg_pnl": round(avg_pnl, 6),
        "avg_gross_pnl": round(avg_gross_pnl, 6),
        "gross_trade_pnl": round(gross_trade_pnl, 2),
        "net_trade_pnl": round(net_trade_pnl, 2),
        "total_txn_cost": round(float(total_txn_cost), 2),
        "trade_txn_cost": round(trade_txn_cost, 2),
        "friction_rejects": int(friction_rejects),
        "avg_position_size": round(float(position_sizes.mean() * 100.0), 6) if len(position_sizes) else 0.0,
        "median_position_size": round(float(np.median(position_sizes) * 100.0), 6) if len(position_sizes) else 0.0,
        "min_position_size": round(float(position_sizes.min() * 100.0), 6) if len(position_sizes) else 0.0,
        "max_position_size": round(float(position_sizes.max() * 100.0), 6) if len(position_sizes) else 0.0,
        "start_date": str(first_day.date()),
        "end_date": str(last_day.date()),
        "n_equity_days": int(len(equity_df)),
    }
    return trade_df, equity_df, stats


# ── CEM objective and optimizer ──────────────────────────────────────────────


def daily_equity_sharpe(equity_df: pd.DataFrame) -> float | None:
    """Annualized portfolio Sharpe from daily equity returns."""
    if equity_df.empty or len(equity_df) < MIN_DAILY_RETURNS_FOR_SHARPE + 1:
        return None
    daily_returns = equity_df["equity"].astype(float).pct_change().dropna()
    if len(daily_returns) < MIN_DAILY_RETURNS_FOR_SHARPE:
        return None
    std = float(daily_returns.std(ddof=1))
    if not np.isfinite(std) or std <= 1e-12:
        return None
    sharpe = float(daily_returns.mean() / std * math.sqrt(252.0))
    return sharpe if np.isfinite(sharpe) else None


def cem_reward(trades: pd.DataFrame, equity: pd.DataFrame, stats: dict[str, Any]) -> float:
    """Daily-equity objective. T1 does not add a second realised penalty here."""
    if len(trades) < MIN_TRADES_FOR_REWARD:
        return INVALID_SCORE
    sharpe = daily_equity_sharpe(equity)
    if sharpe is None:
        return INVALID_SCORE
    return float(sharpe - DD_PENALTY * abs(float(stats["max_dd"])))


def cem_search(
    train_candidates: pd.DataFrame,
    prices: dict,
    probs: dict,
    *,
    bench_sym: str,
    use_hurdle: bool,
    use_train_windows: bool,
    use_kelly: bool,
    train_eval_end: pd.Timestamp,
    seed: int,
    n_iter: int = CEM_ITERS,
    pop: int = CEM_POP,
) -> tuple[dict[str, Any], float, list[dict[str, Any]]]:
    """CEM search using train-only, time-truncated portfolio simulations."""
    if len(train_candidates) < MIN_CEM_RF_CANDIDATES:
        raise ValueError(
            f"CEM received only {len(train_candidates)} RF-filtered candidates; "
            f"need at least {MIN_CEM_RF_CANDIDATES}."
        )

    rng = np.random.default_rng(seed)
    names = list(PORTFOLIO_BOUNDS)
    dim = len(names)
    elite_count = max(2, int(pop * CEM_ELITE_FRAC))
    mean = np.asarray([float(PORT_DEFAULT[name]) for name in names], dtype=float)
    std = np.asarray(
        [(PORTFOLIO_BOUNDS[name][1] - PORTFOLIO_BOUNDS[name][0]) / 4.0 for name in names],
        dtype=float,
    )

    if use_train_windows:
        windows = create_train_windows(train_candidates, train_eval_end)
        tag = f"[TrainWF:{len(windows)}w]"
    else:
        start = as_utc_day(train_candidates["t_theta"].min())
        windows = [{"start": start, "end": as_utc_day(train_eval_end), "df": train_candidates.copy()}]
        tag = ""
    if use_hurdle:
        tag += f"[H={HURDLE_MULT:.0f}x]"
    if use_kelly:
        tag += "[K]"

    best_policy: dict[str, Any] | None = None
    best_score = INVALID_SCORE

    for iteration in range(n_iter):
        samples = rng.normal(mean, std, size=(pop, dim))
        policies = [port_policy_from_vec(sample) for sample in samples]
        scores: list[float] = []

        for candidate_policy in policies:
            window_scores: list[float] = []
            for window in windows:
                trades, equity, stats = sim_opp_cost(
                    window["df"],
                    prices,
                    probs,
                    candidate_policy,
                    bench_sym=bench_sym,
                    initial=INITIAL_CAPITAL,
                    use_kelly=use_kelly,
                    use_hurdle=use_hurdle,
                    start_date=window["start"],
                    end_date=window["end"],
                )
                reward = cem_reward(trades, equity, stats)
                if reward > INVALID_SCORE / 2:
                    window_scores.append(reward)

            scores.append(float(np.mean(window_scores)) if window_scores else INVALID_SCORE)

        scores_arr = np.asarray(scores, dtype=float)
        elite_idx = np.argsort(scores_arr)[-elite_count:]
        elite_samples = samples[elite_idx]
        mean = elite_samples.mean(axis=0)
        std = elite_samples.std(axis=0) + 1e-4

        iter_best_idx = int(np.argmax(scores_arr))
        iter_best_score = float(scores_arr[iter_best_idx])
        if iter_best_score > best_score:
            best_score = iter_best_score
            best_policy = policies[iter_best_idx]

        print(
            f"      {bench_sym}|CEM{tag} iter {iteration + 1}/{n_iter} "
            f"best={iter_best_score:+.3f} global={best_score:+.3f}",
            flush=True,
        )

    if best_policy is None or best_score <= INVALID_SCORE / 2:
        raise RuntimeError(
            "Every CEM policy received an invalid score. Lower MIN_TRADES_FOR_REWARD, "
            "increase the train horizon, or inspect whether simulate_one creates trades."
        )
    return best_policy, best_score, windows


# ── Data loading and partitioning ────────────────────────────────────────────


async def load_paths(df: pd.DataFrame) -> tuple[dict, dict]:
    connection = await connect()
    try:
        symbols = sorted(set(df["symbol"].astype(str).unique()) | {"SPY", "QQQ"})
        market_ids = sorted(df["market_id"].astype(str).unique())
        bars = await connection.fetch(
            f"SELECT symbol, ts, high, low, close "
            f"FROM {SCHEMA}.historical_price_bars "
            f"WHERE resolution='1d' AND symbol=ANY($1::text[]) "
            f"ORDER BY symbol, ts",
            symbols,
        )
        probability_rows = await connection.fetch(
            f"""SELECT DISTINCT ON (market_id, (hour_ts AT TIME ZONE 'UTC')::date)
                market_id,
                (hour_ts AT TIME ZONE 'UTC')::date AS d,
                probability
            FROM {SCHEMA}.historical_probability_points
            WHERE market_id=ANY($1::text[])
              AND EXTRACT(HOUR FROM hour_ts AT TIME ZONE 'UTC') <= 20
            ORDER BY market_id, (hour_ts AT TIME ZONE 'UTC')::date, hour_ts DESC""",
            market_ids,
        )
    finally:
        await connection.close()

    prices: dict[str, list[tuple[pd.Timestamp, float, float, float]]] = {}
    for bar in bars:
        prices.setdefault(str(bar["symbol"]), []).append(
            (
                as_utc_day(bar["ts"]),
                float(bar["high"]),
                float(bar["low"]),
                float(bar["close"]),
            )
        )

    probs: dict[str, list[tuple[pd.Timestamp, float]]] = {}
    for row in probability_rows:
        probs.setdefault(str(row["market_id"]), []).append(
            (as_utc_day(row["d"]), float(row["probability"]))
        )

    for collection in (prices, probs):
        for key in collection:
            collection[key].sort(key=lambda value: value[0])
    return prices, probs


def partition_data(df: pd.DataFrame) -> dict[str, Any]:
    """Create dev, pre-terminal validation, and never-touched terminal slices."""
    split = df["split"].astype(str).str.lower()
    development = df.loc[split == "train"].copy()
    original_test = df.loc[split == "test"].copy()
    if development.empty or original_test.empty:
        raise ValueError("candidates.parquet must contain non-empty split=='train' and split=='test' rows.")

    original_test = original_test.sort_values("t_theta").copy()
    test_times = original_test["t_theta"].map(as_utc_day)
    proposed_start = as_utc_day(test_times.max() - pd.DateOffset(months=FINAL_HOLDOUT_MONTHS))

    preterminal = original_test.loc[test_times < proposed_start].copy()
    terminal = original_test.loc[test_times >= proposed_start].copy()

    # Time duration is preferred. If it creates an undersized block, use the
    # final chronological fraction rather than silently mixing terminal rows.
    if len(preterminal) < MIN_PRETERMINAL_TEST_CANDIDATES or len(terminal) < MIN_TERMINAL_TEST_CANDIDATES:
        cut_idx = int(math.floor(len(original_test) * (1.0 - FINAL_HOLDOUT_FRACTION_FALLBACK)))
        cut_idx = min(max(cut_idx, MIN_PRETERMINAL_TEST_CANDIDATES), len(original_test) - MIN_TERMINAL_TEST_CANDIDATES)
        if cut_idx <= 0 or cut_idx >= len(original_test):
            raise ValueError(
                "Original test split is too small to form both a pre-terminal validation segment and a terminal holdout. "
                "Reduce MIN_*_TEST_CANDIDATES or collect more test candidates."
            )
        proposed_start = as_utc_day(original_test.iloc[cut_idx]["t_theta"])
        preterminal = original_test.iloc[:cut_idx].copy()
        terminal = original_test.iloc[cut_idx:].copy()

    if len(preterminal) < MIN_PRETERMINAL_TEST_CANDIDATES or len(terminal) < MIN_TERMINAL_TEST_CANDIDATES:
        raise ValueError(
            f"Insufficient test candidates after partition: pre-terminal={len(preterminal)}, terminal={len(terminal)}."
        )

    static_start = as_utc_day(preterminal["t_theta"].min())
    terminal_start = as_utc_day(terminal["t_theta"].min())
    static_end = terminal_start - pd.Timedelta(days=1)

    # Do not open a new trade so late in pre-terminal validation that its OOS
    # result requires terminal prices. Open positions are also force-liquidated
    # at static_end as a separate safety measure.
    static_eval = preterminal.loc[
        preterminal["t_theta"] < static_end - pd.Timedelta(days=SETTLEMENT_BUFFER_DAYS)
    ].copy()
    if static_eval.empty:
        raise ValueError("No pre-terminal candidates remain after the settlement buffer.")

    return {
        "development": development,
        "preterminal": preterminal,
        "static_eval": static_eval,
        "terminal": terminal,
        "static_start": static_start,
        "static_end": static_end,
        "terminal_start": terminal_start,
    }


def build_online_windows(preterminal: pd.DataFrame, static_start: pd.Timestamp, static_end: pd.Timestamp) -> list[dict[str, Any]]:
    """Non-overlapping pre-terminal windows for expanding online WFO."""
    windows: list[dict[str, Any]] = []
    ts = preterminal["t_theta"].map(as_utc_day)
    cur = as_utc_day(static_start)
    final_day = as_utc_day(static_end)
    settle = pd.Timedelta(days=SETTLEMENT_BUFFER_DAYS)

    while cur < final_day:
        end = min(as_utc_day(cur + pd.DateOffset(months=ONLINE_WFO_EVAL_MON)), final_day)
        mask = (ts >= cur) & (ts < end - settle)
        window_df = preterminal.loc[mask].copy()
        if len(window_df) >= WF_MIN_CANDS:
            windows.append({"start": cur, "end": end, "df": window_df})
        cur = as_utc_day(cur + pd.DateOffset(months=ONLINE_WFO_STEP_MON))

    return windows


# ── Evaluation stages ────────────────────────────────────────────────────────


def prepare_cem_training_candidates(
    train_pool: pd.DataFrame,
    *,
    target_col: str,
    target_unit: str,
    as_of: pd.Timestamp,
    seed: int,
) -> tuple[pd.DataFrame, int, int]:
    """Build causally OOF-filtered candidate universe for CEM."""
    oof = causal_purged_oof_predictions(
        train_pool,
        target_col=target_col,
        as_of=as_of,
        seed=seed,
    )
    filtered = make_filtered_candidates(train_pool, oof, target_unit=target_unit)
    eligible_count = int(known_label_mask(train_pool, as_of).sum())
    return filtered, eligible_count, int(oof.notna().sum())


def evaluate_static_oos(
    *,
    experiment: dict[str, Any],
    benchmark: str,
    seed: int,
    development: pd.DataFrame,
    static_eval: pd.DataFrame,
    static_start: pd.Timestamp,
    static_end: pd.Timestamp,
    target_col: str,
    target_unit: str,
    prices: dict,
    probs: dict,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    """Train on original development only; evaluate a frozen policy OOS."""
    use_hurdle = bool(experiment["hurdle"])
    use_windows = bool(experiment["wf"])
    use_kelly = bool(experiment["kelly"])

    cem_train, known_labels, oof_count = prepare_cem_training_candidates(
        development,
        target_col=target_col,
        target_unit=target_unit,
        as_of=static_start,
        seed=seed,
    )
    policy, objective, windows = cem_search(
        cem_train,
        prices,
        probs,
        bench_sym=benchmark,
        use_hurdle=use_hurdle,
        use_train_windows=use_windows,
        use_kelly=use_kelly,
        train_eval_end=static_start - pd.Timedelta(days=1),
        seed=seed + BENCHMARK_SEED_OFFSET[benchmark],
    )

    eval_predictions, rf_train_rows = fit_predict_as_of(
        development,
        static_eval,
        target_col=target_col,
        as_of=static_start,
        seed=seed,
    )
    oos_candidates = make_filtered_candidates(static_eval, eval_predictions, target_unit=target_unit)

    train_history: list[dict] | None = None
    if use_kelly:
        train_trades, _train_eq, _train_stats = sim_opp_cost(
            cem_train,
            prices,
            probs,
            policy,
            bench_sym=benchmark,
            use_kelly=True,
            use_hurdle=use_hurdle,
            start_date=as_utc_day(cem_train["t_theta"].min()),
            end_date=static_start - pd.Timedelta(days=1),
        )
        if not train_trades.empty:
            train_history = train_trades.loc[
                train_trades["realized_exit_reason"] != "evaluation_end_liquidation"
            ].to_dict("records")

    trades, equity, stats = sim_opp_cost(
        oos_candidates,
        prices,
        probs,
        policy,
        bench_sym=benchmark,
        use_kelly=use_kelly,
        use_hurdle=use_hurdle,
        start_date=static_start,
        end_date=static_end,
        initial_kelly_history=train_history,
    )

    result = stage_result(
        stage="static_oos",
        experiment=experiment,
        benchmark=benchmark,
        seed=seed,
        target_col=target_col,
        target_unit=target_unit,
        stats=stats,
        cem_objective=objective,
        cem_known_labels=known_labels,
        cem_oof_predictions=oof_count,
        cem_candidates=len(cem_train),
        rf_fit_rows=rf_train_rows,
        predicted_eval_candidates=len(oos_candidates),
        n_windows=len(windows),
        policy=policy,
    )
    return result, trades, equity


def evaluate_terminal_holdout(
    *,
    experiment: dict[str, Any],
    benchmark: str,
    seed: int,
    all_preterminal: pd.DataFrame,
    terminal: pd.DataFrame,
    terminal_start: pd.Timestamp,
    target_col: str,
    target_unit: str,
    prices: dict,
    probs: dict,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame]:
    """Final primary result: terminal block was never read by RF/CEM."""
    use_hurdle = bool(experiment["hurdle"])
    use_windows = bool(experiment["wf"])
    use_kelly = bool(experiment["kelly"])

    cem_train, known_labels, oof_count = prepare_cem_training_candidates(
        all_preterminal,
        target_col=target_col,
        target_unit=target_unit,
        as_of=terminal_start,
        seed=seed,
    )
    policy, objective, windows = cem_search(
        cem_train,
        prices,
        probs,
        bench_sym=benchmark,
        use_hurdle=use_hurdle,
        use_train_windows=use_windows,
        use_kelly=use_kelly,
        train_eval_end=terminal_start - pd.Timedelta(days=1),
        seed=seed + BENCHMARK_SEED_OFFSET[benchmark],
    )

    terminal_predictions, rf_train_rows = fit_predict_as_of(
        all_preterminal,
        terminal,
        target_col=target_col,
        as_of=terminal_start,
        seed=seed,
    )
    terminal_candidates = make_filtered_candidates(terminal, terminal_predictions, target_unit=target_unit)

    train_history: list[dict] | None = None
    if use_kelly:
        train_trades, _train_eq, _train_stats = sim_opp_cost(
            cem_train,
            prices,
            probs,
            policy,
            bench_sym=benchmark,
            use_kelly=True,
            use_hurdle=use_hurdle,
            start_date=as_utc_day(cem_train["t_theta"].min()),
            end_date=terminal_start - pd.Timedelta(days=1),
        )
        if not train_trades.empty:
            train_history = train_trades.loc[
                train_trades["realized_exit_reason"] != "evaluation_end_liquidation"
            ].to_dict("records")

    trades, equity, stats = sim_opp_cost(
        terminal_candidates,
        prices,
        probs,
        policy,
        bench_sym=benchmark,
        use_kelly=use_kelly,
        use_hurdle=use_hurdle,
        start_date=terminal_start,
        end_date=None,
        initial_kelly_history=train_history,
    )

    result = stage_result(
        stage="terminal_holdout",
        experiment=experiment,
        benchmark=benchmark,
        seed=seed,
        target_col=target_col,
        target_unit=target_unit,
        stats=stats,
        cem_objective=objective,
        cem_known_labels=known_labels,
        cem_oof_predictions=oof_count,
        cem_candidates=len(cem_train),
        rf_fit_rows=rf_train_rows,
        predicted_eval_candidates=len(terminal_candidates),
        n_windows=len(windows),
        policy=policy,
    )
    return result, trades, equity


def evaluate_online_wfo(
    *,
    experiment: dict[str, Any],
    benchmark: str,
    seed: int,
    all_data: pd.DataFrame,
    preterminal: pd.DataFrame,
    static_start: pd.Timestamp,
    static_end: pd.Timestamp,
    target_col: str,
    target_unit: str,
    prices: dict,
    probs: dict,
) -> tuple[dict[str, Any] | None, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Expanding online simulation; each window fits only on completed prior labels."""
    use_hurdle = bool(experiment["hurdle"])
    use_windows = bool(experiment["wf"])
    use_kelly = bool(experiment["kelly"])

    online_windows = build_online_windows(preterminal, static_start, static_end)
    dynamic_policies: dict[str, dict[str, Any]] = {}
    selected_frames: list[pd.DataFrame] = []
    policy_rows: list[dict[str, Any]] = []

    for window_number, window in enumerate(online_windows, start=1):
        window_start = as_utc_day(window["start"])
        window_end = as_utc_day(window["end"])

        # Strictly prior candidate timestamps AND fully observed labels only.
        train_pool = all_data.loc[
            (all_data["t_theta"] < window_start)
            & (all_data["label_available_ts"] < window_start - pd.Timedelta(days=PURGE_DAYS))
        ].copy()
        if len(train_pool) < ONLINE_WFO_MIN_TRAIN_ROWS:
            continue

        try:
            cem_train, known_labels, oof_count = prepare_cem_training_candidates(
                train_pool,
                target_col=target_col,
                target_unit=target_unit,
                as_of=window_start,
                seed=seed,
            )
            policy, objective, internal_windows = cem_search(
                cem_train,
                prices,
                probs,
                bench_sym=benchmark,
                use_hurdle=use_hurdle,
                use_train_windows=use_windows,
                use_kelly=use_kelly,
                train_eval_end=window_start - pd.Timedelta(days=1),
                seed=seed + BENCHMARK_SEED_OFFSET[benchmark],
            )
            predictions, rf_fit_rows = fit_predict_as_of(
                train_pool,
                window["df"],
                target_col=target_col,
                as_of=window_start,
                seed=seed,
            )
        except (ValueError, RuntimeError) as exc:
            print(f"      WFO window {window_number} skipped: {exc}", flush=True)
            continue

        selected = make_filtered_candidates(window["df"], predictions, target_unit=target_unit)
        for candidate_id in selected["_candidate_id"].astype(str):
            dynamic_policies[candidate_id] = policy
        if not selected.empty:
            selected_frames.append(selected)

        policy_rows.append(
            {
                "window": window_number,
                "start": str(window_start.date()),
                "end": str(window_end.date()),
                "train_pool_rows": len(train_pool),
                "known_labels": known_labels,
                "oof_predictions": oof_count,
                "cem_candidates": len(cem_train),
                "rf_fit_rows": rf_fit_rows,
                "selected_candidates": len(selected),
                "cem_objective": objective,
                "internal_train_windows": len(internal_windows),
                "policy_json": json.dumps(policy, sort_keys=True),
            }
        )

    policy_df = pd.DataFrame(policy_rows)
    if not selected_frames:
        return None, pd.DataFrame(), pd.DataFrame(), policy_df

    eval_df = pd.concat(selected_frames, axis=0).sort_values("t_theta")
    trades, equity, stats = sim_opp_cost(
        eval_df,
        prices,
        probs,
        PORT_DEFAULT,
        dynamic_policies=dynamic_policies,
        bench_sym=benchmark,
        use_kelly=use_kelly,
        use_hurdle=use_hurdle,
        start_date=static_start,
        end_date=static_end,
        # Live online Kelly begins without hindsight seeded historical trades.
        initial_kelly_history=None,
    )

    result = stage_result(
        stage="online_wfo",
        experiment=experiment,
        benchmark=benchmark,
        seed=seed,
        target_col=target_col,
        target_unit=target_unit,
        stats=stats,
        cem_objective=float(policy_df["cem_objective"].mean()) if not policy_df.empty else np.nan,
        cem_known_labels=int(policy_df["known_labels"].sum()) if not policy_df.empty else 0,
        cem_oof_predictions=int(policy_df["oof_predictions"].sum()) if not policy_df.empty else 0,
        cem_candidates=int(policy_df["cem_candidates"].sum()) if not policy_df.empty else 0,
        rf_fit_rows=int(policy_df["rf_fit_rows"].sum()) if not policy_df.empty else 0,
        predicted_eval_candidates=len(eval_df),
        n_windows=len(policy_df),
        policy=None,
    )
    return result, trades, equity, policy_df


# ── Reporting / persistence ──────────────────────────────────────────────────


def stage_result(
    *,
    stage: str,
    experiment: dict[str, Any],
    benchmark: str,
    seed: int,
    target_col: str,
    target_unit: str,
    stats: dict[str, Any],
    cem_objective: float,
    cem_known_labels: int,
    cem_oof_predictions: int,
    cem_candidates: int,
    rf_fit_rows: int,
    predicted_eval_candidates: int,
    n_windows: int,
    policy: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "stage": stage,
        "experiment": experiment["label"],
        "experiment_id": experiment["id"],
        "benchmark": benchmark,
        "outer_seed": seed,
        "hurdle_entry_only": bool(experiment["hurdle"]),
        "train_windows": bool(experiment["wf"]),
        "kelly": bool(experiment["kelly"]),
        "target_col": target_col,
        "target_unit": target_unit,
        "cem_objective": round(float(cem_objective), 8) if np.isfinite(cem_objective) else np.nan,
        "cem_known_labels": cem_known_labels,
        "cem_oof_predictions": cem_oof_predictions,
        "cem_candidates": cem_candidates,
        "rf_fit_rows": rf_fit_rows,
        "predicted_eval_candidates": predicted_eval_candidates,
        "n_windows": n_windows,
        "return_pct": stats["total_return"],
        "benchmark_return_pct": stats["benchmark_return"],
        "excess_return_pct": stats["excess_return"],
        "max_dd_pct": stats["max_dd"],
        "start_date": stats["start_date"],
        "end_date": stats["end_date"],
        "equity_days": stats["n_equity_days"],
        "trades": stats["n_trades"],
        "win_rate_pct": stats["win_rate"],
        "avg_net_pnl": stats["avg_pnl"],
        "avg_gross_pnl": stats["avg_gross_pnl"],
        "gross_trade_pnl": stats["gross_trade_pnl"],
        "net_trade_pnl": stats["net_trade_pnl"],
        "total_txn_cost": stats["total_txn_cost"],
        "trade_txn_cost": stats["trade_txn_cost"],
        "friction_rejects": stats["friction_rejects"],
        "avg_position_size_pct": stats["avg_position_size"],
        "median_position_size_pct": stats["median_position_size"],
        "min_position_size_pct": stats["min_position_size"],
        "max_position_size_pct": stats["max_position_size"],
        "policy_base_position_size_pct": round(float(policy["position_size_pct"]) * 100.0, 6) if policy else np.nan,
        "policy_max_concurrent": int(policy["max_concurrent"]) if policy else np.nan,
        "policy_json": json.dumps(policy, sort_keys=True) if policy else "DYNAMIC_WFO_POLICY_BY_WINDOW",
    }


def save_audit_logs(stage: str, experiment: str, benchmark: str, seed: int, trades: pd.DataFrame, equity: pd.DataFrame) -> None:
    TRADE_LOG_DIR.mkdir(parents=True, exist_ok=True)
    EQUITY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{_slug(stage)}__{_slug(experiment)}__{benchmark.lower()}__seed_{seed}"
    trades.to_csv(TRADE_LOG_DIR / f"{stem}.csv", index=False)
    equity.to_csv(EQUITY_LOG_DIR / f"{stem}.csv", index=False)


def save_policy_log(experiment: str, benchmark: str, seed: int, policy_df: pd.DataFrame) -> None:
    if policy_df.empty:
        return
    POLICY_LOG_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"online_wfo__{_slug(experiment)}__{benchmark.lower()}__seed_{seed}.csv"
    policy_df.to_csv(POLICY_LOG_DIR / stem, index=False)


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def aggregate_results(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    frame = pd.DataFrame(rows)
    metric_cols = [
        "return_pct", "benchmark_return_pct", "excess_return_pct", "max_dd_pct",
        "trades", "win_rate_pct", "avg_net_pnl", "trade_txn_cost",
        "avg_position_size_pct",
    ]
    group_cols = ["stage", "experiment", "experiment_id", "benchmark", "hurdle_entry_only", "train_windows", "kelly", "target_col", "target_unit"]
    aggregates: list[dict[str, Any]] = []
    for keys, group in frame.groupby(group_cols, dropna=False):
        row = dict(zip(group_cols, keys))
        row["n_outer_seeds"] = int(len(group))
        for metric in metric_cols:
            values = pd.to_numeric(group[metric], errors="coerce").dropna()
            row[f"{metric}_mean"] = round(float(values.mean()), 6) if not values.empty else np.nan
            row[f"{metric}_median"] = round(float(values.median()), 6) if not values.empty else np.nan
            row[f"{metric}_std"] = round(float(values.std(ddof=1)), 6) if len(values) > 1 else 0.0 if len(values) == 1 else np.nan
            row[f"{metric}_min"] = round(float(values.min()), 6) if not values.empty else np.nan
            row[f"{metric}_max"] = round(float(values.max()), 6) if not values.empty else np.nan
        aggregates.append(row)
    return aggregates


def print_stage_table(title: str, rows: list[dict[str, Any]]) -> None:
    print(f"\n{'=' * 128}")
    print(f"  {title}")
    print(f"{'=' * 128}")
    if not rows:
        print("  No results.")
        return
    header = (
        f"  {'Experiment':<22} {'Bench':<5} {'Seed':>5} {'Return':>9} {'B&H':>9} {'Excess':>9} "
        f"{'MaxDD':>9} {'Trades':>7} {'Win%':>7} {'Cost$':>10} {'AvgPos':>8}"
    )
    print(header)
    print(f"  {'-' * 126}")
    for row in rows:
        print(
            f"  {row['experiment']:<22} {row['benchmark']:<5} {row['outer_seed']:>5} "
            f"{row['return_pct']:>+8.2f}% {row['benchmark_return_pct']:>+8.2f}% {row['excess_return_pct']:>+8.2f}% "
            f"{row['max_dd_pct']:>+8.2f}% {row['trades']:>6} {row['win_rate_pct']:>6.1f}% "
            f"${row['trade_txn_cost']:>9.0f} {row['avg_position_size_pct']:>7.1f}%",
            flush=True,
        )


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    started = time.time()
    print("=" * 88)
    print("  LEAKAGE-SAFE RF / CEM / HURDLE / KELLY EXPERIMENT RUNNER")
    print("  Causal OOF RF | Static OOS | Online WFO | Final terminal holdout")
    print("=" * 88)

    if not CANDIDATES_PATH.exists():
        raise FileNotFoundError(f"Candidate parquet not found: {CANDIDATES_PATH}")

    df = pd.read_parquet(CANDIDATES_PATH)
    required = {"symbol", "market_id", "t_theta", "split", REL_COL, LABEL_AVAILABLE_COL}
    missing = sorted(required - set(df.columns))
    if missing:
        raise ValueError(f"{CANDIDATES_PATH} is missing required columns: {missing}")

    df = df[df[REL_COL].astype(float) > 0.5].copy()
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True)
    df["label_available_ts"] = pd.to_datetime(df[LABEL_AVAILABLE_COL], utc=True)
    df = df[df["label_available_ts"].notna()].copy()
    df["_candidate_id"] = df.index.astype(str)

    print(f"\n  {len(df)} relevance-filtered candidates loaded", flush=True)
    print(f"  Target availability timestamp: {LABEL_AVAILABLE_COL}", flush=True)

    partitions = partition_data(df)
    development = partitions["development"]
    preterminal = partitions["preterminal"]
    static_eval = partitions["static_eval"]
    terminal = partitions["terminal"]
    static_start = partitions["static_start"]
    static_end = partitions["static_end"]
    terminal_start = partitions["terminal_start"]

    print(
        f"  development={len(development)} | preterminal validation={len(preterminal)} | "
        f"terminal holdout={len(terminal)}",
        flush=True,
    )
    print(
        f"  static OOS: {static_start.date()} to {static_end.date()} | "
        f"terminal starts: {terminal_start.date()}",
        flush=True,
    )

    prices, probs = asyncio.run(load_paths(df))
    print(f"  loaded paths: {len(prices)} symbols, {len(probs)} markets", flush=True)

    # Make clear whether the hurdle is using raw asset return or a benchmark-
    # relative target. The runner remains executable in either case.
    target_info: dict[str, tuple[str, bool, str]] = {}
    for benchmark in ("SPY", "QQQ"):
        target_col, is_excess = resolve_target_column(df, benchmark)
        unit = infer_return_unit(development[target_col])
        target_info[benchmark] = (target_col, is_excess, unit)
        mode = "benchmark excess-return target" if is_excess else "raw asset-return target"
        print(f"  {benchmark} RF target: {target_col} ({mode}, unit={unit})", flush=True)
        if not is_excess:
            print(
                f"    WARNING: {benchmark} T1 uses raw expected asset return. Add one of "
                f"{BENCHMARK_EXCESS_TARGET_CANDIDATES[benchmark]} to make the hurdle a true active-excess gate.",
                flush=True,
            )

    all_preterminal = pd.concat([development, preterminal], axis=0).sort_values("t_theta").copy()

    static_rows: list[dict[str, Any]] = []
    wfo_rows: list[dict[str, Any]] = []
    terminal_rows: list[dict[str, Any]] = []

    for experiment_number, experiment in enumerate(EXPERIMENTS, start=1):
        label = experiment["label"]
        print(f"\n{'=' * 88}")
        print(f"  EXPERIMENT {experiment_number}/{len(EXPERIMENTS)}: {label}")
        print(
            f"  T1 entry hurdle={experiment['hurdle']} | T2 train windows={experiment['wf']} | "
            f"T3 Kelly={experiment['kelly']}",
            flush=True,
        )
        print(f"{'=' * 88}")

        run_online = RUN_ONLINE_WFO_FOR_ALL_EXPERIMENTS or bool(experiment["wf"])

        for benchmark in ("SPY", "QQQ"):
            target_col, _is_excess, target_unit = target_info[benchmark]
            for outer_seed in OUTER_SEEDS:
                print(f"\n  [{benchmark} | seed={outer_seed} | static OOS]", flush=True)
                static_result, static_trades, static_equity = evaluate_static_oos(
                    experiment=experiment,
                    benchmark=benchmark,
                    seed=outer_seed,
                    development=development,
                    static_eval=static_eval,
                    static_start=static_start,
                    static_end=static_end,
                    target_col=target_col,
                    target_unit=target_unit,
                    prices=prices,
                    probs=probs,
                )
                static_rows.append(static_result)
                save_audit_logs("static_oos", label, benchmark, outer_seed, static_trades, static_equity)
                print(
                    f"    → OOS={static_result['return_pct']:+.2f}% B&H={static_result['benchmark_return_pct']:+.2f}% "
                    f"excess={static_result['excess_return_pct']:+.2f}% trades={static_result['trades']}",
                    flush=True,
                )

                if run_online:
                    print(f"\n  [{benchmark} | seed={outer_seed} | expanding online WFO]", flush=True)
                    wfo_result, wfo_trades, wfo_equity, policy_log = evaluate_online_wfo(
                        experiment=experiment,
                        benchmark=benchmark,
                        seed=outer_seed,
                        all_data=df,
                        preterminal=preterminal,
                        static_start=static_start,
                        static_end=static_end,
                        target_col=target_col,
                        target_unit=target_unit,
                        prices=prices,
                        probs=probs,
                    )
                    if wfo_result is not None:
                        wfo_rows.append(wfo_result)
                        save_audit_logs("online_wfo", label, benchmark, outer_seed, wfo_trades, wfo_equity)
                        save_policy_log(label, benchmark, outer_seed, policy_log)
                        print(
                            f"    → WFO={wfo_result['return_pct']:+.2f}% B&H={wfo_result['benchmark_return_pct']:+.2f}% "
                            f"excess={wfo_result['excess_return_pct']:+.2f}% trades={wfo_result['trades']} "
                            f"windows={wfo_result['n_windows']}",
                            flush=True,
                        )

                print(f"\n  [{benchmark} | seed={outer_seed} | FINAL TERMINAL HOLDOUT]", flush=True)
                terminal_result, terminal_trades, terminal_equity = evaluate_terminal_holdout(
                    experiment=experiment,
                    benchmark=benchmark,
                    seed=outer_seed,
                    all_preterminal=all_preterminal,
                    terminal=terminal,
                    terminal_start=terminal_start,
                    target_col=target_col,
                    target_unit=target_unit,
                    prices=prices,
                    probs=probs,
                )
                terminal_rows.append(terminal_result)
                save_audit_logs("terminal_holdout", label, benchmark, outer_seed, terminal_trades, terminal_equity)
                print(
                    f"    → TERMINAL={terminal_result['return_pct']:+.2f}% B&H={terminal_result['benchmark_return_pct']:+.2f}% "
                    f"excess={terminal_result['excess_return_pct']:+.2f}% trades={terminal_result['trades']}",
                    flush=True,
                )

    write_csv(STATIC_RESULTS_CSV, static_rows)
    write_csv(WFO_RESULTS_CSV, wfo_rows)
    write_csv(TERMINAL_RESULTS_CSV, terminal_rows)
    summary_rows = aggregate_results(static_rows + wfo_rows + terminal_rows)
    write_csv(SUMMARY_RESULTS_CSV, summary_rows)

    print_stage_table("STATIC OOS RESULTS", static_rows)
    print_stage_table("ONLINE WFO RESULTS", wfo_rows)
    print_stage_table("FINAL TERMINAL HOLDOUT RESULTS — PRIMARY EVIDENCE", terminal_rows)

    elapsed_minutes = (time.time() - started) / 60.0
    print(f"\n  Saved static OOS:      {STATIC_RESULTS_CSV}")
    print(f"  Saved online WFO:      {WFO_RESULTS_CSV}")
    print(f"  Saved terminal holdout:{TERMINAL_RESULTS_CSV}")
    print(f"  Saved outer-seed summary: {SUMMARY_RESULTS_CSV}")
    print(f"  Saved trade logs:      {TRADE_LOG_DIR}")
    print(f"  Saved equity logs:     {EQUITY_LOG_DIR}")
    print(f"  Saved WFO policies:    {POLICY_LOG_DIR}")
    print(f"  Total elapsed: {elapsed_minutes:.1f} min")


if __name__ == "__main__":
    main()
