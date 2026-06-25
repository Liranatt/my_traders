from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import UUID

from database.db_connection import connect
from main_backtesting.semantic_groups import (
    LONG_ELIGIBLE_GROUPS,
    PROPOSED_GROUPS,
    classify_assignment,
    classify_question,
    question_yes_outcome_polarity,
)

SCHEMA = "checking_relevant_events"
LONG_RELEVANT_OUTCOMES = frozenset(group.split("+", 1)[0] for group in LONG_ELIGIBLE_GROUPS)


def json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    columns = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def performance(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    values = list(rows)
    net = [float(row.get("net_profit") or 0.0) for row in values]
    gross = [float(row.get("gross_profit") or 0.0) for row in values]
    commissions = [
        float(row.get("entry_commission") or 0.0)
        + float(row.get("exit_commission") or 0.0)
        for row in values
    ]
    return {
        "trades": len(values),
        "events": len({str(row["event_id"]) for row in values}),
        "questions": len({str(row["market_id"]) for row in values}),
        "gross_profit": sum(gross),
        "commissions": sum(commissions),
        "net_profit": sum(net),
        "average_net_profit": sum(net) / len(net) if net else None,
        "win_rate": sum(value > 0 for value in net) / len(net) if net else None,
    }


def clustered_mean_ci(
    rows: list[dict[str, Any]],
    *,
    samples: int = 2_000,
    seed: int = 20260614,
) -> list[float] | None:
    by_event: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        by_event[str(row["event_id"])].append(float(row.get("net_profit") or 0.0))
    events = sorted(by_event)
    if len(events) < 8:
        return None
    rng = random.Random(seed)
    means: list[float] = []
    for _ in range(samples):
        sampled = [rng.choice(events) for _ in events]
        values = [value for event_id in sampled for value in by_event[event_id]]
        means.append(sum(values) / len(values))
    means.sort()
    return [means[int(samples * 0.025)], means[int(samples * 0.975)]]


def assignment_record(
    row: dict[str, Any],
    *,
    symbol: str,
    asset_name: str,
) -> dict[str, Any]:
    tags = list(json_value(row.get("tags") or []))
    assignment = classify_assignment(
        str(row["question"]),
        symbol=symbol,
        asset_name=asset_name,
        event_title=str(row.get("event_title") or ""),
        tags=tags,
    )
    return {
        **row,
        "semantic_outcome": assignment.outcome,
        "asset_role": assignment.asset_role,
        "semantic_group": assignment.group,
        "yes_outcome_polarity": assignment.yes_outcome_polarity,
        "long_eligible": assignment.long_eligible,
        "semantic_confidence": assignment.confidence,
        "semantic_reason": assignment.reason,
    }


async def fetch_questions(conn: Any, run_id: UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"""
        SELECT m.market_id, m.event_id, m.question, m.created_at, m.end_at,
               m.final_outcome, e.title AS event_title, e.tags
        FROM {SCHEMA}.historical_run_markets m
        JOIN {SCHEMA}.source_events e ON e.event_id=m.event_id
        WHERE m.run_id=$1
        ORDER BY m.created_at, m.market_id
        """,
        run_id,
    )
    output = []
    for row in rows:
        item = dict(row)
        classification = classify_question(
            str(row["question"]),
            event_title=str(row["event_title"] or ""),
            tags=list(row["tags"] or []),
        )
        output.append(
            {
                **item,
                "semantic_outcome": classification.outcome,
                "yes_outcome_polarity": question_yes_outcome_polarity(
                    classification.outcome
                ),
                "long_consideration": (
                    question_yes_outcome_polarity(classification.outcome) == "positive"
                ),
                "semantic_confidence": classification.confidence,
                "semantic_reason": classification.reason,
            }
        )
    return output


async def fetch_all_source_questions(conn: Any) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"""
        SELECT q.market_id, q.event_id, q.question, q.created_at, q.end_at,
               e.title AS event_title, e.tags
        FROM {SCHEMA}.source_questions q
        JOIN {SCHEMA}.source_events e ON e.event_id=q.event_id
        WHERE q.question IS NOT NULL
        ORDER BY q.created_at, q.market_id
        """
    )
    output = []
    for row in rows:
        item = dict(row)
        classification = classify_question(
            str(row["question"]),
            event_title=str(row["event_title"] or ""),
            tags=list(row["tags"] or []),
        )
        polarity = question_yes_outcome_polarity(classification.outcome)
        output.append(
            {
                **item,
                "semantic_outcome": classification.outcome,
                "yes_outcome_polarity": polarity,
                "long_consideration": polarity == "positive",
                "semantic_confidence": classification.confidence,
                "semantic_reason": classification.reason,
            }
        )
    return output


