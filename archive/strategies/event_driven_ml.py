from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from uuid import UUID, uuid4
import warnings

import numpy as np
from sklearn.linear_model import (
    LogisticRegression,
    LogisticRegressionCV,
    Ridge,
    RidgeCV,
)

from main_backtesting.models import (
    MLModelSnapshot,
    MLObservation,
    MLPrediction,
    PriceBar,
    ProbabilityPoint,
)

FEATURE_NAMES = [
    # Original price-trend features (kept until shown to add nothing).
    "asset_ytd_change",
    "sector_one_month_trend",
    "spy_two_week_trend",
    "asset_two_week_trend",
    # Polymarket signal features -- the prediction-market edge the paper is built on.
    # All are computed strictly from data at or before the threshold crossing (Tθ).
    "polymarket_probability_at_trigger",
    "polymarket_probability_slope_24h",
    "polymarket_time_to_resolution_days",
    "polymarket_crossing_latency_days",
    "polymarket_pre_entry_volume_log",
    "polymarket_probability_volatility",
]

# Opt-in query-window features: price behaviour relative to the QUERY timeline (T0 -> Te),
# not just the 55% crossing. Added to the model only when config.ml_use_query_window_features
# is set, so we can A/B whether query-relative data lifts accuracy.
QUERY_WINDOW_FEATURE_NAMES = [
    "query_runup_since_creation",
    "query_elapsed_fraction_at_crossing",
]


def active_feature_names(use_query_window_features: bool) -> list[str]:
    if use_query_window_features:
        return list(FEATURE_NAMES) + list(QUERY_WINDOW_FEATURE_NAMES)
    return list(FEATURE_NAMES)
ALPHAS = [0.01, 0.1, 1.0, 10.0]
DAILY_SESSION_LENGTH = timedelta(hours=6, minutes=30)


def close_as_of(bars: list[PriceBar], timestamp: datetime) -> float | None:
    value = None
    for bar in bars:
        if bar.timestamp + DAILY_SESSION_LENGTH > timestamp:
            break
        value = bar.close
    return value


def return_between(bars: list[PriceBar], start: datetime, end: datetime) -> float | None:
    start_price = close_as_of(bars, start)
    end_price = close_as_of(bars, end)
    if start_price is None or end_price is None or start_price <= 0:
        return None
    return end_price / start_price - 1.0


def observation_targets(
    bars: list[PriceBar],
    *,
    event_start: datetime,
    peak_window_start: datetime,
    end: datetime,
) -> tuple[int | None, float | None, dict[str, Any]]:
    completed_before_threshold = [
        bar
        for bar in bars
        if bar.timestamp + DAILY_SESSION_LENGTH <= peak_window_start
    ]
    post_threshold_window = [
        bar
        for bar in bars
        if bar.timestamp + DAILY_SESSION_LENGTH > peak_window_start
        and bar.timestamp + DAILY_SESSION_LENGTH <= end
    ]
    if not completed_before_threshold or completed_before_threshold[-1].close <= 0:
        return None, None, {"reason": "missing_pre_threshold_anchor_close"}
    if not post_threshold_window:
        return None, None, {"reason": "missing_post_threshold_price_path"}
    anchor = completed_before_threshold[-1]
    terminal = post_threshold_window[-1]
    anchor_price = anchor.close
    direction = 1 if terminal.close > anchor_price else -1
    maximum_change = max(
        (bar.close / anchor_price) - 1.0 for bar in post_threshold_window
    )
    minimum_change = min(
        (bar.close / anchor_price) - 1.0 for bar in post_threshold_window
    )
    signed_peak = maximum_change if direction == 1 else minimum_change
    return direction, signed_peak, {
        # Kept as a compatibility alias for existing prediction persistence.
        "event_open_price": anchor_price,
        "anchor_close_at": anchor.timestamp,
        "anchor_close_price": anchor_price,
        "terminal_close_at": terminal.timestamp,
        "terminal_close_price": terminal.close,
        "event_path_rows": len(completed_before_threshold) + len(post_threshold_window),
        "post_threshold_path_rows": len(post_threshold_window),
        "maximum_change": maximum_change,
        "minimum_change": minimum_change,
    }


