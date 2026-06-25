from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from typing import Any
from database.backtesting.repositories import json_value
from database.backtesting.repositories.machine_learning import (
    prior_ml_observations,
    save_ml_prediction,
    save_model_snapshot,
)
from database.backtesting.repositories.prices import price_bars
from database.backtesting.repositories.probabilities import probability_history
from database.backtesting.repositories.runs import finish_work, start_work
from database.backtesting.repositories.trades import (
    increment_duplicate_suppression_count,
    primary_event_symbol_trade_counts,
    save_momentum_parameter_results,
    save_trade,
    select_walk_forward_momentum_parameters,
)
from database.backtesting.repositories.worlds import run_resolved_world_assets
from database.backtesting.market_data import bars_before, bars_from, next_bar_after
from database.backtesting.polymarket import probability_as_of
from main_backtesting.models import Asset, MLObservation, PriceBar, ProbabilityPoint, SourceMarket, Trade
from main_backtesting.reporting import create_trade_graph
from main_backtesting.semantic_groups import (
    SEMANTIC_GROUPING_VERSION,
    classify_assignment,
)
from strategies.event_driven_long import EventDrivenStrategy, ib_style_commission, rate_of_change
from strategies.event_driven_ml import (
    DAILY_SESSION_LENGTH,
    active_feature_names,
    evaluate_prediction,
    predict,
    train_snapshot,
)

from main_backtesting.stages.event_filter import accepted_markets, run_events


def _hours_below_band(
    probabilities: list[ProbabilityPoint], as_of: datetime, band: float
) -> float | None:
    """Hours the Polymarket probability has been continuously below ``band`` as of
    ``as_of``. 0.0 if currently at/above the band; None if there is no history."""
    points = [point for point in probabilities if point.timestamp <= as_of]
    if not points:
        return None
    if points[-1].probability >= band:
        return 0.0
    last_at_or_above = None
    for point in points:
        if point.probability >= band:
            last_at_or_above = point.timestamp
    start = last_at_or_above if last_at_or_above is not None else points[0].timestamp
    return (as_of - start).total_seconds() / 3600.0


def _entry_crossing_confirmed(
    probabilities: list[ProbabilityPoint],
    trigger_at: datetime,
    *,
    base_threshold: float,
    strong_probability: float,
    confirmation_hours: float,
) -> bool:
    """Entry-crossing confirmation (experimental; disabled when confirmation_hours <= 0).

    Crossings at/above ``strong_probability`` enter immediately. Marginal crossings
    (between ``base_threshold`` and ``strong_probability``) enter only if the probability
    stays above ``base_threshold`` for the whole confirmation window; otherwise the
    crossing is treated as a transient spike and rejected.
    """
    if confirmation_hours <= 0:
        return True
    crossing = probability_as_of(probabilities, trigger_at)
    if crossing is None:
        return True
    if crossing >= strong_probability:
        return True
    window_end = trigger_at + timedelta(hours=confirmation_hours)
    window = [
        point.probability
        for point in probabilities
        if trigger_at < point.timestamp <= window_end
    ]
    if not window:
        return False
    return all(probability > base_threshold for probability in window)


def completed_daily_bars_as_of(
    bars: list[PriceBar],
    timestamp: datetime,
) -> list[PriceBar]:
    return [
        bar
        for bar in bars
        if bar.timestamp + DAILY_SESSION_LENGTH <= timestamp
    ]


def latest_completed_daily_bar(
    bars: list[PriceBar],
    timestamp: datetime,
) -> PriceBar | None:
    completed = completed_daily_bars_as_of(bars, timestamp)
    return completed[-1] if completed else None


def polymarket_volume_quality(
    probabilities: list[ProbabilityPoint],
    *,
    trigger_at: datetime,
    minimum_pre_entry_usdc: float,
    concentration_minimum_usdc: float,
    max_single_hour_share: float,
) -> dict[str, Any]:
    completed_volumes = [
        max(float(point.volume_usdc or 0.0), 0.0)
        for point in probabilities
        if point.timestamp <= trigger_at and point.volume_usdc is not None
    ]
    if not completed_volumes:
        return {
            "gate_applied": False,
            "allowed": False,
            "reason": "polymarket_volume_unavailable",
            "pre_entry_volume_usdc": None,
            "active_volume_hours": 0,
            "largest_hour_volume_usdc": None,
            "largest_hour_share": None,
            "median_active_hour_volume_usdc": None,
            "latest_completed_hour_volume_usdc": None,
        }

    total = sum(completed_volumes)
    active_volumes = sorted(volume for volume in completed_volumes if volume > 0)
    active_hours = sum(volume > 0 for volume in completed_volumes)
    largest_hour = max(completed_volumes)
    largest_hour_share = largest_hour / total if total > 0 else None
    median_active = None
    if active_volumes:
        midpoint = len(active_volumes) // 2
        median_active = (
            active_volumes[midpoint]
            if len(active_volumes) % 2
            else (active_volumes[midpoint - 1] + active_volumes[midpoint]) / 2
        )
    if total < minimum_pre_entry_usdc:
        reason = "polymarket_volume_not_significant"
        allowed = False
    elif (
        largest_hour >= concentration_minimum_usdc
        and largest_hour_share is not None
        and largest_hour_share > max_single_hour_share
    ):
        reason = "polymarket_volume_single_hour_concentration"
        allowed = False
    else:
        reason = "polymarket_volume_quality_confirmed"
        allowed = True
    return {
        "gate_applied": False,
        "allowed": allowed,
        "reason": reason,
        "pre_entry_volume_usdc": total,
        "active_volume_hours": active_hours,
        "largest_hour_volume_usdc": largest_hour,
        "largest_hour_share": largest_hour_share,
        "median_active_hour_volume_usdc": median_active,
        "latest_completed_hour_volume_usdc": completed_volumes[-1],
    }


