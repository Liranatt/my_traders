"""Deterministic synthetic price/probability/candidate builders for RL tests.

No database needed. Timestamps are normalized UTC days, matching the loaders.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

DATES = pd.bdate_range("2024-01-02", periods=140, tz="UTC").normalize()


def make_prices(symbol_drift: dict[str, tuple[float, float]]) -> dict:
    """symbol -> (start_price, daily_drift). Smooth, tiny intraday range."""
    prices = {}
    for sym, (p0, drift) in symbol_drift.items():
        bars = []
        p = p0
        for d in DATES:
            p = p * (1.0 + drift)
            hi = p * 1.002
            lo = p * 0.998
            bars.append((d, round(hi, 2), round(lo, 2), round(p, 2)))
        prices[sym] = bars
    return prices


def make_probs(market_id: str, level: float, start_idx: int) -> dict:
    pts = [(d, float(level)) for i, d in enumerate(DATES) if i >= start_idx]
    return {market_id: pts}


def make_candidate(symbol: str, market_id: str, t_theta_idx: int, t_e_idx: int) -> pd.Series:
    return pd.Series({
        "symbol": symbol,
        "market_id": market_id,
        "t_theta": DATES[t_theta_idx],
        "t_e": DATES[t_e_idx],
        "question": "Will the company beat guidance?",
        "feat_archetype": "macro",
        "feat_connection_strength": 0.9,
        "feat_prob_surge_since_t0": np.nan,
        "feat_runup_since_t0": np.nan,
        "feat_prob_at_trigger": 0.8,
        "feat_time_to_resolution_days": float(t_e_idx - t_theta_idx),
        "feat_spy_2w_trend": 0.01,
        "feat_debt_to_equity": 1.2,
        "expected_return_pct": 8.0,
        "confidence_score": 6.0,
        "feat_llm_expected_return": 0.08,
        "feat_llm_confidence": 6.0,
        "rf_pred_decimal": 0.0,
        "rf_pred_rank": 0.0,
        "split": "test",
    })