def build_observation(
    *,
    run_id: UUID,
    event_id: str,
    market_id: str,
    first_pass_number: int,
    first_pass_at: datetime,
    event_created_at: datetime,
    event_end_at: datetime,
    label_data_cutoff: datetime,
    symbol: str,
    event_archetype: str,
    resolution: str,
    asset_daily: list[PriceBar],
    sector_daily: list[PriceBar],
    spy_daily: list[PriceBar],
    research_data: dict[str, Any],
    probabilities: list[ProbabilityPoint] | None = None,
) -> MLObservation:
    from datetime import timedelta, timezone

    year_start = datetime(first_pass_at.year, 1, 1, tzinfo=timezone.utc)
    features = {
        "asset_ytd_change": return_between(asset_daily, year_start, first_pass_at),
        "sector_one_month_trend": return_between(
            sector_daily, first_pass_at - timedelta(days=30), first_pass_at
        ),
        "spy_two_week_trend": return_between(
            spy_daily, first_pass_at - timedelta(days=14), first_pass_at
        ),
        "asset_two_week_trend": return_between(
            asset_daily, first_pass_at - timedelta(days=14), first_pass_at
        ),
    }
    # --- Polymarket signal features (strictly from probability data <= Tθ) ---
    # Neutral 0.0 imputations below apply only when there is no pre-trigger history;
    # they are explicit, documented defaults, not silent cross-system fallbacks.
    pre_trigger_points = [
        point for point in (probabilities or []) if point.timestamp <= first_pass_at
    ]
    probability_at_trigger = (
        pre_trigger_points[-1].probability if pre_trigger_points else None
    )
    cutoff_24h = first_pass_at - timedelta(hours=24)
    points_24h_prior = [p for p in pre_trigger_points if p.timestamp <= cutoff_24h]
    probability_24h_ago = points_24h_prior[-1].probability if points_24h_prior else None
    probability_slope_24h = (
        probability_at_trigger - probability_24h_ago
        if probability_at_trigger is not None and probability_24h_ago is not None
        else 0.0
    )
    completed_volumes = [
        float(p.volume_usdc)
        for p in pre_trigger_points
        if p.volume_usdc is not None and p.volume_usdc > 0
    ]
    probability_series = [p.probability for p in pre_trigger_points]
    features.update(
        {
            "polymarket_probability_at_trigger": probability_at_trigger,
            "polymarket_probability_slope_24h": probability_slope_24h,
            "polymarket_time_to_resolution_days": max(
                (event_end_at - first_pass_at).total_seconds() / 86_400.0, 0.0
            ),
            "polymarket_crossing_latency_days": max(
                (first_pass_at - event_created_at).total_seconds() / 86_400.0, 0.0
            ),
            "polymarket_pre_entry_volume_log": (
                float(np.log1p(sum(completed_volumes))) if completed_volumes else 0.0
            ),
            "polymarket_probability_volatility": (
                float(np.std(probability_series)) if len(probability_series) >= 2 else 0.0
            ),
        }
    )
    # --- Query-window features: price behaviour relative to the QUERY timeline, not just
    # the 55% crossing. runup = move from query creation (T0) to the crossing (Tθ) -- the
    # "already priced-in" move; elapsed_fraction = where in the query's life the signal
    # fired. Both use only data <= Tθ. Defaults are explicit, not silent fallbacks. ---
    query_span_seconds = (event_end_at - event_created_at).total_seconds()
    query_elapsed_seconds = (first_pass_at - event_created_at).total_seconds()
    runup_since_creation = return_between(asset_daily, event_created_at, first_pass_at)
    features.update(
        {
            "query_runup_since_creation": (
                runup_since_creation if runup_since_creation is not None else 0.0
            ),
            "query_elapsed_fraction_at_crossing": (
                min(max(query_elapsed_seconds / query_span_seconds, 0.0), 1.0)
                if query_span_seconds > 0
                else 0.0
            ),
        }
    )
    completed_before_threshold = [
        bar
        for bar in asset_daily
        if bar.timestamp + DAILY_SESSION_LENGTH <= first_pass_at
    ]
    known_anchor_bar = (
        completed_before_threshold[-1] if completed_before_threshold else None
    )
    if event_end_at > label_data_cutoff:
        target_direction, target_magnitude = None, None
        target_data = {
            "reason": "event_unresolved_at_historical_data_cutoff",
            "historical_data_cutoff": label_data_cutoff,
            "event_open_price": known_anchor_bar.close if known_anchor_bar else None,
            "anchor_close_at": known_anchor_bar.timestamp if known_anchor_bar else None,
            "anchor_close_price": known_anchor_bar.close if known_anchor_bar else None,
        }
    else:
        target_direction, target_magnitude, target_data = observation_targets(
            asset_daily,
            event_start=event_created_at,
            peak_window_start=first_pass_at,
            end=event_end_at,
        )
    missing = [name for name, value in features.items() if value is None]
    valid = not missing and target_direction is not None and target_magnitude is not None
    exclusion = None
    if missing:
        exclusion = f"missing_required_feature:{','.join(missing)}"
    elif target_direction is None:
        exclusion = str(target_data.get("reason") or "missing_target")
    return MLObservation(
        observation_id=uuid4(),
        run_id=run_id,
        event_id=event_id,
        market_id=market_id,
        first_pass_number=first_pass_number,
        first_pass_at=first_pass_at,
        label_available_at=event_end_at,
        symbol=symbol.upper(),
        event_archetype=event_archetype,
        resolution=resolution,  # type: ignore[arg-type]
        features=features,
        research_data={**research_data, **target_data},
        classification_target=target_direction,
        regression_target=target_magnitude,
        valid_for_training=valid,
        exclusion_reason=exclusion,
    )