async def fetch_assets(conn: Any, run_id: UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"""
        SELECT *
        FROM (
            SELECT DISTINCT ON (rw.market_id, rw.pass_number, r.resolved_symbol)
                   rw.market_id, rw.pass_number, w.event_id, w.as_of,
                   m.question, e.title AS event_title, e.tags,
                   r.resolved_symbol AS symbol,
                   COALESCE(r.security_name, a.asset_name) AS asset_name,
                   a.asset_class, a.reason
            FROM {SCHEMA}.historical_run_worlds rw
            JOIN {SCHEMA}.historical_asset_worlds w ON w.world_id=rw.world_id
            JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id=rw.world_id
            JOIN {SCHEMA}.historical_run_asset_resolutions r
              ON r.run_id=rw.run_id AND r.original_symbol=a.symbol
            JOIN {SCHEMA}.historical_run_markets m
              ON m.run_id=rw.run_id AND m.market_id=rw.market_id
            JOIN {SCHEMA}.source_events e ON e.event_id=w.event_id
            WHERE rw.run_id=$1 AND r.resolved_symbol IS NOT NULL
            ORDER BY rw.market_id, rw.pass_number, r.resolved_symbol,
                     CASE WHEN a.symbol=r.resolved_symbol THEN 0 ELSE 1 END, a.symbol
        ) resolved
        ORDER BY as_of, market_id, pass_number, symbol
        """,
        run_id,
    )
    return [
        assignment_record(dict(row), symbol=str(row["symbol"]), asset_name=str(row["asset_name"]))
        for row in rows
    ]


async def fetch_observations(conn: Any, run_id: UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"""
        SELECT o.observation_id, o.event_id, o.market_id, o.first_pass_at,
               o.label_available_at, o.symbol, o.event_archetype AS old_event_archetype,
               o.resolution, o.classification_target, o.regression_target,
               o.valid_for_training, o.exclusion_reason,
               m.question, m.final_outcome, e.title AS event_title, e.tags,
               COALESCE(am.asset_name, r.security_name, o.symbol) AS asset_name
        FROM {SCHEMA}.historical_ml_observations o
        JOIN {SCHEMA}.historical_run_markets m
          ON m.run_id=o.run_id AND m.market_id=o.market_id
        JOIN {SCHEMA}.source_events e ON e.event_id=o.event_id
        LEFT JOIN {SCHEMA}.historical_asset_metadata am ON am.symbol=o.symbol
        LEFT JOIN {SCHEMA}.historical_run_asset_resolutions r
          ON r.run_id=o.run_id AND r.resolved_symbol=o.symbol
        WHERE o.run_id=$1
        ORDER BY o.first_pass_at, o.event_id, o.symbol
        """,
        run_id,
    )
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for row in rows:
        observation_id = str(row["observation_id"])
        if observation_id in seen:
            continue
        seen.add(observation_id)
        output.append(
            assignment_record(
                dict(row),
                symbol=str(row["symbol"]),
                asset_name=str(row["asset_name"]),
            )
        )
    return output


async def fetch_trades(conn: Any, run_id: UUID) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        f"""
        SELECT t.*, e.title AS event_title, e.tags,
               CASE
                   WHEN t.resolution <> '1h' THEN TRUE
                   WHEN t.portfolio = 'polymarket_momentum' THEN TRUE
                   WHEN momentum.latest_close IS NULL OR momentum.lookback_close IS NULL THEN FALSE
                   ELSE momentum.latest_close > momentum.lookback_close
               END AS price_confirmed
        FROM {SCHEMA}.historical_trades t
        JOIN {SCHEMA}.source_events e ON e.event_id=t.event_id
        LEFT JOIN LATERAL (
            SELECT
                MAX(close) FILTER (WHERE row_number=1) AS latest_close,
                MAX(close) FILTER (WHERE row_number=15) AS lookback_close
            FROM (
                SELECT close, ROW_NUMBER() OVER (ORDER BY ts DESC) AS row_number
                FROM {SCHEMA}.historical_price_bars
                WHERE symbol=t.symbol AND resolution='1h' AND ts<t.entry_at
                ORDER BY ts DESC
                LIMIT 15
            ) prior_bars
        ) momentum ON t.resolution='1h'
        WHERE t.run_id=$1
        ORDER BY t.entry_at, t.trade_id
        """,
        run_id,
    )
    return [
        assignment_record(dict(row), symbol=str(row["symbol"]), asset_name=str(row["asset_name"]))
        for row in rows
    ]


