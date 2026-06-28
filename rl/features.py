"""
Feature engineering and the shared observation builder for the RL agent.

Everything here is point-in-time: a feature evaluated for day ``d`` only uses
data with timestamp <= d, so neither training nor evaluation can see the future.

Both the training environment (rl/env.py) and the portfolio simulator
(rl/sim_with_policy.py) build observations through ``build_observation`` so the
agent sees an identical vector in both, removing train/eval skew.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from rl.config import OBSERVATION_COLS, RF_OBS_COLS
from rl.shared import (
    TARGET,
    as_utc_day,
    causal_purged_oof_predictions,
    entry_day,
    fit_predict_as_of,
    infer_return_unit,
    prediction_to_decimal,
    _close_on,
)

_DAY = pd.Timedelta(days=1)

# Unbounded-ish static features that benefit from train-only standardization.
STATIC_FEATURE_COLS: tuple[str, ...] = (
    "rf_pred_decimal",
    "rf_pred_rank",
    "feat_prob_at_trigger",
    "feat_time_to_resolution_days",
    "feat_spy_2w_trend",
    "feat_debt_to_equity",
    "feat_asset_2w_trend",
    "feat_runup_since_t0",
    "feat_pre_entry_volume_log",
    "feat_beta",
    "feat_log_market_cap",
    "feat_connection_strength",
    "feat_prob_slope_24h",
    "feat_prob_surge_since_t0",
    "feat_crossing_latency_days",
    "feat_llm_expected_return",
    "expected_return_pct",
    "confidence_score",
    "feat_llm_confidence",
    "llm_target",
    "llm_confidence_norm",
)


def _finite_float(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if np.isfinite(out) else default


def llm_target_from_row(row: pd.Series | dict) -> float:
    """Expected move in decimal units, bounded to a useful swing-trade target."""
    direct = _finite_float(row.get("feat_llm_expected_return", 0.0))
    fallback = _finite_float(row.get("expected_return_pct", 0.0)) / 100.0
    target = abs(direct if abs(direct) > 1e-12 else fallback)
    return float(np.clip(target, 0.03, 0.20))


def llm_confidence_from_row(row: pd.Series | dict) -> float:
    """Normalize the stronger available LLM confidence score onto 0..1."""
    confidence = max(
        _finite_float(row.get("confidence_score", 0.0)),
        _finite_float(row.get("feat_llm_confidence", 0.0)),
    )
    return float(np.clip(confidence / 8.0, 0.0, 1.0))


# ── RF predictions + skill gate ──────────────────────────────────────────────

def get_rf_predictions(
    train_pool: pd.DataFrame,
    eval_df: pd.DataFrame,
    *,
    as_of: pd.Timestamp,
    seed: int,
    use_causal_oof: bool,
) -> tuple[pd.Series, str]:
    """Causal OOF predictions for training rows, or as-of RF for eval rows."""
    target_unit = infer_return_unit(train_pool[TARGET])
    if use_causal_oof:
        predictions = causal_purged_oof_predictions(
            train_pool, target_col=TARGET, as_of=as_of, seed=seed
        )
    else:
        predictions, _ = fit_predict_as_of(
            train_pool, eval_df, target_col=TARGET, as_of=as_of, seed=seed
        )
    return predictions, target_unit


def compute_rf_skill(
    train_pool: pd.DataFrame, *, as_of: pd.Timestamp, seed: int
) -> dict:
    """Out-of-sample RF skill on causal OOF predictions vs realized return.

    Returns directional hit-rate and Spearman rank-IC. Computed only on the
    development pool (never the holdout), so the gate decision is leakage-safe.
    """
    preds = causal_purged_oof_predictions(
        train_pool, target_col=TARGET, as_of=as_of, seed=seed
    )
    y = pd.to_numeric(train_pool[TARGET], errors="coerce")
    mask = preds.notna() & y.notna()
    n = int(mask.sum())
    if n < 20:
        return {"hit_rate": float("nan"), "rank_ic": float("nan"), "n": n}
    p = preds[mask].astype(float)
    yv = y[mask].astype(float)
    hit_rate = float((np.sign(p.to_numpy()) == np.sign(yv.to_numpy())).mean())
    rank_ic = float(p.corr(yv, method="spearman"))
    return {"hit_rate": hit_rate, "rank_ic": rank_ic, "n": n}


def rf_passes_gate(skill: dict, *, min_hit_rate: float, min_rank_ic: float) -> bool:
    """The RF is kept only if it beats a coin flip on hit-rate OR rank-IC."""
    hit = skill.get("hit_rate", float("nan"))
    ic = skill.get("rank_ic", float("nan"))
    hit_ok = bool(np.isfinite(hit) and hit >= min_hit_rate)
    ic_ok = bool(np.isfinite(ic) and ic >= min_rank_ic)
    return hit_ok or ic_ok


def attach_static_features(
    df: pd.DataFrame,
    predictions: pd.Series | None,
    target_unit: str,
    *,
    use_rf: bool,
) -> pd.DataFrame:
    """Attach rf_pred_decimal / rf_pred_rank (zeroed if the RF is gated out)."""
    if df.empty:
        return df.copy()
    out = df.copy()
    if use_rf and predictions is not None:
        out["rf_pred"] = predictions.reindex(out.index)
        out = out.dropna(subset=["rf_pred"]).copy()
        out["rf_pred_decimal"] = out["rf_pred"].astype(float).map(
            lambda p: prediction_to_decimal(p, target_unit)
        )
        out["rf_pred_rank"] = out["rf_pred_decimal"].rank(pct=True)
    else:
        out["rf_pred_decimal"] = 0.0
        out["rf_pred_rank"] = 0.0
    return out


# ── train-only static scaler ─────────────────────────────────────────────────

def fit_static_scaler(df: pd.DataFrame) -> dict[str, tuple[float, float]]:
    """Per-column (mean, std) for the unbounded static features (train rows only)."""
    scaler: dict[str, tuple[float, float]] = {}
    for col in STATIC_FEATURE_COLS:
        if col in df.columns:
            vals = pd.to_numeric(df[col], errors="coerce").dropna()
            if len(vals) > 1:
                mean = float(vals.mean())
                std = float(vals.std(ddof=0))
                scaler[col] = (mean, std if std > 1e-6 else 1.0)
    return scaler


# ── point-in-time market / position state ────────────────────────────────────

def prob_on(probs: dict, market_id: str, day: pd.Timestamp) -> float | None:
    """Latest probability at or before ``day`` (paths are sorted ascending)."""
    val = None
    for t, p in probs.get(market_id, []):
        if as_utc_day(t) <= day:
            val = p
        else:
            break
    return val


def entry_signal_day(probs: dict, market_id: str, t_theta: pd.Timestamp, policy: dict) -> pd.Timestamp | None:
    """Earliest day the validated entry band fires (reuses strategy.entry_day)."""
    ent = entry_day(probs.get(market_id, []), pd.Timestamp(t_theta), policy)
    if ent is None:
        return None
    return as_utc_day(ent[0])


def compute_market_state(
    prices: dict,
    probs: dict,
    symbol: str,
    market_id: str,
    bench_sym: str,
    day: pd.Timestamp,
) -> dict:
    current_prob = prob_on(probs, market_id, day)
    prob_3d = prob_on(probs, market_id, day - 3 * _DAY)
    prob_slope = (
        float(current_prob) - float(prob_3d)
        if current_prob is not None and prob_3d is not None
        else 0.0
    )
    bench_now = _close_on(prices, bench_sym, day)
    bench_5d = _close_on(prices, bench_sym, day - 5 * _DAY)
    bench_trend = (bench_now / bench_5d - 1.0) if bench_now and bench_5d else 0.0
    return {
        "current_prob": float(current_prob) if current_prob is not None else 0.0,
        "prob_slope_3d": float(prob_slope),
        "bench_trend_5d": float(bench_trend),
    }


def compute_position_state(
        *,
        is_long: bool,
        entry_price: float,
        asset_price: float | None,
        bench_entry_price: float = 0.0,
        bench_price: float | None = None,
        peak_ret: float,
        window_fraction: float,
        position_size_pct: float = 0.0,
) -> dict:
    if not is_long or not entry_price or not asset_price:
        return {
            "window_fraction_elapsed": float(window_fraction),
            "unrealized_ret": 0.0,
            "peak_ret": 0.0,
            "drawdown_from_peak": 0.0,
            "position_size_pct": 0.0,
            "convergence_residual": 0.0,
        }
    unrealized = asset_price / entry_price - 1.0
    peak = max(peak_ret, unrealized)
    drawdown = peak - unrealized

    bench_ret = (bench_price / bench_entry_price - 1.0) if bench_entry_price and bench_price else 0.0
    convergence_residual = bench_ret - unrealized

    return {
        "window_fraction_elapsed": float(window_fraction),
        "unrealized_ret": float(unrealized),
        "peak_ret": float(peak),
        "drawdown_from_peak": float(drawdown),
        "position_size_pct": float(position_size_pct),
        "convergence_residual": float(convergence_residual),
    }


def static_features_from_row(row: pd.Series | dict) -> dict:
    def g(key):
        return _finite_float(row.get(key, 0.0), 0.0)
    return {
        "rf_pred_decimal": g("rf_pred_decimal"),
        "rf_pred_rank": g("rf_pred_rank"),
        "feat_prob_at_trigger": g("feat_prob_at_trigger"),
        "feat_time_to_resolution_days": g("feat_time_to_resolution_days"),
        "feat_spy_2w_trend": g("feat_spy_2w_trend"),
        "feat_debt_to_equity": g("feat_debt_to_equity"),
        "feat_asset_2w_trend": g("feat_asset_2w_trend"),
        "feat_runup_since_t0": g("feat_runup_since_t0"),
        "feat_pre_entry_volume_log": g("feat_pre_entry_volume_log"),
        "feat_beta": g("feat_beta"),
        "feat_log_market_cap": g("feat_log_market_cap"),
        "feat_connection_strength": g("feat_connection_strength"),
        "feat_prob_slope_24h": g("feat_prob_slope_24h"),
        "feat_prob_surge_since_t0": g("feat_prob_surge_since_t0"),
        "feat_crossing_latency_days": g("feat_crossing_latency_days"),
        "feat_llm_expected_return": g("feat_llm_expected_return"),
        "expected_return_pct": g("expected_return_pct"),
        "confidence_score": g("confidence_score"),
        "feat_llm_confidence": g("feat_llm_confidence"),
        "llm_target": llm_target_from_row(row),
        "llm_confidence_norm": llm_confidence_from_row(row),
        "archetype_is_earnings": 1.0
        if "earnings" in str(row.get("feat_archetype", "")).lower()
        else 0.0,
    }


def build_observation(
    static: dict,
    position: dict,
    market: dict,
    scaler: dict[str, tuple[float, float]] | None,
) -> np.ndarray:
    """Assemble the fixed-order observation vector (train-only scaler + clip)."""
    merged = {**static, **position, **market}
    
    # Compute derived dynamic context features
    expected_ret = max(0.001, abs(float(merged.get("llm_target", 0.03))))
    unrealized_ret = float(merged.get("unrealized_ret", 0.0))
    
    # 1. Momentum: Are we feeding on the news and surging?
    merged["profit_vs_expectation"] = unrealized_ret / expected_ret
    
    # 2. Trailing Stop Conviction: "Don't go beneath it once passed"
    # If we crossed the target, how much buffer do we have? If negative, it's 0.
    cushion_above_target = max(0.0, unrealized_ret - expected_ret)
    merged["time_decay_conviction"] = cushion_above_target / max(0.001, expected_ret)

    vec = np.empty(len(OBSERVATION_COLS), dtype=np.float32)
    for i, col in enumerate(OBSERVATION_COLS):
        v = float(merged.get(col, 0.0))
        if scaler and col in scaler:
            mean, std = scaler[col]
            v = (v - mean) / std
        if not np.isfinite(v):
            v = 0.0
        vec[i] = float(np.clip(v, -10.0, 10.0))
    return vec
