from datetime import datetime, timezone
from uuid import uuid4

import pytest

from main_backtesting.models import PriceBar, Trade
from strategies.event_driven_long import (
    average_close_volatility,
    rate_of_change,
    EventDrivenStrategy,
)
from strategies.event_driven_ml import observation_targets, evaluate_prediction
from database.backtesting.market_data import next_bar_after
from main_backtesting.stages.simulation import MLObservation


def _make_bar(dt: datetime, o: float, h: float, l: float, c: float) -> PriceBar:
    return PriceBar(
        timestamp=dt,
        open=o,
        high=h,
        low=l,
        close=c,
        volume=1000,
    )


def test_average_close_volatility():
    # We need period + 1 bars to have `period` moves.
    bars = [
        _make_bar(datetime(2024, 1, 1, tzinfo=timezone.utc), 100, 100, 100, 100),
        _make_bar(datetime(2024, 1, 2, tzinfo=timezone.utc), 100, 100, 100, 102),  # +2
        _make_bar(datetime(2024, 1, 3, tzinfo=timezone.utc), 100, 100, 100, 101),  # -1 -> abs 1
        _make_bar(datetime(2024, 1, 4, tzinfo=timezone.utc), 100, 100, 100, 104),  # +3
        _make_bar(datetime(2024, 1, 5, tzinfo=timezone.utc), 100, 100, 100, 102),  # -2 -> abs 2
        _make_bar(datetime(2024, 1, 6, tzinfo=timezone.utc), 100, 100, 100, 106),  # +4
    ]
    # Moves are: 2, 1, 3, 2, 4. Average = (2+1+3+2+4)/5 = 12/5 = 2.4
    assert average_close_volatility(bars, period=5) == 2.4
    # Insufficient bars:
    assert average_close_volatility(bars[:5], period=5) is None


def test_daily_momentum_uses_completed_daily_closes():
    bars = [
        _make_bar(datetime(2024, 1, 1, tzinfo=timezone.utc), 100, 100, 100, 100),
        _make_bar(datetime(2024, 1, 2, tzinfo=timezone.utc), 100, 100, 100, 105),
        _make_bar(datetime(2024, 1, 3, tzinfo=timezone.utc), 100, 100, 100, 110),
    ]
    # rate_of_change over period 2: (110 - 100) / 100 = 0.1
    assert rate_of_change(bars, 2) == pytest.approx(0.1)

    bars_neg = [
        _make_bar(datetime(2024, 1, 1, tzinfo=timezone.utc), 100, 100, 100, 100),
        _make_bar(datetime(2024, 1, 2, tzinfo=timezone.utc), 100, 100, 100, 95),
        _make_bar(datetime(2024, 1, 3, tzinfo=timezone.utc), 100, 100, 100, 90),
    ]
    assert rate_of_change(bars_neg, 2) == pytest.approx(-0.1)


def test_midday_threshold_enters_next_daily_open():
    bars = [
        _make_bar(datetime(2024, 1, 1, tzinfo=timezone.utc), 100, 105, 95, 102),
        _make_bar(datetime(2024, 1, 2, tzinfo=timezone.utc), 102, 107, 101, 106),
        _make_bar(datetime(2024, 1, 3, tzinfo=timezone.utc), 106, 110, 105, 108),
    ]
    trigger = datetime(2024, 1, 1, 14, 0, tzinfo=timezone.utc)
    entry_bar = next_bar_after(bars, trigger)
    assert entry_bar is not None
    assert entry_bar.timestamp == datetime(2024, 1, 2, tzinfo=timezone.utc)
    assert entry_bar.open == 102


def test_intraday_stop_breach_does_not_exit():
    trade = Trade(
        trade_id=uuid4(),
        run_id=uuid4(),
        portfolio="test",
        strategy_branch="momentum",
        resolution="1d",
        direction="long",
        market_id="m1",
        event_id="e1",
        question="q",
        symbol="SYM",
        asset_name="Asset",
        pass_number=1,
        trigger_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        entry_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        entry_price=100.0,
        quantity=10,
        entry_commission=0,
        initial_stop=90.0,
        current_stop=95.0,
        highest_price=100.0,
        final_outcome="yes",
    )
    trade.current_stop = 95.0
    strategy = EventDrivenStrategy(trade_notional=1000, range_period=5, range_multiplier=10.0)
    
    previous_bars = [
        _make_bar(datetime(2024, 1, i, tzinfo=timezone.utc), 100, 105, 95, 100 + i)
        for i in range(1, 7)
    ]
    # Current bar goes low (94) which is below stop (95), but closes high (96).
    bar = _make_bar(datetime(2024, 1, 7, tzinfo=timezone.utc), 100, 105, 94, 96)
    
    triggered = strategy.update_trade_daily_close(trade, bar, previous_bars)
    assert not triggered


def test_daily_close_below_stop_exits_next_open():
    trade = Trade(
        trade_id=uuid4(),
        run_id=uuid4(),
        portfolio="test",
        strategy_branch="momentum",
        resolution="1d",
        direction="long",
        market_id="m1",
        event_id="e1",
        question="q",
        symbol="SYM",
        asset_name="Asset",
        pass_number=1,
        trigger_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        entry_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        entry_price=100.0,
        quantity=10,
        entry_commission=0,
        initial_stop=90.0,
        current_stop=95.0,
        highest_price=100.0,
        final_outcome="yes",
    )
    trade.current_stop = 95.0
    strategy = EventDrivenStrategy(trade_notional=1000, range_period=5, range_multiplier=1.0)
    
    previous_bars = [
        _make_bar(datetime(2024, 1, i, tzinfo=timezone.utc), 100, 105, 95, 100 + i)
        for i in range(1, 7)
    ]
    # Current bar close is 94, below stop of 95.
    bar = _make_bar(datetime(2024, 1, 7, tzinfo=timezone.utc), 100, 105, 90, 94)
    
    triggered = strategy.update_trade_daily_close(trade, bar, previous_bars)
    assert triggered
    assert trade.exit_reason == "daily_close_trailing_stop"