async def simulate_one_trade(
    self,
    *,
    market: SourceMarket,
    asset: Asset,
    pass_number: int,
    trigger_at: datetime,
    portfolio: str,
    strategy_branch: str,
    direction: str,
    resolution: str,
    bars: list[PriceBar],
    probabilities: list[ProbabilityPoint],
    strategy: EventDrivenStrategy | None = None,
    momentum_lookback: int | None = None,
    graph_bars: list[PriceBar] | None = None,
    predicted_target_price: float | None = None,
    predicted_target_reached: bool | None = None,
    evaluation_event_end: datetime | None = None,
    create_graph: bool = True,
    anchor_close_at: datetime | None = None,
    anchor_close_price: float | None = None,
) -> tuple[Trade | None, dict[str, Any]]:
    active_strategy = strategy or self.strategy
    decision: dict[str, Any] = {
        "portfolio": portfolio,
        "strategy_branch": strategy_branch,
        "direction": direction,
        "opened": False,
    }
    entry_bar = next_bar_after(bars, trigger_at)
    if (
        entry_bar is None
        or entry_bar.timestamp < self.config.start
        or entry_bar.timestamp >= self.config.end
    ):
        return None, {**decision, "reason": "no_next_price_bar_before_simulation_end"}
    latest_probability = probability_as_of(probabilities, entry_bar.timestamp)
    decision["entry_at"] = entry_bar.timestamp
    decision["probability_at_entry"] = latest_probability
    if latest_probability is None or latest_probability <= self.config.threshold:
        return None, {**decision, "reason": "probability_not_above_threshold_at_entry"}
    if not _entry_crossing_confirmed(
        probabilities,
        trigger_at,
        base_threshold=self.config.threshold,
        strong_probability=self.config.entry_strong_probability,
        confirmation_hours=self.config.entry_confirmation_hours,
    ):
        return None, {**decision, "reason": "entry_crossing_not_confirmed"}
    # Any branch that supplies an event end (ML, counterfactual, event_window_long
    # fallback) exits one day before resolution. Momentum passes no end -> no deadline.
    exit_deadline = (
        evaluation_event_end - timedelta(days=1)
        if evaluation_event_end is not None
        else None
    )
    # Hard max-holding-period cap (config.max_holding_days; 0 = disabled). Event edges are
    # front-loaded, so this bounds the horizon and protects capital efficiency / % return.
    holding_deadline = (
        entry_bar.timestamp + timedelta(days=self.config.max_holding_days)
        if self.config.max_holding_days and self.config.max_holding_days > 0
        else None
    )
    if exit_deadline is not None and entry_bar.timestamp >= exit_deadline:
        return None, {**decision, "reason": "ml_entry_after_one_day_before_market_end"}
    trade = active_strategy.open_trade(
        run_id=self.run_id,
        market_id=market.market_id,
        event_id=market.event_id,
        question=market.question,
        symbol=asset.symbol,
        asset_name=asset.asset_name,
        pass_number=pass_number,
        trigger_at=trigger_at,
        entry_bar=entry_bar,
        previous_bars=bars_before(bars, entry_bar.timestamp),
        final_outcome=market.final_outcome,
        direction=direction,  # type: ignore[arg-type]
        portfolio=portfolio,
        strategy_branch=strategy_branch,
        resolution=resolution,  # type: ignore[arg-type]
        predicted_target_price=predicted_target_price,
        price_policy=self.config.asset_price_policy,
    )
    if trade is None:
        return None, {
            **decision,
            "reason": "insufficient_trailing_stop_history_or_invalid_entry_price",
        }
    trade.predicted_target_reached = predicted_target_reached
    daily_close_policy = self.config.asset_price_policy == "daily_close_to_close"
    # Record diagnostic fields
    trade.price_policy = self.config.asset_price_policy
    trade.anchor_close_at = anchor_close_at
    trade.anchor_close_price = anchor_close_price
    pending_momentum_exit = False
    pending_probability_exit = False
    probability_exit_bar = None
    predicted_target_locked = False
    exit_decision_bar = None
    for bar in bars_from(bars, entry_bar.timestamp):
        # ---- deferred exits: execute at next daily open ----
        if bar.timestamp > entry_bar.timestamp and pending_momentum_exit:
            reason = "daily_momentum_reversal" if daily_close_policy else "momentum_reversal"
            trade.exit_decision_at = (
                exit_decision_bar.timestamp + DAILY_SESSION_LENGTH
                if daily_close_policy and exit_decision_bar
                else (exit_decision_bar.timestamp if exit_decision_bar else None)
            )
            trade.exit_decision_price = exit_decision_bar.close if exit_decision_bar else None
            active_strategy.close_trade(
                trade,
                timestamp=bar.timestamp,
                price=bar.open,
                reason=reason,
            )
            break
        if bar.timestamp > entry_bar.timestamp and pending_probability_exit:
            trade.exit_decision_at = (
                probability_exit_bar.timestamp + DAILY_SESSION_LENGTH
                if daily_close_policy and probability_exit_bar
                else (probability_exit_bar.timestamp if probability_exit_bar else None)
            )
            trade.exit_decision_price = (
                probability_exit_bar.close if probability_exit_bar else None
            )
            active_strategy.close_trade(
                trade,
                timestamp=bar.timestamp,
                price=bar.open,
                reason="probability_fell_below_threshold",
            )
            break
        if (
            holding_deadline is not None
            and bar.timestamp > entry_bar.timestamp
            and bar.timestamp >= holding_deadline
        ):
            active_strategy.close_trade(
                trade,
                timestamp=bar.timestamp,
                price=bar.open,
                reason="max_holding_period",
            )
            break
        if (
            exit_deadline is not None
            and bar.timestamp > entry_bar.timestamp
            and bar.timestamp >= exit_deadline
        ):
            active_strategy.close_trade(
                trade,
                timestamp=bar.timestamp,
                price=bar.open,
                reason="one_day_before_market_end",
            )
            break
        # ---- trailing stop evaluation ----
        if daily_close_policy:
            stop_triggered = active_strategy.update_trade_daily_close(
                trade, bar, bars_before(bars, bar.timestamp)
            )
            if stop_triggered:
                exit_decision_bar = bar
                # Execution deferred to next bar open
                next_bar = next_bar_after(bars, bar.timestamp)
                if next_bar is not None:
                    trade.exit_decision_at = bar.timestamp + DAILY_SESSION_LENGTH
                    trade.exit_decision_price = bar.close
                    if predicted_target_locked and trade.exit_reason == "daily_close_trailing_stop":
                        trade.exit_reason = "ml_predicted_target_lock"
                    active_strategy.close_trade(
                        trade,
                        timestamp=next_bar.timestamp,
                        price=next_bar.open,
                        reason=trade.exit_reason or "daily_close_trailing_stop",
                    )
                else:
                    trade.exit_decision_at = bar.timestamp + DAILY_SESSION_LENGTH
                    trade.exit_decision_price = bar.close
                    active_strategy.close_trade(
                        trade,
                        timestamp=bar.timestamp + DAILY_SESSION_LENGTH,
                        price=bar.close,
                        reason=trade.exit_reason or "daily_close_trailing_stop",
                    )
                break
        else:
            if active_strategy.update_trade(trade, bar, bars_before(bars, bar.timestamp)):
                if predicted_target_locked and trade.exit_reason == "trailing_stop":
                    trade.exit_reason = "ml_predicted_target_lock"
                break
        # ---- ML target lock ----
        if predicted_target_price is not None and not predicted_target_locked:
            if daily_close_policy:
                target_touched = (
                    bar.close >= predicted_target_price
                    if direction == "long"
                    else bar.close <= predicted_target_price
                )
            else:
                target_touched = (
                    bar.high >= predicted_target_price
                    if direction == "long"
                    else bar.low <= predicted_target_price
                )
            if target_touched:
                predicted_target_locked = True
                trade.current_stop = (
                    max(trade.current_stop, predicted_target_price)
                    if direction == "long"
                    else min(trade.current_stop, predicted_target_price)
                )
                trade.stop_history.append(
                    {
                        "timestamp": bar.timestamp,
                        "stop": trade.current_stop,
                        "reason": "ml_predicted_target_locked",
                    }
                )
        # ---- momentum reversal check (deferred execution) ----
        if strategy_branch == "momentum" and bar.timestamp >= entry_bar.timestamp:
            completed_bars = [
                item for item in bars if item.timestamp <= bar.timestamp
            ]
            momentum = rate_of_change(
                completed_bars,
                momentum_lookback or self.config.momentum_lookback_bars,
            )
            if momentum is not None and momentum <= 0:
                pending_momentum_exit = True
                exit_decision_bar = bar
            else:
                pending_momentum_exit = False
                exit_decision_bar = None
        # ---- probability fall-below exit (experimental; off in production) ----
        if (
            self.config.probability_exit_mode != "off"
            and not pending_probability_exit
            and bar.timestamp >= entry_bar.timestamp
        ):
            hours_below = _hours_below_band(
                probabilities, bar.timestamp, self.config.probability_exit_band
            )
            if hours_below:
                if self.config.probability_exit_mode == "immediate_band":
                    confirmed = True
                else:
                    confirmed = (
                        hours_below >= self.config.probability_exit_confirmation_hours
                    )
                if confirmed:
                    if self.config.probability_exit_mode == "tighten_breakeven":
                        trade.current_stop = (
                            max(trade.current_stop, trade.entry_price)
                            if direction == "long"
                            else min(trade.current_stop, trade.entry_price)
                        )
                    else:
                        pending_probability_exit = True
                        probability_exit_bar = bar
    if trade.exit_at is None:
        mark_end = min(self.config.end, exit_deadline) if exit_deadline else self.config.end
        final_bars = [bar for bar in bars if bar.timestamp < mark_end]
        if final_bars:
            trade.exit_decision_at = (
                final_bars[-1].timestamp + DAILY_SESSION_LENGTH
                if daily_close_policy
                else final_bars[-1].timestamp
            )
            trade.exit_decision_price = final_bars[-1].close
            active_strategy.close_trade(
                trade,
                timestamp=(
                    final_bars[-1].timestamp + DAILY_SESSION_LENGTH
                    if daily_close_policy
                    else final_bars[-1].timestamp
                ),
                price=final_bars[-1].close,
                reason=(
                    "one_day_before_market_end"
                    if exit_deadline is not None
                    else "market_end"
                ),
            )
    # Compute holding days
    if trade.exit_at is not None:
        trade.holding_days = max(1, (trade.exit_at - trade.entry_at).days)
    if create_graph:
        trade.graph_path = str(
            create_trade_graph(
                trade,
                bars=graph_bars or bars,
                probabilities=probabilities,
                simulation_end=self.config.end,
                event_end=evaluation_event_end if strategy_branch == "machine_learning" else None,
                graph_dir=self.run_dir / "graphs" / "trades",
            )
        )
    return trade, {
        **decision,
        "opened": True,
        "reason": "opened",
        "trade_id": str(trade.trade_id),
    }


