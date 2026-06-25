"""Unit coverage for the 20260623 sweep changes: query-window default, the new
ML-stop / fallback config knobs and their reproducibility, and the capital/percent-return
metric. All pure functions -- no DB, no full backtest (that is covered by the smoke run)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from main_backtesting.config import BacktestConfig
from main_backtesting.models import Trade
from main_backtesting.reporting import _capital_metrics
from strategies.event_driven_ml import (
    FEATURE_NAMES,
    QUERY_WINDOW_FEATURE_NAMES,
    active_feature_names,
)


def test_query_window_features_are_on_by_default() -> None:
    assert BacktestConfig().ml_use_query_window_features is True
    on = active_feature_names(True)
    assert on[: len(FEATURE_NAMES)] == FEATURE_NAMES
    assert on[-len(QUERY_WINDOW_FEATURE_NAMES) :] == QUERY_WINDOW_FEATURE_NAMES
    assert active_feature_names(False) == FEATURE_NAMES


def test_new_stop_and_fallback_config_fields_roundtrip() -> None:
    config = BacktestConfig(
        ml_stop_mode="vol_trail",
        ml_vol_stop_multiplier=4.0,
        fallback_strategy="event_window_long",
        fallback_stop_mode="fixed",
        fallback_stop_loss_pct=0.06,
        max_holding_days=15,
        starting_capital=75_000.0,
    )
    assert BacktestConfig.from_json(config.to_json()) == config


def test_resumed_old_config_keeps_query_window_off_and_momentum_fallback() -> None:
    """A run saved before these knobs existed must resume exactly as it ran: query-window
    features OFF (even though new runs default them ON) and the momentum fallback."""
    values = BacktestConfig().to_json()
    for key in (
        "ml_use_query_window_features",
        "ml_stop_mode",
        "ml_vol_stop_multiplier",
        "fallback_strategy",
        "fallback_stop_mode",
        "fallback_stop_loss_pct",
        "max_holding_days",
        "starting_capital",
    ):
        values.pop(key, None)
    restored = BacktestConfig.from_json(values)
    assert restored.ml_use_query_window_features is False
    assert restored.fallback_strategy == "momentum"
    assert restored.ml_stop_mode == "fixed"
    assert restored.max_holding_days == 0  # no cap by default -> old runs unchanged
    assert restored.starting_capital == 50_000.0


def _trade(entry: datetime, exit_at: datetime, net: float) -> dict:
    return {"entry_at": entry, "exit_at": exit_at, "net_profit": net}


def test_capital_metrics_peak_concurrency_and_percent_returns() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    day = timedelta(days=1)
    trades = [
        _trade(start, start + 5 * day, 100.0),             # open days 0-5
        _trade(start + 2 * day, start + 8 * day, -40.0),   # open days 2-8
        _trade(start + 4 * day, start + 6 * day, 30.0),    # open days 4-6 (all three at 4-5)
    ]
    metrics = _capital_metrics(trades, trade_notional=1000.0, starting_capital=50_000.0)
    assert metrics["peak_concurrent_positions"] == 3
    assert metrics["peak_capital_deployed"] == 3000.0
    assert metrics["net_profit"] == 90.0
    assert metrics["return_on_starting_capital_pct"] == 90.0 / 50_000.0 * 100.0
    assert metrics["return_on_peak_capital_pct"] == 90.0 / 3000.0 * 100.0
    assert metrics["total_tickets_notional"] == 3000.0


def test_capital_metrics_release_before_acquire_at_same_timestamp() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    day = timedelta(days=1)
    # First trade closes exactly as the second opens -> never two positions at once.
    trades = [
        _trade(start, start + 2 * day, 10.0),
        _trade(start + 2 * day, start + 4 * day, 10.0),
    ]
    metrics = _capital_metrics(trades, trade_notional=1000.0, starting_capital=10_000.0)
    assert metrics["peak_concurrent_positions"] == 1
    assert metrics["peak_capital_deployed"] == 1000.0


def test_capital_metrics_handles_open_trades_without_exit() -> None:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    trades = [{"entry_at": start, "exit_at": None, "net_profit": None}]
    metrics = _capital_metrics(trades, trade_notional=1000.0, starting_capital=10_000.0)
    assert metrics["peak_concurrent_positions"] == 1
    assert metrics["net_profit"] == 0.0
    assert metrics["return_on_starting_capital_pct"] == 0.0


def _hedge_trade(direction: str, entry: float, exit_: float, qty: float) -> Trade:
    return Trade(
        trade_id=uuid4(), run_id=uuid4(), market_id="m", event_id="e", question="q",
        symbol="X", asset_name="X", pass_number=1,
        trigger_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        entry_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
        entry_price=entry, quantity=qty, entry_commission=0.0,
        initial_stop=0.0, current_stop=0.0, highest_price=entry,
        final_outcome=None, direction=direction, exit_price=exit_, exit_commission=0.0,
    )


def test_hedge_folds_into_net_profit_for_a_long() -> None:
    t = _hedge_trade("long", 100.0, 110.0, 10.0)   # beneficiary +10% -> +100 unhedged
    assert t.net_profit == 100.0             # no hedge attached yet
    # long beneficiary -> SHORT the ETF; ETF rose +6% (50 -> 53)
    t.hedge_symbol, t.hedge_entry_price, t.hedge_exit_price = "XLE", 50.0, 53.0
    t.hedge_quantity, t.hedge_commission = 20.0, 0.0
    assert t.hedge_net_profit == -60.0       # short 20 sh * +3
    assert t.net_profit == 40.0              # idiosyncratic +4% (asset +10% beat ETF +6%)


def test_hedge_folds_into_net_profit_for_a_short() -> None:
    t = _hedge_trade("short", 100.0, 90.0, 10.0)   # beneficiary -10% -> short makes +100 unhedged
    assert t.net_profit == 100.0
    # short beneficiary -> LONG the ETF; ETF fell -4% (50 -> 48)
    t.hedge_symbol, t.hedge_entry_price, t.hedge_exit_price = "XLE", 50.0, 48.0
    t.hedge_quantity, t.hedge_commission = 20.0, 0.0
    assert t.hedge_net_profit == -40.0       # long 20 sh * -2
    assert t.net_profit == 60.0              # idiosyncratic +6% (asset -10% vs ETF -4%)
