import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from main_backtesting.config import BacktestConfig
from main_backtesting.models import PriceBar, ProbabilityPoint, Trade, Asset
from main_backtesting.stages.simulation import simulate_one_trade
from strategies.event_driven_long import EventDrivenLongStrategy

START = datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc)

def test_daily_close_integration_end_to_end(tmp_path) -> None:
    async def run():
        config = BacktestConfig(
            output_root=tmp_path,
            asset_price_policy="daily_close_to_close",
            trailing_stop_close_volatility_multiplier=2.0,
        )
        
        fake_engine = SimpleNamespace(
            config=config,
            run_id=uuid4(),
            run_dir=tmp_path,
            strategy=EventDrivenLongStrategy(range_period=2, range_multiplier=2.0),
        )
        # Sequence of daily bars
        bars = [
            PriceBar(timestamp=START, open=100, high=105, low=95, close=100, volume=100),
            PriceBar(timestamp=START + timedelta(days=1), open=100, high=105, low=95, close=102, volume=100),
            PriceBar(timestamp=START + timedelta(days=2), open=102, high=107, low=97, close=104, volume=100),
            PriceBar(timestamp=START + timedelta(days=3), open=104, high=109, low=99, close=106, volume=100),
            PriceBar(timestamp=START + timedelta(days=4), open=106, high=111, low=90, close=100, volume=100), # Drops to 100!
            PriceBar(timestamp=START + timedelta(days=5), open=98, high=100, low=90, close=95, volume=100), # Exit execution bar
            PriceBar(timestamp=START + timedelta(days=6), open=95, high=98, low=92, close=94, volume=100),
        ]
        
        market = SimpleNamespace(
            market_id="m1",
            event_id="e1",
            question="Question?",
            end_at=START + timedelta(days=10),
            final_outcome="Yes",
            tags=[],
            raw_market={},
            yes_token_id="yes",
            condition_id="c1",
        )
        
        trigger = START + timedelta(days=2, hours=14)
        
        # We need event_symbol_trade_counts = {} to test it
        trade, _ = await simulate_one_trade(
            fake_engine,
            market=market,
            asset=Asset("TEST", "Test Asset", "stock", "Direct exposure"),
            pass_number=1,
            trigger_at=trigger,
            portfolio="polymarket_momentum",
            strategy_branch="momentum",
            direction="long",
            resolution="1d",
            bars=bars,
            probabilities=[ProbabilityPoint(trigger, 0.60)],
        )
        return trade
    
    trade = asyncio.run(run())
    assert trade is not None
    # Entry should be next open after trigger (which is at day 2 + 14h) -> day 3
    assert trade.entry_at == START + timedelta(days=3)
    assert trade.entry_price == 104
    
    # Exit decision should be at day 4 close because close drops from 106 to 100
    # Stop was ratcheting up. Day 2 to 3 move was 104-102=2. Day 1 to 2 was 102-100=2. Vol = 2.0.
    # At day 3 (close 106), stop was 106 - 2.0*2 = 102.
    # At day 4 (close 100), it's < 102. So exit triggered.
    # Execution happens on day 5 open (98).
    assert trade.exit_reason == "daily_close_trailing_stop"
    assert trade.exit_decision_at == START + timedelta(days=4, hours=6, minutes=30)
    assert trade.exit_decision_price == 100.0
    assert trade.exit_at == START + timedelta(days=5)
    assert trade.exit_price == 98.0