def _close_at_or_before(bars: list[PriceBar], ts: datetime) -> float | None:
    value = None
    for bar in bars:
        if bar.timestamp <= ts:
            value = bar.close
        else:
            break
    return value


async def attach_sector_hedge(
    conn: Any,
    trade: Trade,
    *,
    sector_etf_map: dict[str, str],
    bars_cache: dict[str, list[PriceBar]],
    fallback_symbol: str,
    trade_notional: float,
    window_start: datetime,
    window_end: datetime,
) -> None:
    """Attach an opposite-side, equal-notional sector-ETF hedge leg (entered/exited with the
    trade) so the booked net_profit becomes the sector-hedged idiosyncratic move. Tries the
    asset's sector ETF, then the fallback (SPY). No-op if no ETF price is available."""
    if trade.exit_at is None or trade.entry_at is None:
        return
    symbol = trade.symbol.upper()
    for etf in (sector_etf_map.get(symbol), fallback_symbol):
        if not etf or etf == symbol:
            continue
        bars = bars_cache.get(etf)
        if bars is None:
            bars = await price_bars(
                conn, symbol=etf, resolution="1d", start=window_start, end=window_end
            )
            bars_cache[etf] = bars
        entry_close = _close_at_or_before(bars, trade.entry_at)
        exit_close = _close_at_or_before(bars, trade.exit_at)
        if entry_close and exit_close and entry_close > 0:
            quantity = trade_notional / entry_close
            trade.hedge_symbol = etf
            trade.hedge_entry_price = entry_close
            trade.hedge_exit_price = exit_close
            trade.hedge_quantity = quantity
            trade.hedge_commission = (
                ib_style_commission(quantity, trade_notional)
                + ib_style_commission(quantity, quantity * exit_close)
            )
            beneficiary_net = (
                (trade.gross_profit or 0.0) - trade.entry_commission - trade.exit_commission
            )
            trade.parameter_selection = {
                **(trade.parameter_selection or {}),
                "hedge": {
                    "symbol": etf,
                    "entry_price": entry_close,
                    "exit_price": exit_close,
                    "leg_net": trade.hedge_net_profit,
                    "beneficiary_net": beneficiary_net,
                },
            }
            return


