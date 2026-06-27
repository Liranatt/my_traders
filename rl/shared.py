"""
Re-exports of shared simulation, portfolio, and experiment components
for the RL agent. This avoids code duplication and ensures that the RL
agent's environment and the existing CEM simulations are comparable.
"""

# flake8: noqa
# ruff: noqa

# ── From run_experiments.py ──────────────────────────────────────────────────
from run_experiments import (
    INITIAL_CAPITAL,
    PORTFOLIO_BOUNDS,
    DD_PENALTY,
    MIN_DAILY_RETURNS_FOR_SHARPE,
    as_utc_day,
    ib_cost,
    _close_on,
    _calendar_dates,
    truncate_paths,
    _affordable_buy_qty,
    kelly_size,
    create_expanding_wf_folds,
    rows_completed_before,
    assert_rows_completed_before,
    daily_equity_sharpe,
    completed_trade_history_before,
    load_paths,
)

# ── From run_experiments_rf.py ───────────────────────────────────────────────
from run_experiments_rf import (
    PURGE_DAYS,
    TARGET_RETURN_UNIT,
    build_rf,
    causal_purged_oof_predictions,
    fit_predict_as_of,
    known_label_mask,
    infer_return_unit,
    prediction_to_decimal,
    partition_data,
    build_online_windows,
)

# ── From pipeline/strategy.py ────────────────────────────────────────────────
from pipeline.strategy import (
    DEFAULT_POLICY,
    RELEVANCE_COL,
    entry_day,
    long_unfavorable,
)

# ── From pipeline/data_loader.py ─────────────────────────────────────────────
from pipeline.data_loader import NUM_FEATURES_LEAN, TARGET
