"""Target-aware hindsight labels for RL warm-starting.

The teacher is intentionally weak: it labels only clear ENTER/HOLD/EXIT states
from completed training paths and leaves ambiguous states out of BC.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from rl.config import ACTION_DIM, ACTION_ENTER_OFFSET, ACTION_EXIT, ACTION_HOLD
from rl.features import (
    build_observation,
    compute_market_state,
    compute_position_state,
    entry_signal_day,
    llm_target_from_row,
    prob_on,
    static_features_from_row,
)
from rl.exits import update_peak_ret
from rl.shared import DEFAULT_POLICY, as_utc_day, _calendar_dates, _close_on

_DAY = pd.Timedelta(days=1)


@dataclass
class TeacherBatch:
    obs: np.ndarray
    actions: np.ndarray
    masks: np.ndarray
    counts: dict[str, int] = field(default_factory=dict)

    def __len__(self) -> int:
        return int(self.actions.shape[0])


def _empty_batch(counts: dict[str, int] | None = None) -> TeacherBatch:
    return TeacherBatch(
        obs=np.empty((0, 0), dtype=np.float32),
        actions=np.empty((0,), dtype=np.int64),
        masks=np.empty((0, ACTION_DIM), dtype=bool),
        counts=counts or {},
    )


def _as_path(values: list[float] | np.ndarray) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    return arr[np.isfinite(arr)]


def label_entry_path(active_path: list[float] | np.ndarray, target: float) -> int | None:
    """Return ENTER/HOLD for clear paths, else None for ambiguous states."""
    path = _as_path(active_path)
    if path.size == 0:
        return None
    future_best = float(np.max(path))
    final = float(path[-1])
    clear_target_progress = max(0.02, 0.50 * target)
    if future_best >= clear_target_progress:
        return ACTION_ENTER_OFFSET
    if future_best < 0.015 and final < 0.0:
        return ACTION_HOLD
    return None


def label_exit_path(
    active_path: list[float] | np.ndarray,
    idx: int,
    target: float,
    *,
    current_prob: float | None = None,
    prob_slope_3d: float = 0.0,
    days_since_high: int = 0,
) -> int | None:
    """Return HOLD/EXIT for clear open-position states, else None."""
    path = _as_path(active_path)
    if path.size == 0:
        return None
    idx = max(0, min(int(idx), path.size - 1))
    current = float(path[idx])
    future_best = float(np.max(path[idx:]))
    remaining_upside = future_best - current
    thesis_broken = current_prob is not None and (
        current_prob < 0.50 or (current_prob < 0.55 and prob_slope_3d <= 0.0)
    )
    if thesis_broken:
        return ACTION_EXIT

    meaningful_upside = max(0.01, 0.20 * target)
    near_best_remaining = remaining_upside <= max(0.005, 0.10 * target)
    stalled_below_target = days_since_high >= 4 and prob_slope_3d <= 0.0 and current < target

    if current >= target and near_best_remaining:
        return ACTION_EXIT
    if stalled_below_target:
        return ACTION_EXIT
    if remaining_upside >= meaningful_upside:
        return ACTION_HOLD
    if near_best_remaining:
        return ACTION_EXIT
    return None


def _active_path_for_candidate(
    row: pd.Series,
    prices: dict,
    bench_sym: str,
    dates: list[pd.Timestamp],
) -> np.ndarray:
    entry_price = _close_on(prices, row["symbol"], dates[0])
    bench_entry = _close_on(prices, bench_sym, dates[0])
    if not entry_price or not bench_entry:
        return np.empty((0,), dtype=float)
    out: list[float] = []
    for day in dates:
        asset = _close_on(prices, row["symbol"], day)
        bench = _close_on(prices, bench_sym, day)
        if not asset or not bench:
            out.append(np.nan)
            continue
        out.append(float(asset / entry_price - 1.0) - float(bench / bench_entry - 1.0))
    return np.asarray(out, dtype=float)


def _flat_mask() -> np.ndarray:
    """Mask for flat-state decisions: HOLD or ENTER."""
    mask = np.zeros(ACTION_DIM, dtype=bool)
    mask[ACTION_HOLD] = True
    mask[ACTION_ENTER_OFFSET] = True
    return mask


def _long_mask() -> np.ndarray:
    """Mask for open-position decisions: HOLD or EXIT."""
    mask = np.zeros(ACTION_DIM, dtype=bool)
    mask[ACTION_HOLD] = True
    mask[ACTION_EXIT] = True
    return mask


def build_teacher_examples(
    df: pd.DataFrame,
    prices: dict,
    probs: dict,
    bench_sym: str,
    *,
    scaler: dict | None = None,
    entry_policy: dict | None = None,
) -> TeacherBatch:
    entry_policy = entry_policy or DEFAULT_POLICY
    obs_rows: list[np.ndarray] = []
    actions: list[int] = []
    masks: list[np.ndarray] = []
    counts = {
        "candidates": 0,
        "entry_enter": 0,
        "entry_skip": 0,
        "entry_ambiguous": 0,
        "exit_hold": 0,
        "exit_exit": 0,
        "exit_ambiguous": 0,
    }

    for _, row in df.iterrows():
        sig = entry_signal_day(probs, row["market_id"], row["t_theta"], entry_policy)
        if sig is None:
            continue
        t_e = as_utc_day(row["t_e"])
        dates = _calendar_dates(prices, bench_sym, sig, t_e)
        dates = [day for day in dates if day <= t_e - _DAY]
        if len(dates) < 2:
            continue
        active_path = _active_path_for_candidate(row, prices, bench_sym, dates)
        if active_path.size != len(dates) or not np.isfinite(active_path).all():
            continue

        counts["candidates"] += 1
        target = llm_target_from_row(row)
        static = static_features_from_row(row)

        entry_label = label_entry_path(active_path, target)
        if entry_label is None:
            counts["entry_ambiguous"] += 1
        else:
            day = dates[0]
            market = compute_market_state(prices, probs, row["symbol"], row["market_id"], bench_sym, day)
            position = compute_position_state(
                is_long=False,
                entry_price=0.0,
                asset_price=None,
                peak_ret=0.0,
                window_fraction=0.0,
                position_size_pct=0.0,
            )
            obs_rows.append(build_observation(static, position, market, scaler))
            actions.append(entry_label)
            masks.append(_flat_mask())
            counts["entry_enter" if entry_label == ACTION_ENTER_OFFSET else "entry_skip"] += 1

        entry_price = _close_on(prices, row["symbol"], dates[0])
        bench_entry = _close_on(prices, bench_sym, dates[0])
        if not entry_price or not bench_entry:
            continue
        peak_ret = 0.0
        for idx, day in enumerate(dates[1:], start=1):
            asset_price = _close_on(prices, row["symbol"], day)
            bench_price = _close_on(prices, bench_sym, day)
            if not asset_price or not bench_price:
                continue
            peak_ret = update_peak_ret(prices, row["symbol"], day, float(entry_price), peak_ret)
            best_idx = int(np.nanargmax(active_path[: idx + 1]))
            current_prob = prob_on(probs, row["market_id"], day)
            prob_3d = prob_on(probs, row["market_id"], day - 3 * _DAY)
            prob_slope = (
                float(current_prob) - float(prob_3d)
                if current_prob is not None and prob_3d is not None
                else 0.0
            )
            label = label_exit_path(
                active_path,
                idx,
                target,
                current_prob=current_prob,
                prob_slope_3d=prob_slope,
                days_since_high=idx - best_idx,
            )
            if label is None:
                counts["exit_ambiguous"] += 1
                continue

            frac = idx / max(1, len(dates) - 1)
            market = compute_market_state(prices, probs, row["symbol"], row["market_id"], bench_sym, day)
            position = compute_position_state(
                is_long=True,
                entry_price=float(entry_price),
                asset_price=float(asset_price),
                bench_entry_price=float(bench_entry),
                bench_price=float(bench_price),
                peak_ret=peak_ret,
                window_fraction=frac,
                position_size_pct=0.10,
            )
            obs_rows.append(build_observation(static, position, market, scaler))
            actions.append(label)
            masks.append(_long_mask())
            counts["exit_hold" if label == ACTION_HOLD else "exit_exit"] += 1

    if not obs_rows:
        return _empty_batch(counts)
    return TeacherBatch(
        obs=np.asarray(obs_rows, dtype=np.float32),
        actions=np.asarray(actions, dtype=np.int64),
        masks=np.asarray(masks, dtype=bool),
        counts=counts,
    )