def _matrix(
    observations: list[MLObservation], feature_names: list[str]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x = np.array(
        [[float(item.features[name]) for name in feature_names] for item in observations],
        dtype=float,
    )
    y_class = np.array([int(item.classification_target) for item in observations], dtype=float)
    y_class = (y_class + 1.0) / 2.0
    y_reg = np.array([float(item.regression_target) for item in observations], dtype=float)
    return x, y_class, y_reg


def _standardize(x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    means = x.mean(axis=0)
    scales = x.std(axis=0)
    scales[scales == 0] = 1.0
    return (x - means) / scales, means, scales


def _sigmoid(value: np.ndarray) -> np.ndarray:
    clipped = np.clip(value, -35, 35)
    return 1.0 / (1.0 + np.exp(-clipped))


def _fit_classifier(
    x: np.ndarray, y: np.ndarray
) -> tuple[np.ndarray, float, float, dict[str, float]]:
    """L2-regularized logistic regression (scikit-learn, lbfgs).

    Returns coefficients on the standardized features, the intercept, the selected
    L2 ``alpha`` (= 1/C), and per-alpha cross-validation scores. Regularization is
    chosen by stratified CV when there are enough samples per class; otherwise a
    moderate fixed strength is used. Replaces the old 2000-step hand-rolled gradient
    descent (faster and with proper convergence).
    """
    classes = sorted({int(round(v)) for v in y.tolist()})
    min_class = int(min((y == c).sum() for c in classes)) if len(classes) == 2 else 0
    inverse_strengths = [1.0 / alpha for alpha in ALPHAS]  # C values
    cv_scores: dict[str, float] = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        if len(classes) == 2 and len(x) >= 12 and min_class >= 3:
            folds = int(min(5, min_class))
            model = LogisticRegressionCV(
                Cs=inverse_strengths,
                cv=folds,
                scoring="neg_log_loss",
                max_iter=2000,
                refit=True,
            ).fit(x, y)
            selected_c = float(model.C_[0])
            try:
                fold_scores = next(iter(model.scores_.values()))  # (folds, n_Cs)
                mean_scores = fold_scores.mean(axis=0)
                cv_scores = {
                    str(round(1.0 / c, 4)): float(s)
                    for c, s in zip(inverse_strengths, mean_scores)
                }
            except Exception:
                cv_scores = {}
        else:
            selected_c = 1.0
            model = LogisticRegression(C=selected_c, max_iter=2000).fit(x, y)
    return (
        model.coef_[0].astype(float),
        float(model.intercept_[0]),
        float(1.0 / selected_c),
        cv_scores,
    )


def _fit_ridge_model(
    x: np.ndarray, y: np.ndarray
) -> tuple[np.ndarray, float, float, dict[str, float]]:
    """Ridge regression (scikit-learn) with efficient built-in LOO alpha selection."""
    cv_scores: dict[str, float] = {}
    if len(x) >= 5:
        model = RidgeCV(alphas=ALPHAS, store_cv_results=True).fit(x, y)
        selected_alpha = float(model.alpha_)
        try:
            mse = model.cv_results_.mean(axis=0)  # (n_alphas,) leave-one-out MSE
            cv_scores = {str(a): float(-m) for a, m in zip(ALPHAS, mse)}
        except Exception:
            cv_scores = {}
    else:
        selected_alpha = 1.0
        model = Ridge(alpha=selected_alpha).fit(x, y)
    return model.coef_.astype(float), float(model.intercept_), selected_alpha, cv_scores


def train_snapshot(
    *,
    run_id: UUID,
    symbol: str,
    event_archetype: str,
    training_cutoff: datetime,
    observations: list[MLObservation],
    minimum_prior_observations: int,
    minimum_prior_events: int = 0,
    prediction_features: dict[str, float | None] | None = None,
    feature_names: list[str] | None = None,
) -> MLModelSnapshot:
    names = list(feature_names) if feature_names else list(FEATURE_NAMES)
    valid = [
        item
        for item in observations
        if item.valid_for_training
        and item.classification_target is not None
        and item.regression_target is not None
    ]
    base = dict(
        snapshot_id=uuid4(),
        run_id=run_id,
        symbol=symbol.upper(),
        event_archetype=event_archetype,
        training_cutoff=training_cutoff,
        training_event_ids=[item.event_id for item in valid],
        training_sample_count=len(valid),
        feature_names=names,
        feature_means={},
        feature_scales={},
        classifier_coefficients=None,
        classifier_intercept=None,
        ridge_coefficients=None,
        ridge_intercept=None,
        hyperparameters={},
        validation_metrics={},
    )
    if len(valid) < minimum_prior_observations:
        return MLModelSnapshot(status="insufficient_history", **base)
    if len({item.event_id for item in valid}) < minimum_prior_events:
        return MLModelSnapshot(status="insufficient_event_history", **base)
    classes = {item.classification_target for item in valid}
    if len(classes) < 2:
        return MLModelSnapshot(status="insufficient_class_diversity", **base)
    if prediction_features is not None and any(
        prediction_features.get(name) is None for name in names
    ):
        return MLModelSnapshot(status="missing_required_feature", **base)

    x, y_class, y_reg = _matrix(valid, names)
    standardized, means, scales = _standardize(x)
    class_weights, class_intercept, classifier_alpha, classifier_cv = _fit_classifier(
        standardized, y_class
    )
    ridge_weights, ridge_intercept, ridge_alpha, ridge_cv = _fit_ridge_model(
        standardized, y_reg
    )
    class_predictions = (
        _sigmoid(standardized @ class_weights + class_intercept) >= 0.5
    ).astype(float)
    ridge_predictions = standardized @ ridge_weights + ridge_intercept
    metrics = {
        "training_classification_accuracy": float((class_predictions == y_class).mean()),
        "training_regression_mae": float(np.abs(ridge_predictions - y_reg).mean()),
        "training_regression_rmse": float(np.sqrt(((ridge_predictions - y_reg) ** 2).mean())),
        "classifier_cv_scores": classifier_cv,
        "ridge_cv_scores": ridge_cv,
    }
    return MLModelSnapshot(
        status="trained",
        feature_means=dict(zip(names, means.tolist())),
        feature_scales=dict(zip(names, scales.tolist())),
        classifier_coefficients=dict(zip(names, class_weights.tolist())),
        classifier_intercept=class_intercept,
        ridge_coefficients=dict(zip(names, ridge_weights.tolist())),
        ridge_intercept=ridge_intercept,
        hyperparameters={
            "classifier_l2_alpha": classifier_alpha,
            "ridge_alpha": ridge_alpha,
        },
        validation_metrics=metrics,
        **{key: value for key, value in base.items() if key not in {
            "feature_means", "feature_scales", "classifier_coefficients",
            "classifier_intercept", "ridge_coefficients", "ridge_intercept",
            "hyperparameters", "validation_metrics"
        }},
    )


def predict(
    snapshot: MLModelSnapshot,
    *,
    run_id: UUID,
    market_id: str,
    event_id: str,
    pass_number: int,
    symbol: str,
    features: dict[str, float | None],
    event_open_price: float,
    realized_price_at_entry: float,
    direction_mode: str = "classifier",
) -> MLPrediction | None:
    if snapshot.status != "trained":
        return None
    # Use the snapshot's own feature list so prediction always matches what was trained
    # (handles the opt-in query-window features without a global constant).
    names = list(snapshot.feature_names)
    if any(features.get(name) is None for name in names):
        return None
    vector = np.array([float(features[name]) for name in names], dtype=float)
    means = np.array([snapshot.feature_means[name] for name in names])
    scales = np.array([snapshot.feature_scales[name] for name in names])
    standardized = (vector - means) / scales
    class_weights = np.array(
        [snapshot.classifier_coefficients[name] for name in names], dtype=float
    )
    ridge_weights = np.array(
        [snapshot.ridge_coefficients[name] for name in names], dtype=float
    )
    probability = float(_sigmoid(np.array([standardized @ class_weights + snapshot.classifier_intercept]))[0])
    predicted_peak = float(standardized @ ridge_weights + snapshot.ridge_intercept)
    if direction_mode == "regression_sign":
        # Direction taken from the Ridge predicted peak's sign; the logistic classifier
        # is not used as a gate (directions_agree is trivially satisfied).
        direction = "long" if predicted_peak >= 0 else "short"
        directions_agree = True
    else:
        direction = "long" if probability >= 0.5 else "short"
        directions_agree = (direction == "long" and predicted_peak > 0) or (
            direction == "short" and predicted_peak < 0
        )
    realized = realized_price_at_entry / event_open_price - 1.0
    directional_realized = realized if direction == "long" else -realized
    gap = abs(predicted_peak) - directional_realized
    target_price = event_open_price * (1.0 + predicted_peak)
    return MLPrediction(
        prediction_id=uuid4(),
        run_id=run_id,
        snapshot_id=snapshot.snapshot_id,
        market_id=market_id,
        event_id=event_id,
        pass_number=pass_number,
        symbol=symbol.upper(),
        direction=direction,
        classification_probability=probability,
        predicted_peak_percent=predicted_peak,
        predicted_target_price=target_price,
        realized_move_at_entry=realized,
        remaining_gap=gap,
        directions_agree=directions_agree,
    )


def evaluate_prediction(
    prediction: MLPrediction,
    *,
    event_open_price: float,
    bars_until_event_end: list[PriceBar],
) -> MLPrediction:
    if not bars_until_event_end:
        return prediction
    changes = [bar.close / event_open_price - 1.0 for bar in bars_until_event_end]
    favorable = max(changes) if prediction.direction == "long" else -min(changes)
    adverse = min(changes) if prediction.direction == "long" else -max(changes)
    terminal_change = changes[-1]
    actual_direction = "long" if terminal_change > 0 else "short"
    if prediction.direction == "long":
        hit = next(
            (bar for bar in bars_until_event_end if bar.close >= prediction.predicted_target_price),
            None,
        )
    else:
        hit = next(
            (bar for bar in bars_until_event_end if bar.close <= prediction.predicted_target_price),
            None,
        )
    prediction.target_reached = hit is not None
    prediction.target_reached_at = (
        hit.timestamp + DAILY_SESSION_LENGTH if hit else None
    )
    prediction.actual_max_favorable = favorable
    prediction.actual_max_adverse = adverse
    prediction.actual_direction = actual_direction
    prediction.classification_correct = actual_direction == prediction.direction
    actual_signed_peak = max(changes) if actual_direction == "long" else min(changes)
    prediction.regression_error = actual_signed_peak - prediction.predicted_peak_percent
    return prediction