def group_summary(
    trades: list[dict[str, Any]],
    assets: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    output = []
    for group in sorted(PROPOSED_GROUPS):
        group_trades = [row for row in trades if row["semantic_group"] == group]
        group_assets = [row for row in assets if row["semantic_group"] == group]
        group_observations = [row for row in observations if row["semantic_group"] == group]
        valid_observations = [row for row in group_observations if row["valid_for_training"]]
        yes_observations = [
            row
            for row in valid_observations
            if str(row.get("final_outcome") or "").lower() == "yes"
        ]
        no_observations = [
            row
            for row in valid_observations
            if str(row.get("final_outcome") or "").lower() == "no"
        ]
        eligible_trades = [
            row
            for row in group_trades
            if row["long_eligible"]
            and row["direction"] == "long"
            and (row["resolution"] != "1h" or bool(row["price_confirmed"]))
        ]
        stats = performance(group_trades)
        eligible_stats = performance(eligible_trades)
        output.append(
            {
                "semantic_group": group,
                "long_eligible": group in LONG_ELIGIBLE_GROUPS,
                "selected_asset_rows": len(group_assets),
                "selected_questions": len({str(row["market_id"]) for row in group_assets}),
                "observation_count": len(group_observations),
                "valid_observation_count": sum(bool(row["valid_for_training"]) for row in group_observations),
                "observation_events": len({str(row["event_id"]) for row in group_observations}),
                "observation_symbols": len({str(row["symbol"]) for row in group_observations}),
                "yes_outcome_observations": len(yes_observations),
                "yes_outcome_positive_label_rate": (
                    sum(int(row["classification_target"]) == 1 for row in yes_observations)
                    / len(yes_observations)
                    if yes_observations
                    else None
                ),
                "yes_outcome_average_target": (
                    sum(float(row["regression_target"]) for row in yes_observations)
                    / len(yes_observations)
                    if yes_observations
                    else None
                ),
                "no_outcome_observations": len(no_observations),
                "no_outcome_positive_label_rate": (
                    sum(int(row["classification_target"]) == 1 for row in no_observations)
                    / len(no_observations)
                    if no_observations
                    else None
                ),
                "no_outcome_average_target": (
                    sum(float(row["regression_target"]) for row in no_observations)
                    / len(no_observations)
                    if no_observations
                    else None
                ),
                "eligible_clustered_mean_net_95ci": clustered_mean_ci(eligible_trades),
                **{f"eligible_{key}": value for key, value in eligible_stats.items()},
                **stats,
            }
        )
    return output


def filter_cascade(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    classified = [row for row in trades if row["semantic_group"]]
    positive = [row for row in classified if row["yes_outcome_polarity"] == "positive"]
    long_only = [row for row in positive if row["direction"] == "long" and row["long_eligible"]]
    confirmed = [
        row
        for row in long_only
        if row["resolution"] != "1h" or bool(row["price_confirmed"])
    ]
    return [
        {"filter": "all_completed_trades", **performance(trades)},
        {"filter": "proposed_semantic_groups_only", **performance(classified)},
        {"filter": "positive_yes_outcome_and_beneficiary_role", **performance(positive)},
        {"filter": "long_direction_only", **performance(long_only)},
        {"filter": "hourly_price_confirmation_required", **performance(confirmed)},
    ]


def chronological_holdout(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    eligible = [
        row
        for row in trades
        if row["long_eligible"]
        and row["direction"] == "long"
        and (row["resolution"] != "1h" or bool(row["price_confirmed"]))
    ]
    event_first: dict[str, datetime] = {}
    for row in eligible:
        event_id = str(row["event_id"])
        entry_at = row["entry_at"]
        event_first[event_id] = min(event_first.get(event_id, entry_at), entry_at)
    ordered = sorted(event_first, key=event_first.get)
    split = max(1, int(len(ordered) * 0.7))
    train_events = set(ordered[:split])
    holdout_events = set(ordered[split:])
    output = []
    for label, event_ids in [("first_70pct_events", train_events), ("last_30pct_events_holdout", holdout_events)]:
        rows = [row for row in eligible if str(row["event_id"]) in event_ids]
        output.append(
            {
                "period": label,
                "clustered_mean_net_95ci": clustered_mean_ci(rows),
                **performance(rows),
            }
        )
    return output


async def audit(run_id: UUID) -> Path:
    conn = await connect()
    try:
        run = await conn.fetchrow(
            f"SELECT output_dir, status, current_stage, NOW() AS audited_at FROM {SCHEMA}.historical_backtest_runs WHERE run_id=$1",
            run_id,
        )
        if run is None:
            raise ValueError(f"Unknown run_id: {run_id}")
        all_source_questions = await fetch_all_source_questions(conn)
        questions = await fetch_questions(conn, run_id)
        assets = await fetch_assets(conn, run_id)
        observations = await fetch_observations(conn, run_id)
        trades = await fetch_trades(conn, run_id)
    finally:
        await conn.close()

    output_dir = Path(run["output_dir"]) / "semantic_long_only_audit"
    question_groups = defaultdict(int)
    for row in questions:
        question_groups[str(row["semantic_outcome"] or "ambiguous")] += 1
    all_source_question_groups = defaultdict(int)
    for row in all_source_questions:
        all_source_question_groups[str(row["semantic_outcome"] or "ambiguous")] += 1
    summaries = group_summary(trades, assets, observations)
    cascade = filter_cascade(trades)
    holdout = chronological_holdout(trades)
    summary = {
        "run_id": str(run_id),
        "run_status": run["status"],
        "current_stage": run["current_stage"],
        "audited_at": run["audited_at"],
        "methodology": {
            "classification_uses_pnl": False,
            "ambiguous_questions_excluded": True,
            "ambiguous_asset_roles_excluded": True,
            "hourly_ml_price_confirmation": "14 completed hourly bars; momentum trades already require positive selected momentum",
            "confidence_interval": "event-cluster bootstrap of mean net profit per trade",
            "holdout": "latest 30% of eligible events by first trade time",
        },
        "all_database_question_count": len(all_source_questions),
        "all_database_long_relevant_question_count": sum(
            bool(row["long_consideration"]) for row in all_source_questions
        ),
        "all_database_question_outcome_counts": dict(
            sorted(all_source_question_groups.items())
        ),
        "question_outcome_counts": dict(sorted(question_groups.items())),
        "group_summary": summaries,
        "filter_cascade": cascade,
        "chronological_holdout": holdout,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "all_database_question_classifications.csv", all_source_questions)
    write_csv(
        output_dir / "all_database_long_relevant_questions.csv",
        [row for row in all_source_questions if row["long_consideration"]],
    )
    write_csv(output_dir / "question_classifications.csv", questions)
    write_csv(
        output_dir / "long_relevant_questions.csv",
        [row for row in questions if row["semantic_outcome"] in LONG_RELEVANT_OUTCOMES],
    )
    write_csv(output_dir / "selected_asset_classifications.csv", assets)
    write_csv(
        output_dir / "long_relevant_selected_assets.csv",
        [row for row in assets if row["long_eligible"]],
    )
    write_csv(output_dir / "observation_classifications.csv", observations)
    write_csv(output_dir / "trade_classifications.csv", trades)
    write_csv(output_dir / "group_summary.csv", summaries)
    write_csv(output_dir / "filter_cascade.csv", cascade)
    write_csv(output_dir / "chronological_holdout.csv", holdout)
    with (output_dir / "summary.json").open("w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2, default=str)
    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit conservative semantic long-only groups.")
    parser.add_argument("--run-id", required=True, type=UUID)
    args = parser.parse_args()
    output_dir = asyncio.run(audit(args.run_id))
    print(output_dir)


if __name__ == "__main__":
    main()