async def run(self, conn: Any) -> None:
    events = {event.event_id: event for event in await run_events(self, conn)}
    markets = {market.market_id: market for market in await accepted_markets(self, conn)}
    observations_rows = await conn.fetch(
        """
        SELECT * FROM checking_relevant_events.historical_ml_observations
        WHERE run_id = $1
        """,
        self.run_id,
    )
    observations = {
        (row["event_id"], row["symbol"]): MLObservation(
            row["observation_id"], row["run_id"], row["event_id"], row["market_id"],
            row["first_pass_number"], row["first_pass_at"], row["label_available_at"], row["symbol"],
            row["event_archetype"], row["resolution"], json_value(row["features"]), json_value(row["research_data"]),
            row["classification_target"], row["regression_target"],
            row["valid_for_training"], row["exclusion_reason"],
        )
        for row in observations_rows
    }
    semantic_long_only = (
        self.config.semantic_ml_grouping_version == SEMANTIC_GROUPING_VERSION
    )
    daily_close_policy = self.config.asset_price_policy == "daily_close_to_close"
    # ML branch exits on the Ridge predicted-target lock, one day before event end, or a
    # stop. The stop is either a fixed fractional hard stop (config.ml_stop_loss_pct,
    # negative range_multiplier) or a close-volatility trailing stop (ml_vol_stop_multiplier,
    # positive range_multiplier) when ml_stop_mode == "vol_trail".
    ml_range_multiplier = (
        abs(self.config.ml_vol_stop_multiplier)
        if self.config.ml_stop_mode == "vol_trail"
        else -abs(self.config.ml_stop_loss_pct)
    )
    ml_strategy = EventDrivenStrategy(
        trade_notional=self.config.trade_notional,
        range_period=self.config.trailing_range_bars,
        range_multiplier=ml_range_multiplier,
    )
    event_symbol_trade_counts = await primary_event_symbol_trade_counts(
        conn,
        self.run_id,
    )
    # Sector-ETF hedge (config.hedge_ml_trades): preload the symbol -> sector ETF map once;
    # per-ETF daily bars are fetched lazily and cached across trades.
    hedge_enabled = self.config.hedge_ml_trades
    sector_etf_map: dict[str, str] = {}
    hedge_bars_cache: dict[str, list[PriceBar]] = {}
    if hedge_enabled:
        sector_etf_map = {
            row["symbol"].upper(): row["sector_etf"]
            for row in await conn.fetch(
                "SELECT symbol, sector_etf FROM checking_relevant_events.historical_asset_metadata "
                "WHERE sector_etf IS NOT NULL"
            )
        }
    hedge_window_start = self.config.start - timedelta(days=10)
    hedge_window_end = self.config.end + timedelta(days=5)
    # Counterfactual accumulator (analysis only, never persisted): net P&L if every
    # ML-eligible candidate were traded long-only, ignoring the directional classifier.
    counterfactual_long_only: list[float] = []
    for row in await run_resolved_world_assets(conn, self.run_id):
        work_key = f"{row['market_id']}:{row['pass_number']}:{row['symbol']}"
        self.current_work_key = work_key
        if not await start_work(
            conn,
            run_id=self.run_id,
            stage="simulation",
            work_key=work_key,
            payload={"market_id": row["market_id"], "pass_number": row["pass_number"], "symbol": row["symbol"]},
        ):
            continue
        market = markets[row["market_id"]]
        event = events[market.event_id]
        asset = Asset(row["symbol"], row["asset_name"], row["asset_class"], row["reason"])
        semantic_assignment = (
            classify_assignment(
                market.question,
                symbol=asset.symbol,
                asset_name=asset.asset_name,
                event_title=market.event_title,
                tags=market.tags,
            )
            if semantic_long_only
            else None
        )
        if semantic_assignment is not None and not semantic_assignment.long_eligible:
            await finish_work(
                conn,
                run_id=self.run_id,
                stage="simulation",
                work_key=work_key,
                result={
                    "model_status": "semantic_long_only_excluded",
                    "trades_opened": 0,
                    "semantic_group": semantic_assignment.group,
                    "yes_outcome_polarity": semantic_assignment.yes_outcome_polarity,
                    "entry_decisions": [
                        {
                            "strategy_branch": "none",
                            "opened": False,
                            "reason": "semantic_group_not_positive_long_beneficiary",
                            "semantic_group": semantic_assignment.group,
                            "semantic_reason": semantic_assignment.reason,
                        }
                    ],
                },
            )
            continue
        # ---- duplicate event+symbol suppression ----
        es_key = (market.event_id, asset.symbol.upper())
        existing_count = event_symbol_trade_counts.get(es_key, 0)
        primary_trade_suppressed = (
            self.config.max_trades_per_event_symbol is not None
            and existing_count >= self.config.max_trades_per_event_symbol
        )
        # ---- resolution selection ----
        if daily_close_policy:
            resolution = "1d"
        else:
            resolution = "1h" if row["as_of"] >= self.hourly_boundary else "1d"
        warmup = timedelta(days=14 if resolution == "1h" else 45)
        bar_start = row["as_of"] - warmup
        if resolution == "1h":
            bar_start = max(bar_start, self.hourly_boundary)
        graph_end = market.end_at + timedelta(days=4)
        available_bars = await price_bars(
            conn,
            symbol=asset.symbol,
            resolution=resolution,
            start=bar_start,
            end=graph_end,
        )
        trade_end = min(self.config.end, market.end_at)
        bars = [bar for bar in available_bars if bar.timestamp < trade_end]
        entry_bar = next_bar_after(bars, row["as_of"])
        minimum_pre_entry_bars = max(self.config.momentum_lookback_grid) + 1
        if (
            resolution == "1h"
            and (
                entry_bar is None
                or len(bars_before(bars, entry_bar.timestamp)) < minimum_pre_entry_bars
            )
        ):
            daily_bars = await price_bars(
                conn,
                symbol=asset.symbol,
                resolution="1d",
                start=row["as_of"] - timedelta(days=45),
                end=graph_end,
            )
            daily_entry = next_bar_after(daily_bars, row["as_of"])
            if (
                daily_entry is not None
                and len(bars_before(daily_bars, daily_entry.timestamp))
                >= minimum_pre_entry_bars
            ):
                resolution = "1d"
                available_bars = daily_bars
                bars = [bar for bar in available_bars if bar.timestamp < trade_end]
        probabilities = await probability_history(
            conn,
            market_id=market.market_id,
            start=market.created_at,
            end=min(self.config.end, market.end_at),
        )
        volume_quality = polymarket_volume_quality(
            probabilities,
            trigger_at=row["as_of"],
            minimum_pre_entry_usdc=self.config.polymarket_volume_minimum_pre_entry_usdc,
            concentration_minimum_usdc=self.config.polymarket_volume_concentration_minimum_usdc,
            max_single_hour_share=self.config.polymarket_volume_max_single_hour_share,
        )
        observation = observations.get((market.event_id, asset.symbol))
        snapshot = None
        if observation is not None:
            prior = await prior_ml_observations(
                conn,
                run_id=self.run_id,
                symbol=asset.symbol,
                event_archetype=observation.event_archetype,
                before=row["as_of"],
            )
            snapshot = train_snapshot(
                run_id=self.run_id,
                symbol=asset.symbol,
                event_archetype=observation.event_archetype,
                training_cutoff=row["as_of"],
                observations=prior,
                minimum_prior_observations=self.config.minimum_ml_prior_observations,
                minimum_prior_events=(
                    self.config.minimum_ml_prior_events if semantic_long_only else 0
                ),
                prediction_features=observation.features,
                feature_names=active_feature_names(
                    self.config.ml_use_query_window_features
                ),
            )
            snapshot.snapshot_id = await save_model_snapshot(conn, snapshot)
        opened = 0
        entry_decisions: list[dict[str, Any]] = []
        ml_untrainable = snapshot is None or snapshot.status in {
            "insufficient_history",
            "insufficient_event_history",
            "insufficient_class_diversity",
        }
        if snapshot is not None and snapshot.status == "trained":
            entry_bar = next_bar_after(bars, row["as_of"])
            event_open_price = (
                observation.research_data.get("anchor_close_price")
                or observation.research_data.get("event_open_price")
            )
            prediction = (
                predict(
                    snapshot,
                    run_id=self.run_id,
                    market_id=market.market_id,
                    event_id=market.event_id,
                    pass_number=row["pass_number"],
                    symbol=asset.symbol,
                    features=observation.features,
                    event_open_price=float(event_open_price),
                    realized_price_at_entry=float(entry_bar.open),
                    direction_mode=self.config.ml_direction_mode,
                )
                if entry_bar and event_open_price
                else None
            )
            if prediction:
                if event.end_at <= self.config.historical_data_cutoff:
                    event_bars = await price_bars(
                        conn,
                        symbol=asset.symbol,
                        resolution="1d",
                        start=event.created_at,
                        end=event.end_at + timedelta(days=1),
                    )
                    event_bars = [
                        bar
                        for bar in event_bars
                        if row["as_of"] < bar.timestamp + DAILY_SESSION_LENGTH
                        and bar.timestamp + DAILY_SESSION_LENGTH <= event.end_at
                    ]
                    evaluate_prediction(
                        prediction,
                        event_open_price=float(event_open_price),
                        bars_until_event_end=event_bars,
                    )
                await save_ml_prediction(conn, prediction)
                prediction_log = {
                    "direction": prediction.direction,
                    "classification_probability": prediction.classification_probability,
                    "predicted_peak_percent": prediction.predicted_peak_percent,
                    "predicted_target_price": prediction.predicted_target_price,
                    "realized_move_at_entry": prediction.realized_move_at_entry,
                    "remaining_gap": prediction.remaining_gap,
                    "directions_agree": prediction.directions_agree,
                    "target_reached": prediction.target_reached,
                }
                # Counterfactual (analysis only, not persisted): trade this ML-eligible
                # candidate LONG with the 3% hard stop + one-day-before-resolution exit,
                # ignoring the directional classifier/target/gap gating. Measures what the
                # directional classifier actually adds. No DB write, no graph.
                cf_anchor_bar = latest_completed_daily_bar(bars, row["as_of"])
                cf_trade, _ = await simulate_one_trade(
                    self,
                    market=market,
                    asset=asset,
                    pass_number=row["pass_number"],
                    trigger_at=row["as_of"],
                    portfolio="counterfactual_long_only",
                    strategy_branch="machine_learning",
                    strategy=ml_strategy,
                    direction="long",
                    resolution=resolution,
                    bars=bars,
                    graph_bars=available_bars,
                    probabilities=probabilities,
                    predicted_target_price=None,
                    evaluation_event_end=market.end_at,
                    create_graph=False,
                    anchor_close_at=cf_anchor_bar.timestamp if cf_anchor_bar else None,
                    anchor_close_price=cf_anchor_bar.close if cf_anchor_bar else None,
                )
                if cf_trade is not None:
                    counterfactual_long_only.append(float(cf_trade.net_profit or 0.0))
                semantic_blocked_reason = None
                price_confirmation_momentum = None
                # When shorts are enabled we trust the ML direction both ways and skip the
                # long-only short-rejection and the long-only price-confirmation veto.
                if semantic_long_only and not self.config.enable_shorts:
                    if prediction.direction != "long":
                        semantic_blocked_reason = "semantic_long_only_rejects_short_prediction"
                    else:
                        completed_before_entry = (
                            bars_before(bars, entry_bar.timestamp) if entry_bar else []
                        )
                        price_confirmation_momentum = rate_of_change(
                            completed_before_entry,
                            self.config.momentum_lookback_bars,
                        )
                        if price_confirmation_momentum is None:
                            semantic_blocked_reason = (
                                "insufficient_daily_price_confirmation_history"
                                if daily_close_policy
                                else "insufficient_hourly_price_confirmation_history"
                            )
                        elif price_confirmation_momentum <= 0:
                            semantic_blocked_reason = (
                                "daily_price_momentum_not_positive"
                                if daily_close_policy
                                else "hourly_price_momentum_not_positive"
                            )
                    prediction_log["price_confirmation_momentum"] = (
                        price_confirmation_momentum
                    )
                if (
                    semantic_blocked_reason is None
                    and prediction.directions_agree
                    and prediction.remaining_gap > 0
                ):
                    # Compute anchor close for ML under daily policy
                    ml_anchor_bar = latest_completed_daily_bar(bars, row["as_of"])
                    ml_anchor_at = ml_anchor_bar.timestamp if ml_anchor_bar else None
                    ml_anchor_price = ml_anchor_bar.close if ml_anchor_bar else None
                    trade, decision = await simulate_one_trade(
                        self,
                        market=market,
                        asset=asset,
                        pass_number=row["pass_number"],
                        trigger_at=row["as_of"],
                        portfolio="machine_learning",
                        strategy_branch="machine_learning",
                        strategy=ml_strategy,
                        direction=prediction.direction,
                        resolution=resolution,
                        bars=bars,
                        graph_bars=available_bars,
                        probabilities=probabilities,
                        predicted_target_price=prediction.predicted_target_price,
                        predicted_target_reached=prediction.target_reached,
                        evaluation_event_end=market.end_at,
                        anchor_close_at=ml_anchor_at,
                        anchor_close_price=ml_anchor_price,
                    )
                    decision.update(prediction_log)
                    decision["polymarket_volume_statistics"] = volume_quality
                    entry_decisions.append(decision)
                    if trade:
                        if primary_trade_suppressed:
                            await increment_duplicate_suppression_count(
                                conn,
                                run_id=self.run_id,
                                event_id=market.event_id,
                                symbol=asset.symbol,
                            )
                            decision.update(
                                {
                                    "opened": False,
                                    "reason": "duplicate_event_symbol_suppressed",
                                    "existing_trades_for_event_symbol": existing_count,
                                }
                            )
                        else:
                            trade.duplicate_suppression_count = 0
                            if hedge_enabled:
                                await attach_sector_hedge(
                                    conn,
                                    trade,
                                    sector_etf_map=sector_etf_map,
                                    bars_cache=hedge_bars_cache,
                                    fallback_symbol=self.config.hedge_fallback_symbol,
                                    trade_notional=self.config.trade_notional,
                                    window_start=hedge_window_start,
                                    window_end=hedge_window_end,
                                )
                            await save_trade(conn, trade)
                            event_symbol_trade_counts[es_key] = existing_count + 1
                            opened += 1
                else:
                    entry_decisions.append(
                        {
                            "portfolio": "machine_learning",
                            "strategy_branch": "machine_learning",
                            "opened": False,
                            "reason": (
                                semantic_blocked_reason
                                or (
                                    "classifier_and_ridge_directions_disagree"
                                    if not prediction.directions_agree
                                    else "remaining_predicted_gap_not_positive"
                                )
                            ),
                            **prediction_log,
                            "polymarket_volume_statistics": volume_quality,
                        }
                    )
            else:
                entry_decisions.append(
                    {
                        "portfolio": "machine_learning",
                        "strategy_branch": "machine_learning",
                        "opened": False,
                        "reason": "missing_prediction_input",
                        "has_entry_bar": entry_bar is not None,
                        "has_event_open_price": event_open_price is not None,
                        "polymarket_volume_statistics": volume_quality,
                    }
                )
        elif ml_untrainable and self.config.fallback_strategy == "none":
            entry_decisions.append(
                {
                    "portfolio": "fallback",
                    "strategy_branch": "none",
                    "opened": False,
                    "reason": "fallback_disabled_no_ml_model",
                    "polymarket_volume_statistics": volume_quality,
                }
            )
        elif ml_untrainable and self.config.fallback_strategy in {
            "event_window_long",
            "hold_to_resolution",  # legacy alias
        }:
            # No ML model yet: take the prediction-market signal at face value -- enter long
            # on the crossing and hold the event window, exited at the max_holding_days cap /
            # one-day-before resolution / probability fall-below / stop, whichever is first.
            # No ROC reversal exit (that exit is what makes the momentum branch lose money).
            fb_multiplier = (
                abs(self.config.trailing_stop_close_volatility_multiplier)
                if self.config.fallback_stop_mode == "vol_trail"
                else -abs(self.config.fallback_stop_loss_pct)
            )
            fb_strategy = EventDrivenStrategy(
                trade_notional=self.config.trade_notional,
                range_period=self.config.trailing_range_bars,
                range_multiplier=fb_multiplier,
            )
            fb_anchor = latest_completed_daily_bar(bars, row["as_of"])
            trade, decision = await simulate_one_trade(
                self,
                market=market,
                asset=asset,
                pass_number=row["pass_number"],
                trigger_at=row["as_of"],
                portfolio="fallback_long",
                strategy_branch="fallback_long",
                direction="long",
                resolution=resolution,
                bars=bars,
                graph_bars=available_bars,
                probabilities=probabilities,
                strategy=fb_strategy,
                evaluation_event_end=market.end_at,
                anchor_close_at=fb_anchor.timestamp if fb_anchor else None,
                anchor_close_price=fb_anchor.close if fb_anchor else None,
            )
            decision["polymarket_volume_statistics"] = volume_quality
            entry_decisions.append(decision)
            if trade:
                if primary_trade_suppressed:
                    await increment_duplicate_suppression_count(
                        conn,
                        run_id=self.run_id,
                        event_id=market.event_id,
                        symbol=asset.symbol,
                    )
                    decision.update(
                        {
                            "opened": False,
                            "reason": "duplicate_event_symbol_suppressed",
                            "existing_trades_for_event_symbol": existing_count,
                        }
                    )
                else:
                    trade.duplicate_suppression_count = 0
                    await save_trade(conn, trade)
                    event_symbol_trade_counts[es_key] = existing_count + 1
                    opened += 1
        elif ml_untrainable:
            entry_bar = next_bar_after(bars, row["as_of"])
            completed_before_entry = (
                bars_before(bars, entry_bar.timestamp) if entry_bar else []
            )
            selection = await select_walk_forward_momentum_parameters(
                conn,
                run_id=self.run_id,
                before=row["as_of"],
                resolution=resolution,
                minimum_samples=self.config.momentum_walk_forward_minimum_samples,
                fallback_period=self.config.momentum_lookback_bars,
                fallback_multiplier=(
                    self.config.trailing_stop_close_volatility_multiplier
                    if daily_close_policy
                    else self.config.trailing_range_multiplier
                ),
            )
            # ---- walk-forward abstention ----
            walk_forward_abstain = not bool(selection.get("should_trade", True))
            selected_period = int(selection["range_period"])
            selected_multiplier = float(selection["range_multiplier"])
            selected_trade: Trade | None = None
            selected_decision: dict[str, Any] | None = None
            shadow_results: list[dict[str, object]] = []
            for momentum_lookback in self.config.momentum_lookback_grid:
                momentum = rate_of_change(completed_before_entry, momentum_lookback)
                for range_multiplier in self.config.trailing_range_multiplier_grid:
                    variant = f"n{momentum_lookback}_k{range_multiplier:g}"
                    blocked_reason = None
                    if self.config.momentum_entry_mode == "none":
                        blocked_reason = "momentum_disabled"
                    elif momentum is None:
                        blocked_reason = "insufficient_price_momentum_history"
                    elif self.config.momentum_entry_mode == "random":
                        # Null baseline: seeded coin flip in place of the ROC gate, to
                        # test whether the rate-of-change signal beats random entry.
                        coin = random.Random(
                            f"{self.run_id}:{market.market_id}:{asset.symbol}"
                            f":{momentum_lookback}:{range_multiplier}"
                        ).random()
                        if coin >= 0.5:
                            blocked_reason = "random_walk_no_entry"
                    elif momentum <= 0:
                        blocked_reason = "price_momentum_not_positive"
                    if blocked_reason:
                        shadow_results.append(
                            {
                                "run_id": self.run_id,
                                "market_id": market.market_id,
                                "event_id": market.event_id,
                                "pass_number": row["pass_number"],
                                "symbol": asset.symbol,
                                "trigger_at": row["as_of"],
                                "resolution": resolution,
                                "range_period": momentum_lookback,
                                "range_multiplier": range_multiplier,
                                "opened": False,
                                "reason": blocked_reason,
                                "net_profit": None,
                                "momentum_value": momentum,
                                "passes_five_percent": (
                                    momentum is not None and momentum >= 0.05
                                ),
                                "passes_ten_percent": (
                                    momentum is not None and momentum >= 0.10
                                ),
                            }
                        )
                        if (
                            momentum_lookback == selected_period
                            and range_multiplier == selected_multiplier
                        ):
                            selected_decision = {
                                "portfolio": "polymarket_momentum",
                                "strategy_branch": "momentum",
                                "opened": False,
                                "reason": blocked_reason,
                                "price_momentum": momentum,
                            }
                        continue
                    strategy = EventDrivenStrategy(
                        trade_notional=self.config.trade_notional,
                        range_period=momentum_lookback,
                        range_multiplier=range_multiplier,
                    )
                    # Compute anchor close for momentum under daily policy
                    mom_anchor_at = None
                    mom_anchor_price = None
                    if daily_close_policy:
                        anchor_bar = latest_completed_daily_bar(bars, row["as_of"])
                        if anchor_bar:
                            mom_anchor_at = anchor_bar.timestamp
                            mom_anchor_price = anchor_bar.close
                    trade, decision = await simulate_one_trade(
                        self,
                        market=market,
                        asset=asset,
                        pass_number=row["pass_number"],
                        trigger_at=row["as_of"],
                        portfolio=f"momentum_shadow_{variant}",
                        strategy_branch="momentum",
                        direction="long",
                        resolution=resolution,
                        bars=bars,
                        graph_bars=available_bars,
                        probabilities=probabilities,
                        strategy=strategy,
                        momentum_lookback=momentum_lookback,
                        create_graph=False,
                        anchor_close_at=mom_anchor_at,
                        anchor_close_price=mom_anchor_price,
                    )
                    shadow_results.append(
                        {
                            "run_id": self.run_id,
                            "market_id": market.market_id,
                            "event_id": market.event_id,
                            "pass_number": row["pass_number"],
                            "symbol": asset.symbol,
                            "trigger_at": row["as_of"],
                            "resolution": resolution,
                            "range_period": momentum_lookback,
                            "range_multiplier": range_multiplier,
                            "opened": trade is not None,
                            "reason": decision["reason"],
                            "net_profit": trade.net_profit if trade else None,
                            "momentum_value": momentum,
                            "passes_five_percent": momentum >= 0.05,
                            "passes_ten_percent": momentum >= 0.10,
                        }
                    )
                    if (
                        momentum_lookback == selected_period
                        and range_multiplier == selected_multiplier
                    ):
                        selected_trade = trade
                        selected_decision = decision
                        if selected_trade:
                            selected_trade.portfolio = "polymarket_momentum"
                            selected_trade.parameter_selection = dict(selection)
            await save_momentum_parameter_results(conn, shadow_results)
            if selected_decision is None:
                selected_decision = {
                    "portfolio": "polymarket_momentum",
                    "strategy_branch": "momentum",
                    "opened": False,
                    "reason": "selected_parameters_not_in_configured_grid",
                }
            selected_decision.update(
                {
                    "portfolio": "polymarket_momentum",
                    "price_momentum": rate_of_change(completed_before_entry, selected_period),
                    "momentum_lookback": selected_period,
                    "range_multiplier": selected_multiplier,
                    "parameter_selection": selection,
                    "polymarket_volume_statistics": volume_quality,
                }
            )
            entry_decisions.append(selected_decision)
            if selected_trade:
                if walk_forward_abstain or primary_trade_suppressed:
                    if primary_trade_suppressed and not walk_forward_abstain:
                        await increment_duplicate_suppression_count(
                            conn,
                            run_id=self.run_id,
                            event_id=market.event_id,
                            symbol=asset.symbol,
                        )
                    selected_decision.update(
                        {
                            "opened": False,
                            "reason": (
                                str(selection.get(
                                    "selection_method", "walk_forward_abstention"
                                ))
                                if walk_forward_abstain
                                else "duplicate_event_symbol_suppressed"
                            ),
                            "existing_trades_for_event_symbol": existing_count,
                        }
                    )
                else:
                    selected_trade.duplicate_suppression_count = 0
                    selected_trade.graph_path = str(
                        create_trade_graph(
                            selected_trade,
                            bars=available_bars,
                            probabilities=probabilities,
                            simulation_end=self.config.end,
                            graph_dir=self.run_dir / "graphs" / "trades",
                        )
                    )
                    await save_trade(conn, selected_trade)
                    event_symbol_trade_counts[es_key] = existing_count + 1
                    opened += 1
        else:
            entry_decisions.append(
                {
                    "strategy_branch": "none",
                    "opened": False,
                    "reason": f"model_state_{snapshot.status}",
                }
            )
        await finish_work(
            conn,
            run_id=self.run_id,
            stage="simulation",
            work_key=work_key,
            result={
                "model_status": snapshot.status if snapshot else "not_ml_eligible",
                "trades_opened": opened,
                "semantic_group": (
                    semantic_assignment.group if semantic_assignment is not None else None
                ),
                "yes_outcome_polarity": (
                    semantic_assignment.yes_outcome_polarity
                    if semantic_assignment is not None
                    else None
                ),
                "entry_decisions": entry_decisions,
            },
        )

    # --- Counterfactual report (analysis only, not persisted to the DB) ---
    # "What if we did not use the directional classifier?" Every ML-eligible candidate
    # traded long-only with the 3% stop + one-day-before-resolution exit.
    cf = counterfactual_long_only
    cf_wins = [value for value in cf if value > 0]
    cf_losses = [value for value in cf if value < 0]
    cf_summary = {
        "description": (
            "Analysis only (not persisted). Every ML-eligible candidate traded LONG with a "
            "3% hard stop and a one-day-before-resolution exit, ignoring the directional "
            "classifier and its gating. Compare against strategies.machine_learning in summary.json."
        ),
        "trade_count": len(cf),
        "total_net_profit": sum(cf),
        "win_rate": (len(cf_wins) / len(cf)) if cf else None,
        "average_trade": (sum(cf) / len(cf)) if cf else None,
        "profit_factor": (sum(cf_wins) / abs(sum(cf_losses))) if cf_losses else None,
        "gross_profit": sum(cf_wins),
        "gross_loss": sum(cf_losses),
    }
    report_dir = self.run_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    (report_dir / "counterfactual_long_only_ml.json").write_text(
        json.dumps(cf_summary, indent=2), encoding="utf-8"
    )
    print(
        f"[counterfactual long-only ML] trades={cf_summary['trade_count']} "
        f"net={cf_summary['total_net_profit']:.2f} "
        f"win_rate={(cf_summary['win_rate'] or 0):.3f}"
    )
