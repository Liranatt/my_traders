from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BacktestConfig:
    start: datetime = datetime(2022, 1, 1, tzinfo=timezone.utc)
    end: datetime = datetime(2026, 6, 2, tzinfo=timezone.utc)
    maximum_events: int = 0
    selected_event_ids: tuple[str, ...] = ()
    pipeline_start_stage: str = "event_filter"
    threshold: float = 0.55
    minimum_days_remaining: float = 5.0
    maximum_days_remaining: float = 60.0
    trade_notional: float = 1_000.0
    # Bankroll used to express results as a percent return. The strategy deploys
    # ``trade_notional`` per signal; peak concurrent deployment is ~$36k historically, so
    # $50k gives headroom. A fixed value keeps the percent comparable across sweep variants.
    starting_capital: float = 50_000.0
    asset_price_policy: str = "daily_close_to_close"
    max_trades_per_event_symbol: int = 1
    trailing_stop_close_volatility_multiplier: float = 3.0
    ml_stop_loss_pct: float = 0.03
    # ML stop style. "fixed" = a fixed fractional hard stop at ``ml_stop_loss_pct`` from
    # entry (never trails -- the tight 3% default that whipsaws out ~104 trades at 4% win).
    # "vol_trail" = a close-to-close-volatility trailing stop at ``ml_vol_stop_multiplier``
    # (adaptive: wider on noisy names, tighter on calm ones -- "a stop, but not too harsh").
    ml_stop_mode: str = "fixed"
    ml_vol_stop_multiplier: float = 3.0
    trailing_range_bars: int = 14
    trailing_range_multiplier: float = 3.0
    momentum_lookback_bars: int = 14
    momentum_lookback_grid: tuple[int, ...] = (5, 7, 12, 14, 18, 21)
    # Positive entries are ATR / close-volatility multipliers (k, "k-fold tune k");
    # negative entries are fixed fractional hard stops from entry (-0.03 = 3% max loss,
    # "shadow test"). The momentum branch walk-forward-selects across both families.
    trailing_range_multiplier_grid: tuple[float, ...] = (
        1.5, 2.0, 2.5, 3.0, -0.01, -0.02, -0.03, -0.05,
    )
    momentum_walk_forward_minimum_samples: int = 30
    polymarket_volume_lookback_hours: int = 24
    polymarket_volume_minimum_history_hours: int = 6
    polymarket_volume_confirmation_ratio: float = 1.0
    polymarket_volume_minimum_pre_entry_usdc: float = 10.0
    polymarket_volume_concentration_minimum_usdc: float = 1_000.0
    polymarket_volume_max_single_hour_share: float = 0.95
    news_lookback: timedelta = timedelta(hours=72)
    max_articles: int = 9
    probability_chunk_days: int = 10
    minimum_ml_prior_observations: int = 14
    minimum_ml_prior_events: int = 30
    # ML direction source: "classifier" (logistic + ridge agreement gate, production) or
    # "regression_sign" (direction = sign of the Ridge predicted peak; no classifier gate).
    ml_direction_mode: str = "classifier"
    # Act on SHORT predictions. Off = long-only (the semantic gate rejects short calls and
    # requires positive price-confirmation momentum). On = take the ML direction both ways
    # (long the longs, short the shorts) within the recognized beneficiary universe.
    enable_shorts: bool = False
    # Hedge every ML trade with an opposite-side, equal-notional position in the asset's
    # sector ETF (fallback ``hedge_fallback_symbol``). Booked P&L becomes the idiosyncratic,
    # sector-hedged move -- the alpha the paper targets, with index/sector beta removed.
    hedge_ml_trades: bool = False
    hedge_fallback_symbol: str = "SPY"
    # Add the query-window features (price run-up since query creation, and where in the
    # query's life the 55% crossing fired) to the ML model. On by default: the 20260623
    # sweep showed a clean single-variable lift (net 883 -> 1135, accuracy 0.441 -> 0.453).
    ml_use_query_window_features: bool = True
    # Momentum entry gate: "roc" (rate-of-change > 0, production), "random" (seeded coin
    # flip -- null baseline to test whether the ROC signal beats random), or "none".
    momentum_entry_mode: str = "roc"
    # What to do for candidates that cannot train an ML model yet (insufficient history).
    #   "momentum"          -> the walk-forward momentum branch (ROC entry + ROC-reversal
    #                          exit). The reversal exit is the loser: -472 over 27 trades at
    #                          15% win, while the same trades that held the event window
    #                          (capped, see max_holding_days) made +796.
    #   "event_window_long" -> enter long on the crossing and hold the event window -- exit
    #                          at the max_holding_days cap, one-day-before resolution,
    #                          probability fall-below, or the stop; whichever comes first.
    #                          No ROC reversal exit. ("hold_to_resolution" is a legacy alias.)
    #   "none"              -> skip these candidates entirely (ML-only book).
    fallback_strategy: str = "momentum"
    # Stop for the event_window_long fallback. "vol_trail" trails on close volatility at
    # ``trailing_stop_close_volatility_multiplier``; "fixed" is a hard ``fallback_stop_loss_pct``.
    fallback_stop_mode: str = "vol_trail"
    fallback_stop_loss_pct: float = 0.05
    # Hard cap on how long ANY trade (ML, momentum, fallback) is held, in calendar days from
    # entry. 0 = no cap (horizon left to the predicted-target lock / one-day-before-end /
    # stop). The trade edge is event-driven and front-loaded, so a tight cap protects capital
    # efficiency and the percent return; sweep it rather than leaving the horizon implicit.
    max_holding_days: int = 0
    # --- Probability fall-below exit (experimental; default "off" = current behavior) ---
    # While in a trade, if the Polymarket probability falls back below the band:
    #   "sustained_exit"    -> exit once it stays below the band for the confirmation window
    #   "tighten_breakeven" -> move the stop to entry (break-even) instead of exiting
    #   "immediate_band"    -> exit on the first bar below the band (no confirmation window)
    probability_exit_mode: str = "off"
    probability_exit_band: float = 0.50
    probability_exit_confirmation_hours: float = 48.0
    # --- Entry crossing confirmation (experimental; default off when hours <= 0) ---
    # Crossings >= entry_strong_probability enter immediately. Marginal crossings (between
    # `threshold` and entry_strong_probability) must hold above `threshold` for
    # entry_confirmation_hours, otherwise the crossing is treated as a transient spike.
    entry_confirmation_hours: float = 0.0
    entry_strong_probability: float = 0.60
    gdelt_concurrency: int = 8
    gdelt_minimum_request_interval_seconds: float = 5.5
    article_download_concurrency: int = 12
    price_download_concurrency: int = 4
    event_filter_batch_size: int = 25
    asset_world_batch_size: int = 20
    ollama_sentiment_batch_size: int = 1
    finbert_batch_size: int = 32
    historical_data_cutoff: datetime = datetime(2026, 6, 2, tzinfo=timezone.utc)
    event_filter_prompt_version: str = "historical-market-filter-v3"
    asset_world_model: str = "gemini-3.5-flash"
    asset_world_thinking_level: str = "low"
    asset_world_prompt_version: str = "historical-pass-world-v9-reasoning-no-ticker-leakage"
    semantic_ml_grouping_version: str = "semantic-outcome-role-v1"
    ollama_sentiment_prompt_version: str = "historical-sentiment-v1"
    output_root: Path = REPO_ROOT / "main_backtesting" / "output" / "runs"
    included_tags: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "ai",
                "big-tech",
                "business",
                "china",
                "commodities",
                "economy",
                "economic-policy",
                "earnings",
                "equities",
                "fda",
                "fed",
                "fed-rates",
                "finance",
                "foreign-policy",
                "gdp",
                "geopolitics",
                "inflation",
                "iran",
                "israel",
                "jobs",
                "kpis",
                "macro-indicators",
                "middle-east",
                "military-action",
                "nfp",
                "oil",
                "politics",
                "real-estate",
                "russia",
                "stocks",
                "strait-of-hormuz",
                "tech",
                "trade-war",
                "ukraine",
                "unemployment",
                "world",
            }
        )
    )
    excluded_tags: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                "bitcoin",
                "crypto",
                "crypto-prices",
                "daily",
                "daily-close",
                "finance-updown",
                "hit-price",
                "multi-strikes",
                "pyth-finance",
                "recurring",
                "sports",
                "stock-prices",
                "today",
                "up-or-down",
                "weekly",
            }
        )
    )

    def run_dir(self, run_id: object) -> Path:
        return self.output_root / str(run_id)

    def to_json(self) -> dict[str, Any]:
        values = asdict(self)
        for key, value in values.items():
            if isinstance(value, Path):
                values[key] = str(value)
            elif isinstance(value, frozenset):
                values[key] = sorted(value)
            elif isinstance(value, timedelta):
                values[key] = value.total_seconds()
            elif isinstance(value, datetime):
                values[key] = value.isoformat()
        return values

    @classmethod
    def from_json(cls, values: dict[str, Any]) -> BacktestConfig:
        parsed = dict(values)
        parsed.setdefault("semantic_ml_grouping_version", "legacy-symbol-archetype-v1")
        parsed["start"] = datetime.fromisoformat(parsed["start"])
        parsed["end"] = datetime.fromisoformat(parsed["end"])
        if "historical_data_cutoff" in parsed:
            parsed["historical_data_cutoff"] = datetime.fromisoformat(
                parsed["historical_data_cutoff"]
            )
        parsed["news_lookback"] = timedelta(seconds=float(parsed["news_lookback"]))
        parsed["output_root"] = Path(parsed["output_root"])
        parsed["included_tags"] = frozenset(parsed["included_tags"])
        parsed["excluded_tags"] = frozenset(parsed["excluded_tags"])
        parsed["selected_event_ids"] = tuple(parsed.get("selected_event_ids", ()))
        parsed.setdefault("pipeline_start_stage", "event_filter")
        parsed["momentum_lookback_grid"] = tuple(
            parsed.get("momentum_lookback_grid", (5, 7, 12, 14, 18, 21))
        )
        parsed["trailing_range_multiplier_grid"] = tuple(
            parsed.get(
                "trailing_range_multiplier_grid",
                (1.5, 2.0, 2.5, 3.0, -0.01, -0.02, -0.03, -0.05),
            )
        )
        parsed.setdefault("ml_stop_loss_pct", 0.03)
        parsed.setdefault("ml_stop_mode", "fixed")
        parsed.setdefault("ml_vol_stop_multiplier", 3.0)
        parsed.setdefault("ml_direction_mode", "classifier")
        parsed.setdefault("enable_shorts", False)
        parsed.setdefault("hedge_ml_trades", False)
        parsed.setdefault("hedge_fallback_symbol", "SPY")
        # Old saved runs predate the query-window feature; keep them reproducible (off),
        # even though new runs default it on.
        parsed.setdefault("ml_use_query_window_features", False)
        parsed.setdefault("momentum_entry_mode", "roc")
        parsed.setdefault("fallback_strategy", "momentum")
        parsed.setdefault("fallback_stop_mode", "vol_trail")
        parsed.setdefault("fallback_stop_loss_pct", 0.05)
        parsed.setdefault("max_holding_days", 0)
        parsed.setdefault("starting_capital", 50_000.0)
        parsed.setdefault("probability_exit_mode", "off")
        parsed.setdefault("probability_exit_band", 0.50)
        parsed.setdefault("probability_exit_confirmation_hours", 48.0)
        parsed.setdefault("entry_confirmation_hours", 0.0)
        parsed.setdefault("entry_strong_probability", 0.60)
        parsed.setdefault("asset_price_policy", "daily_close_to_close")
        parsed.setdefault("max_trades_per_event_symbol", 1)
        parsed.setdefault("trailing_stop_close_volatility_multiplier", 3.0)
        return cls(**parsed)


def hourly_availability_boundary(now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    return (current - timedelta(days=729)).replace(hour=0, minute=0, second=0, microsecond=0)
