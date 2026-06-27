"""Shared hard-exit rules for RL training and portfolio replay."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from rl.config import POLY_EXIT_THRESHOLD
from rl.shared import as_utc_day

_DAY = pd.Timedelta(days=1)
_BAR_CACHE: dict[tuple[int, str], tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = {}


@dataclass(frozen=True)
class PriceBar:
    day: pd.Timestamp
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class HardExitSignal:
    reason: str
    exit_price: float


def bar_on(prices: dict, symbol: str, day: Any) -> PriceBar | None:
    key = (id(prices), symbol)
    cached = _BAR_CACHE.get(key)
    if cached is None:
        bars = prices.get(symbol, [])
        if not bars:
            return None
        days = np.array([as_utc_day(t).value for t, *_ in bars], dtype=np.int64)
        highs = np.asarray([float(bar[1]) for bar in bars], dtype=float)
        lows = np.asarray([float(bar[2]) for bar in bars], dtype=float)
        closes = np.asarray([float(bar[3]) for bar in bars], dtype=float)
        cached = (days, highs, lows, closes)
        _BAR_CACHE[key] = cached

    days, highs, lows, closes = cached
    day_ns = as_utc_day(day).value
    loc = int(np.searchsorted(days, day_ns, side="right")) - 1
    if loc < 0:
        return None
    return PriceBar(
        day=pd.Timestamp(days[loc], tz="UTC"),
        high=float(highs[loc]),
        low=float(lows[loc]),
        close=float(closes[loc]),
    )


def prob_on_day(probs: dict, market_id: str, day: Any) -> float | None:
    target = as_utc_day(day)
    val = None
    for t, p in probs.get(market_id, []):
        if as_utc_day(t) <= target:
            val = float(p)
        else:
            break
    return val


def update_peak_ret(prices: dict, symbol: str, day: Any, entry_price: float, peak_ret: float) -> float:
    bar = bar_on(prices, symbol, day)
    if bar is None or entry_price <= 0:
        return float(peak_ret)
    return float(max(peak_ret, bar.high / entry_price - 1.0))


def evaluate_hard_exit(
    *,
    prices: dict,
    probs: dict,
    symbol: str,
    market_id: str,
    day: Any,
    entry_day: Any,
    entry_price: float,
    t_e: Any,
    resolution_exit_day: Any | None = None,
    poly_exit_threshold: float = POLY_EXIT_THRESHOLD,
    expected_return: float | None = None,
    peak_ret: float | None = None,
) -> HardExitSignal | None:
    day = as_utc_day(day)
    if day <= as_utc_day(entry_day):
        return None

    bar = bar_on(prices, symbol, day)
    if bar is None or entry_price <= 0:
        return None

    if expected_return is not None and expected_return > 0 and peak_ret is not None:
        if peak_ret >= expected_return:
            current_ret = bar.close / entry_price - 1.0
            
            # Dynamic cushion: give the runner 25% of its peak as breathing room (minimum 2% pullback)
            cushion = max(0.02, peak_ret * 0.25)
            drawdown = peak_ret - current_ret
            
            if drawdown >= cushion:
                return HardExitSignal("profit_lock_llm", float(bar.close))
        
    current_prob = prob_on_day(probs, market_id, day)
    if current_prob is not None and current_prob < poly_exit_threshold:
        return HardExitSignal(f"poly<{poly_exit_threshold:g}", float(bar.close))

    resolution_cap = as_utc_day(resolution_exit_day) if resolution_exit_day is not None else as_utc_day(t_e) - _DAY
    if day >= resolution_cap:
        return HardExitSignal("resolution-1d", float(bar.close))

    return None
