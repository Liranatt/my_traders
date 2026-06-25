"""Task 3: confront direction on idiosyncratic forward moves.

The target is the sign of the forward return hedged against the symbol's sector ETF
(fallback SPY). Signals are offline and backtestable only: semantic role/polarity,
oracle dynamics, pre-crossing price action, cohort rank, and macro repricing.

Usage:
  .venv/Scripts/python.exe -m general_testing.direction_signal_study
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from typing import Any

import numpy as np

from general_testing.diffusion_research_helpers import (
    DEFAULT_RUN_STATUSES,
    SPY,
    ci_rate,
    cluster_bootstrap_hit_ci,
    fwd_return_bars,
    fwd_return_elapsed,
    hedge_symbol,
    hours_ago,
    load_observations,
    load_price_series,
    load_probability_series,
    polarity_for_group,
    probability_at_or_after,
    probability_at_or_before,
    probability_volume_sum,
    return_between,
    summarize_dataset,
)
from main_backtesting.semantic_groups import LONG_ELIGIBLE_GROUPS, PROPOSED_GROUPS

HOURLY_HORIZONS = {"6h": 6, "24h": 24, "48h": 48}
DAILY_HORIZONS = {"10d": 10}
NARROWED_ARCHETYPES = frozenset(
    {
        "military_escalation+energy_beneficiary",
        "oil_supply_disruption+energy_beneficiary",
        "fda_approval+direct_company",
    }
)
NUMERIC_SIGNALS = (
    ("oracle_level", "oracle_level"),
    ("oracle_slope_24h", "oracle_slope_24h"),
    ("oracle_acceleration", "oracle_acceleration"),
    ("oracle_volume_24h_log", "oracle_volume_24h_log"),
    ("pre_asset_return", "pre_asset_return"),
    ("pre_hedged_return", "pre_hedged_return"),
    ("cohort_pre_asset_rank", "cohort_pre_asset_rank"),
    ("cohort_pre_hedged_rank", "cohort_pre_hedged_rank"),
    ("oracle_move_since_t0", "oracle_move_since_t0"),
)


def finite(value: Any) -> bool:
    return value is not None and np.isfinite(float(value))


def sign(value: float | None) -> int | None:
    if value is None or not np.isfinite(value) or value == 0:
        return None
    return 1 if value > 0 else -1


def add_probability_features(rows: list[dict], probabilities: dict) -> None:
    for row in rows:
        market_id = str(row["market_id"])
        t = row["first_pass_at"]
        t0 = row["t0"]
        features = row.get("features") or {}
        p_now = probability_at_or_before(probabilities, market_id, t)
        if p_now is None:
            p_now = features.get("polymarket_probability_at_trigger")
        p_24 = probability_at_or_before(probabilities, market_id, hours_ago(t, 24))
        p_48 = probability_at_or_before(probabilities, market_id, hours_ago(t, 48))
        p0 = probability_at_or_before(probabilities, market_id, t0)
        if p0 is None:
            p0 = probability_at_or_after(probabilities, market_id, t0)
        slope_24 = (p_now - p_24) if p_now is not None and p_24 is not None else features.get(
            "polymarket_probability_slope_24h"
        )
        prev_slope = (p_24 - p_48) if p_24 is not None and p_48 is not None else None
        volume_24 = probability_volume_sum(probabilities, market_id, hours_ago(t, 24), t)
        row["oracle_level"] = float(p_now) if p_now is not None else None
        row["oracle_slope_24h"] = float(slope_24) if slope_24 is not None else None
        row["oracle_acceleration"] = (
            float(slope_24 - prev_slope)
            if slope_24 is not None and prev_slope is not None
            else None
        )
        row["oracle_volume_24h_log"] = float(np.log1p(volume_24)) if volume_24 is not None else None
        row["oracle_move_since_t0"] = float(p_now - p0) if p_now is not None and p0 is not None else None


def add_price_features(rows: list[dict], prices: dict, suffix: str) -> None:
    for row in rows:
        asset_pre = return_between(prices, str(row["symbol"]), row["t0"], row["first_pass_at"])
        hedge = hedge_symbol(row)
        hedge_pre = return_between(prices, hedge, row["t0"], row["first_pass_at"])
        if hedge_pre is None and hedge != SPY:
            hedge = SPY
            hedge_pre = return_between(prices, hedge, row["t0"], row["first_pass_at"])
        row[f"pre_asset_return_{suffix}"] = asset_pre
        row[f"pre_hedged_return_{suffix}"] = (
            asset_pre - hedge_pre if asset_pre is not None and hedge_pre is not None else None
        )
        row[f"cohort_pre_asset_rank_{suffix}"] = None
        row[f"cohort_pre_hedged_rank_{suffix}"] = None

    cohorts: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in rows:
        cohorts[(str(row["market_id"]), int(row["first_pass_number"]))].append(row)
    for members in cohorts.values():
        for field in ("pre_asset_return", "pre_hedged_return"):
            values = [(idx, row[f"{field}_{suffix}"]) for idx, row in enumerate(members) if finite(row[f"{field}_{suffix}"])]
            if len(values) < 3:
                continue
            ordered = sorted(values, key=lambda item: float(item[1]))
            denom = max(1, len(ordered) - 1)
            for rank, (idx, _) in enumerate(ordered):
                members[idx][f"cohort_{field.replace('return', 'rank')}_{suffix}"] = rank / denom


def structural_semantic_prediction(row: dict) -> int | None:
    group = row.get("semantic_group") or row.get("event_archetype")
    if group in LONG_ELIGIBLE_GROUPS:
        return 1
    if group in PROPOSED_GROUPS and polarity_for_group(group) == "negative":
        return -1
    return None


def target_for_hourly(row: dict, prices: dict, hours: int) -> float | None:
    asset = None
    if row["symbol"] in prices:
        asset = fwd_return_elapsed(prices, str(row["symbol"]), row["first_pass_at"], hours)
    hedge = hedge_symbol(row)
    hedge_ret = fwd_return_elapsed(prices, hedge, row["first_pass_at"], hours)
    if hedge_ret is None and hedge != SPY:
        hedge_ret = fwd_return_elapsed(prices, SPY, row["first_pass_at"], hours)
    return asset - hedge_ret if asset is not None and hedge_ret is not None else None


def target_for_daily(row: dict, prices: dict, days: int) -> float | None:
    asset = fwd_return_bars(prices, str(row["symbol"]), row["first_pass_at"], days)
    hedge = hedge_symbol(row)
    hedge_ret = fwd_return_bars(prices, hedge, row["first_pass_at"], days)
    if hedge_ret is None and hedge != SPY:
        hedge_ret = fwd_return_bars(prices, SPY, row["first_pass_at"], days)
    return asset - hedge_ret if asset is not None and hedge_ret is not None else None


def prepared_rows_for_horizon(
    base_rows: list[dict],
    *,
    horizon_label: str,
    target_prices: dict,
    feature_suffix: str,
) -> list[dict]:
    rows = []
    for idx, base in enumerate(base_rows):
        row = dict(base)
        if horizon_label.endswith("h"):
            target = target_for_hourly(row, target_prices, int(horizon_label[:-1]))
        else:
            target = target_for_daily(row, target_prices, int(horizon_label[:-1]))
        target_sign = sign(target)
        if target_sign is None:
            continue
        row["row_id"] = idx
        row["horizon"] = horizon_label
        row["target"] = target
        row["target_sign"] = target_sign
        row["semantic_category"] = (
            f"{row.get('yes_outcome_polarity')}|{row.get('semantic_outcome')}|{row.get('asset_role')}"
        )
        row["semantic_structural_prediction"] = structural_semantic_prediction(row)
        for name, _ in NUMERIC_SIGNALS:
            if name.startswith("pre_") or name.startswith("cohort_"):
                row[name] = row.get(f"{name}_{feature_suffix}")
        rows.append(row)
    rows.sort(key=lambda item: (item["first_pass_at"], item["label_available_at"], str(item["symbol"])))
    return rows


def training_rows(rows: list[dict], current: dict, field: str | None = None) -> list[dict]:
    output = []
    for row in rows:
        if row["label_available_at"] >= current["first_pass_at"]:
            continue
        if field is not None and not finite(row.get(field)):
            continue
        output.append(row)
    return output


def majority_predictions(rows: list[dict], *, min_train: int) -> dict[int, int]:
    predictions = {}
    for row in rows:
        train = training_rows(rows, row)
        if len(train) < min_train:
            continue
        mean_sign = float(np.mean([item["target_sign"] for item in train]))
        predictions[row["row_id"]] = 1 if mean_sign >= 0 else -1
    return predictions


def numeric_signal_predictions(rows: list[dict], field: str, *, min_train: int) -> list[dict]:
    predictions = []
    for row in rows:
        if not finite(row.get(field)):
            continue
        train = training_rows(rows, row, field)
        if len(train) < min_train:
            continue
        values = np.array([float(item[field]) for item in train], dtype=float)
        if np.nanstd(values) == 0:
            continue
        threshold = float(np.nanmedian(values))
        high = [item["target_sign"] for item in train if float(item[field]) >= threshold]
        low = [item["target_sign"] for item in train if float(item[field]) < threshold]
        if not high or not low:
            continue
        high_is_positive = np.mean(high) >= np.mean(low)
        pred = 1 if (float(row[field]) >= threshold) == high_is_positive else -1
        predictions.append({**row, "prediction": pred, "signal": field})
    return predictions


def categorical_signal_predictions(
    rows: list[dict],
    field: str,
    *,
    min_bucket: int,
) -> list[dict]:
    predictions = []
    for row in rows:
        category = row.get(field)
        if category is None:
            continue
        train = [item for item in training_rows(rows, row) if item.get(field) == category]
        if len(train) < min_bucket:
            continue
        mean_sign = float(np.mean([item["target_sign"] for item in train]))
        pred = 1 if mean_sign >= 0 else -1
        predictions.append({**row, "prediction": pred, "signal": field})
    return predictions


def deterministic_predictions(rows: list[dict], field: str) -> list[dict]:
    output = []
    for row in rows:
        pred = row.get(field)
        if pred in (-1, 1):
            output.append({**row, "prediction": int(pred), "signal": field})
    return output


def summarize_predictions(
    predictions: list[dict],
    *,
    signal_name: str,
    majority_by_row: dict[int, int],
) -> dict[str, Any] | None:
    if not predictions:
        return None
    hits = [row["prediction"] == row["target_sign"] for row in predictions]
    majority_hits = [
        majority_by_row[row["row_id"]] == row["target_sign"]
        for row in predictions
        if row["row_id"] in majority_by_row
    ]
    ci = cluster_bootstrap_hit_ci(predictions)
    return {
        "signal": signal_name,
        "n": len(predictions),
        "events": len({str(row["event_id"]) for row in predictions}),
        "hit": float(np.mean(hits)),
        "hit_ci": ci,
        "majority_hit_same_rows": float(np.mean(majority_hits)) if majority_hits else None,
        "positive_prediction_rate": float(np.mean([row["prediction"] > 0 for row in predictions])),
        "positive_target_rate": float(np.mean([row["target_sign"] > 0 for row in predictions])),
    }


def evaluate_scope(rows: list[dict], *, min_train: int, min_bucket: int) -> list[dict[str, Any]]:
    majority_by_row = majority_predictions(rows, min_train=min_train)
    results: list[dict[str, Any]] = []
    baseline_rows = [
        {**row, "prediction": majority_by_row[row["row_id"]], "signal": "majority_class_walk_forward"}
        for row in rows
        if row["row_id"] in majority_by_row
    ]
    baseline = summarize_predictions(
        baseline_rows,
        signal_name="majority_class_walk_forward",
        majority_by_row=majority_by_row,
    )
    if baseline:
        results.append(baseline)
    structural = summarize_predictions(
        deterministic_predictions(rows, "semantic_structural_prediction"),
        signal_name="semantic_structural_direction",
        majority_by_row=majority_by_row,
    )
    if structural:
        results.append(structural)
    semantic = summarize_predictions(
        categorical_signal_predictions(rows, "semantic_category", min_bucket=min_bucket),
        signal_name="semantic_category_walk_forward",
        majority_by_row=majority_by_row,
    )
    if semantic:
        results.append(semantic)
    for signal_name, field in NUMERIC_SIGNALS:
        result = summarize_predictions(
            numeric_signal_predictions(rows, field, min_train=min_train),
            signal_name=signal_name,
            majority_by_row=majority_by_row,
        )
        if result:
            results.append(result)
    results.sort(key=lambda item: (item["hit"], item["n"]), reverse=True)
    return results


def print_ranked(
    title: str,
    results: list[dict[str, Any]],
    *,
    limit: int = 12,
    min_report_n: int = 10,
) -> None:
    print("\n" + title)
    print("-" * len(title))
    print("signal                              n   ev   hit   95ci       maj_same  pred+  target+")
    display = [result for result in results if result["n"] >= min_report_n]
    if not display:
        display = results
    for result in display[:limit]:
        maj = result["majority_hit_same_rows"]
        maj_text = "   na" if maj is None else f"{maj:6.0%}"
        print(
            f"{result['signal'][:34]:34} {result['n']:4} {result['events']:4} "
            f"{result['hit']:5.0%} {ci_rate(result['hit_ci']):>11} "
            f"{maj_text} {result['positive_prediction_rate']:6.0%} {result['positive_target_rate']:7.0%}"
        )


def best_signal_summary(
    rows: list[dict],
    *,
    min_train: int,
    min_bucket: int,
    min_n: int,
) -> dict[str, Any] | None:
    results = [
        result
        for result in evaluate_scope(rows, min_train=min_train, min_bucket=min_bucket)
        if result["signal"] != "majority_class_walk_forward" and result["n"] >= min_n
    ]
    return results[0] if results else None


async def run(args: argparse.Namespace) -> None:
    rows, meta = await load_observations(
        run_ids=args.run_id,
        statuses=args.status,
        dedupe=not args.keep_duplicates,
    )
    print("\nDIRECTION SIGNAL STUDY")
    print("=" * 80)
    summarize_dataset(rows, meta)

    symbols = {row["symbol"] for row in rows}
    symbols |= {hedge_symbol(row) for row in rows}
    symbols.add(SPY)
    hourly_prices, daily_prices, probabilities = await asyncio.gather(
        load_price_series(symbols, "1h"),
        load_price_series(symbols, "1d"),
        load_probability_series({row["market_id"] for row in rows}),
    )
    print(
        f"\nloaded prices: hourly_symbols={len(hourly_prices)} daily_symbols={len(daily_prices)} "
        f"probability_markets={len(probabilities)}"
    )
    add_probability_features(rows, probabilities)
    add_price_features(rows, hourly_prices, "1h")
    add_price_features(rows, daily_prices, "1d")

    reliable_candidates = []
    for horizon_label in [*HOURLY_HORIZONS, *DAILY_HORIZONS]:
        target_prices = hourly_prices if horizon_label.endswith("h") else daily_prices
        feature_suffix = "1h" if horizon_label.endswith("h") else "1d"
        horizon_rows = prepared_rows_for_horizon(
            rows,
            horizon_label=horizon_label,
            target_prices=target_prices,
            feature_suffix=feature_suffix,
        )
        print(f"\nHORIZON {horizon_label}: target rows={len(horizon_rows)}")
        all_results = evaluate_scope(
            horizon_rows,
            min_train=args.min_train,
            min_bucket=args.min_bucket,
        )
        print_ranked(f"{horizon_label} - ALL reactive names", all_results, min_report_n=args.min_report_n)

        narrowed = [row for row in horizon_rows if row["event_archetype"] in NARROWED_ARCHETYPES]
        narrowed_results = evaluate_scope(
            narrowed,
            min_train=max(5, min(args.min_train, 20)),
            min_bucket=max(3, min(args.min_bucket, 5)),
        )
        print_ranked(
            f"{horizon_label} - NARROWED energy/FDA",
            narrowed_results,
            min_report_n=max(5, min(args.min_report_n, 10)),
        )

        print("\n" + f"{horizon_label} - best signal per archetype")
        print("-" * (len(horizon_label) + 28))
        print("archetype                                      target_n signal_n  best_signal                         hit   95ci")
        for archetype in sorted({row["event_archetype"] for row in horizon_rows}):
            scoped = [row for row in horizon_rows if row["event_archetype"] == archetype]
            best = best_signal_summary(
                scoped,
                min_train=max(5, min(args.min_train, 20)),
                min_bucket=max(3, min(args.min_bucket, 5)),
                min_n=max(5, min(args.min_report_n, 10)),
            )
            if best is None:
                print(f"{archetype:48} {len(scoped):8} {'-':>8}  no OOS signal coverage")
                continue
            print(
                f"{archetype:48} {len(scoped):8} {best['n']:8}  {best['signal'][:34]:34} "
                f"{best['hit']:5.0%} {ci_rate(best['hit_ci']):>11}"
            )
            ci = best["hit_ci"]
            if best["n"] >= 30 and best["hit"] > 0.55 and ci is not None and ci[0] > 0.55:
                reliable_candidates.append((horizon_label, archetype, best))

    print("\nVERDICT: ANY BACKTESTABLE DIRECTION SIGNAL RELIABLY > 0.55?")
    print("-" * 80)
    if reliable_candidates:
        for horizon_label, archetype, best in reliable_candidates:
            print(
                f"YES  {horizon_label:4} {archetype:48} {best['signal']} "
                f"hit={best['hit']:.0%} CI={ci_rate(best['hit_ci'])} n={best['n']}"
            )
    else:
        print("NO reliable signal met hit > 0.55 with cluster-bootstrap lower CI > 0.55 and n >= 30.")
        print("This is directional-signal evidence only; it does not evaluate stops or beta management.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-forward direction signal study.")
    parser.add_argument("--run-id", action="append", help="Optional run_id filter; repeat for multiple runs.")
    parser.add_argument(
        "--status",
        action="append",
        default=list(DEFAULT_RUN_STATUSES),
        help="Run status to include; default includes complete and kept.",
    )
    parser.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="Keep exact duplicate candidates across runs instead of deduping them.",
    )
    parser.add_argument("--min-train", type=int, default=20, help="Minimum prior labeled rows for numeric signals.")
    parser.add_argument("--min-bucket", type=int, default=5, help="Minimum prior labeled rows in a categorical bucket.")
    parser.add_argument("--min-report-n", type=int, default=10, help="Minimum signal coverage shown in ranked tables.")
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