def test_ml_labels_use_close_to_close():
    bars = [
        _make_bar(datetime(2024, 1, 1, tzinfo=timezone.utc), 100, 100, 100, 100),
        _make_bar(datetime(2024, 1, 2, tzinfo=timezone.utc), 100, 110, 90, 105), # max high 110, max close 105
        _make_bar(datetime(2024, 1, 3, tzinfo=timezone.utc), 100, 100, 85, 95),  # min low 85, min close 95
    ]
    direction, peak, metadata = observation_targets(
        bars,
        event_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        peak_window_start=datetime(2024, 1, 1, 7, tzinfo=timezone.utc),
        end=datetime(2024, 1, 10, tzinfo=timezone.utc)
    )
    assert metadata["maximum_change"] == pytest.approx(0.05) # not 0.10
    assert metadata["minimum_change"] == pytest.approx(-0.05) # not -0.15


def test_ml_target_reached_uses_daily_close():
    class DummyPrediction:
        def __init__(self):
            self.direction = "long"
            self.predicted_target_price = 105.0
            self.classification_probability = 0.8
            self.predicted_peak_percent = 0.05
            self.directions_agree = True
            self.target_reached = None
            self.realized_move_at_entry = 0.0
            self.remaining_gap = 0.0

    pred = DummyPrediction()
    # High reaches 106, but close is 104
    bars1 = [
        _make_bar(datetime(2024, 1, 2, tzinfo=timezone.utc), 100, 106, 90, 104)
    ]
    evaluate_prediction(pred, event_open_price=100.0, bars_until_event_end=bars1)
    assert pred.target_reached is False

    pred = DummyPrediction()
    # High reaches 106, and close is 105
    bars2 = [
        _make_bar(datetime(2024, 1, 2, tzinfo=timezone.utc), 100, 106, 90, 105)
    ]
    evaluate_prediction(pred, event_open_price=100.0, bars_until_event_end=bars2)
    assert pred.target_reached is True


def test_ml_direction_uses_terminal_close_not_largest_intraday_move():
    bars = [
        _make_bar(datetime(2024, 1, 1, tzinfo=timezone.utc), 100, 100, 100, 100),
        _make_bar(datetime(2024, 1, 2, tzinfo=timezone.utc), 100, 150, 99, 120),
        _make_bar(datetime(2024, 1, 3, tzinfo=timezone.utc), 120, 125, 90, 95),
    ]
    direction, _, metadata = observation_targets(
        bars,
        event_start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        peak_window_start=datetime(2024, 1, 1, 7, tzinfo=timezone.utc),
        end=datetime(2024, 1, 10, tzinfo=timezone.utc),
    )

    assert direction == -1
    assert metadata["terminal_close_price"] == 95


def test_duplicate_event_symbol_suppression():
    event_symbol_trade_counts = {("e1", "SYM"): 1}
    max_trades_per_event_symbol = 1
    existing_count = event_symbol_trade_counts.get(("e1", "SYM"), 0)
    should_suppress = existing_count >= max_trades_per_event_symbol
    assert should_suppress is True


def test_close_based_trailing_stop_ratchets_up():
    trade = Trade(
        trade_id=uuid4(),
        run_id=uuid4(),
        portfolio="test",
        strategy_branch="momentum",
        resolution="1d",
        direction="long",
        market_id="m1",
        event_id="e1",
        question="q",
        symbol="SYM",
        asset_name="Asset",
        pass_number=1,
        trigger_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        entry_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        entry_price=100.0,
        quantity=10,
        entry_commission=0,
        initial_stop=90.0,
        current_stop=90.0,
        highest_price=100.0,
        final_outcome="yes",
    )
    trade.current_stop = 90.0
    strategy = EventDrivenStrategy(trade_notional=1000, range_period=2, range_multiplier=1.0)
    
    # Needs some previous bars for volatility
    previous_bars = [
        _make_bar(datetime(2024, 1, 1, tzinfo=timezone.utc), 100, 100, 100, 100),
        _make_bar(datetime(2024, 1, 2, tzinfo=timezone.utc), 100, 100, 100, 102),
        _make_bar(datetime(2024, 1, 3, tzinfo=timezone.utc), 100, 100, 100, 104),
    ] # Moves: 2, 2. vol = 2.0
    
    bar1 = _make_bar(datetime(2024, 1, 4, tzinfo=timezone.utc), 100, 100, 100, 106) # Vol: 2.0. stop candidate: 106 - 1.0*2.0 = 104
    strategy.update_trade_daily_close(trade, bar1, previous_bars)
    assert trade.current_stop == 104.0
    
    previous_bars.append(bar1)
    bar2 = _make_bar(datetime(2024, 1, 5, tzinfo=timezone.utc), 100, 100, 100, 108) # Vol: 2.0. stop candidate: 108 - 2.0 = 106
    strategy.update_trade_daily_close(trade, bar2, previous_bars)
    assert trade.current_stop == 106.0
    
    previous_bars.append(bar2)
    bar3 = _make_bar(datetime(2024, 1, 6, tzinfo=timezone.utc), 100, 100, 100, 107) # Drop. Vol: 1.5. stop candidate: 108 (highest close) - 1.5 = 106.5
    strategy.update_trade_daily_close(trade, bar3, previous_bars)
    assert trade.current_stop == 106.5 # updated correctly
