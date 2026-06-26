from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID
import asyncpg

from database.backtesting.repositories._shared import SCHEMA, json_text


async def create_historical_run(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    config: dict[str, Any],
    hourly_boundary: datetime,
    output_dir: Path,
) -> None:
    await conn.execute(
        f"""
        INSERT INTO {SCHEMA}.historical_backtest_runs
            (run_id, status, current_stage, config, hourly_boundary, output_dir)
        VALUES ($1, 'running', 'event_filter', $2::JSONB, $3, $4)
        """,
        run_id,
        json_text(config),
        hourly_boundary,
        str(output_dir),
    )


async def historical_run(conn: asyncpg.Connection, run_id: UUID) -> asyncpg.Record:
    row = await conn.fetchrow(
        f"SELECT * FROM {SCHEMA}.historical_backtest_runs WHERE run_id = $1",
        run_id,
    )
    if row is None:
        raise ValueError(f"Historical backtest run does not exist: {run_id}")
    return row


async def stored_world_source_summary(
    conn: asyncpg.Connection,
    run_id: UUID,
) -> dict[str, Any]:
    await historical_run(conn, run_id)
    row = await conn.fetchrow(
        f"""
        SELECT
            (SELECT COUNT(*) FROM {SCHEMA}.historical_run_market_passes
             WHERE run_id=$1) AS pass_count,
            (SELECT COUNT(*) FROM {SCHEMA}.historical_run_worlds
             WHERE run_id=$1) AS world_count,
            (SELECT COUNT(*)
             FROM {SCHEMA}.historical_run_market_passes p
             WHERE p.run_id=$1
               AND NOT EXISTS (
                   SELECT 1 FROM {SCHEMA}.historical_run_worlds w
                   WHERE w.run_id=p.run_id
                     AND w.market_id=p.market_id
                     AND w.pass_number=p.pass_number
               )) AS passes_without_worlds,
            (SELECT COUNT(*)
             FROM {SCHEMA}.historical_run_worlds w
             WHERE w.run_id=$1
               AND NOT EXISTS (
                   SELECT 1 FROM {SCHEMA}.historical_run_market_passes p
                   WHERE p.run_id=w.run_id
                     AND p.market_id=w.market_id
                     AND p.pass_number=w.pass_number
               )) AS worlds_without_passes,
            (SELECT COUNT(*)
             FROM {SCHEMA}.historical_run_worlds rw
             JOIN {SCHEMA}.historical_asset_world_assets a
               ON a.world_id=rw.world_id
             WHERE rw.run_id=$1) AS asset_count,
            (SELECT ARRAY_AGG(DISTINCT w.model_name ORDER BY w.model_name)
             FROM {SCHEMA}.historical_run_worlds rw
             JOIN {SCHEMA}.historical_asset_worlds w
               ON w.world_id=rw.world_id
             WHERE rw.run_id=$1) AS model_names
        """,
        run_id,
    )
    summary = dict(row)
    if not summary["world_count"]:
        raise ValueError(f"Source run has no stored asset worlds: {run_id}")
    if summary["passes_without_worlds"] or summary["worlds_without_passes"]:
        raise ValueError(
            "Source run does not have complete pass/world coverage: "
            f"passes_without_worlds={summary['passes_without_worlds']} "
            f"worlds_without_passes={summary['worlds_without_passes']}"
        )
    return summary


async def clone_stored_run_decisions(
    conn: asyncpg.Connection,
    *,
    source_run_id: UUID,
    target_run_id: UUID,
) -> None:
    await conn.execute(
        f"""
        INSERT INTO {SCHEMA}.historical_run_event_decisions
            (run_id, event_id, input_hash)
        SELECT $2, event_id, input_hash
        FROM {SCHEMA}.historical_run_event_decisions
        WHERE run_id=$1
        ON CONFLICT (run_id, event_id) DO UPDATE
        SET input_hash=EXCLUDED.input_hash
        """,
        source_run_id,
        target_run_id,
    )
    await conn.execute(
        f"""
        INSERT INTO {SCHEMA}.historical_run_market_decisions
            (run_id, market_id, input_hash)
        SELECT $2, market_id, input_hash
        FROM {SCHEMA}.historical_run_market_decisions
        WHERE run_id=$1
        ON CONFLICT (run_id, market_id) DO UPDATE
        SET input_hash=EXCLUDED.input_hash
        """,
        source_run_id,
        target_run_id,
    )


async def clone_stored_world_state(
    conn: asyncpg.Connection,
    *,
    source_run_id: UUID,
    target_run_id: UUID,
    market_ids: list[str],
) -> dict[str, int]:
    if not market_ids:
        raise ValueError(
            "No source markets remain after applying the current semantic filters"
        )
    await conn.execute(
        f"""
        INSERT INTO {SCHEMA}.historical_run_markets (
            run_id, market_id, event_id, question, created_at, end_at,
            final_outcome, probability_hour_count, probability_graph_path
        )
        SELECT $2, market_id, event_id, question, created_at, end_at,
               final_outcome, probability_hour_count, probability_graph_path
        FROM {SCHEMA}.historical_run_markets
        WHERE run_id=$1 AND market_id=ANY($3::TEXT[])
        ON CONFLICT (run_id, market_id) DO NOTHING
        """,
        source_run_id,
        target_run_id,
        market_ids,
    )
    await conn.execute(
        f"""
        INSERT INTO {SCHEMA}.historical_run_market_passes (
            run_id, market_id, event_id, question, pass_number, above_at,
            above_probability, fell_below_at, fell_below_probability, final_outcome
        )
        SELECT $2, market_id, event_id, question, pass_number, above_at,
               above_probability, fell_below_at, fell_below_probability, final_outcome
        FROM {SCHEMA}.historical_run_market_passes
        WHERE run_id=$1 AND market_id=ANY($3::TEXT[])
        ON CONFLICT (run_id, market_id, pass_number) DO NOTHING
        """,
        source_run_id,
        target_run_id,
        market_ids,
    )
    await conn.execute(
        f"""
        INSERT INTO {SCHEMA}.historical_run_worlds
            (run_id, market_id, pass_number, world_id)
        SELECT $2, market_id, pass_number, world_id
        FROM {SCHEMA}.historical_run_worlds
        WHERE run_id=$1 AND market_id=ANY($3::TEXT[])
        ON CONFLICT (run_id, market_id, pass_number) DO NOTHING
        """,
        source_run_id,
        target_run_id,
        market_ids,
    )
    row = await conn.fetchrow(
        f"""
        SELECT
            (SELECT COUNT(*) FROM {SCHEMA}.historical_run_markets
             WHERE run_id=$1) AS market_count,
            (SELECT COUNT(*) FROM {SCHEMA}.historical_run_market_passes
             WHERE run_id=$1) AS pass_count,
            (SELECT COUNT(*) FROM {SCHEMA}.historical_run_worlds
             WHERE run_id=$1) AS world_count
        """,
        target_run_id,
    )
    summary = dict(row)
    if not summary["world_count"] or summary["pass_count"] != summary["world_count"]:
        raise ValueError(
            "Stored world clone is incomplete: "
            f"passes={summary['pass_count']} worlds={summary['world_count']}"
        )
    return summary


async def update_run(
    conn: asyncpg.Connection,
    run_id: UUID,
    *,
    status: str,
    stage: str | None = None,
    error: str | None = None,
) -> None:
    await conn.execute(
        f"""
        UPDATE {SCHEMA}.historical_backtest_runs
        SET status = $2,
            current_stage = COALESCE($3, current_stage),
            error = $4,
            finished_at = CASE WHEN $2 IN ('complete', 'failed') THEN NOW() ELSE NULL END
        WHERE run_id = $1
        """,
        run_id,
        status,
        stage,
        error,
    )


async def start_work(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    stage: str,
    work_key: str,
    payload: dict[str, Any],
) -> bool:
    row = await conn.fetchrow(
        f"""
        INSERT INTO {SCHEMA}.historical_backtest_stage_work
            (run_id, stage, work_key, status, attempts, payload, started_at)
        VALUES ($1, $2, $3, 'running', 1, $4::JSONB, NOW())
        ON CONFLICT (run_id, stage, work_key) DO UPDATE SET
            status = 'running',
            attempts = {SCHEMA}.historical_backtest_stage_work.attempts + 1,
            payload = EXCLUDED.payload,
            error = NULL,
            started_at = NOW(),
            finished_at = NULL
        WHERE {SCHEMA}.historical_backtest_stage_work.status <> 'complete'
        RETURNING work_key
        """,
        run_id,
        stage,
        work_key,
        json_text(payload),
    )
    return row is not None


async def finish_work(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    stage: str,
    work_key: str,
    result: dict[str, Any] | None = None,
) -> None:
    await conn.execute(
        f"""
        UPDATE {SCHEMA}.historical_backtest_stage_work
        SET status = 'complete', result = $4::JSONB, error = NULL, finished_at = NOW()
        WHERE run_id = $1 AND stage = $2 AND work_key = $3
        """,
        run_id,
        stage,
        work_key,
        json_text(result or {}),
    )


async def record_stage_failure(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    stage: str,
    work_key: str | None,
    error: BaseException,
) -> None:
    if work_key is not None:
        await conn.execute(
            f"""
            UPDATE {SCHEMA}.historical_backtest_stage_work
            SET status = 'failed', error = $4, finished_at = NOW()
            WHERE run_id = $1 AND stage = $2 AND work_key = $3
            """,
            run_id,
            stage,
            work_key,
            str(error)[:10_000],
        )
    await conn.execute(
        f"""
        INSERT INTO {SCHEMA}.historical_run_failures
            (run_id, stage, work_key, error_type, error)
        VALUES ($1, $2, $3, $4, $5)
        """,
        run_id,
        stage,
        work_key,
        type(error).__name__,
        str(error)[:10_000],
    )
    await update_run(conn, run_id, status="failed", stage=stage, error=str(error))


async def purge_run(conn: asyncpg.Connection, run_id: UUID) -> str | None:
    output_dir = await conn.fetchval(
        f"SELECT output_dir FROM {SCHEMA}.historical_backtest_runs WHERE run_id = $1",
        run_id,
    )
    if output_dir is None:
        return None
    run_specific_tables = [
        "historical_ml_predictions",
        "historical_momentum_parameter_results",
        "historical_trades",
        "historical_run_asset_resolutions",
        "historical_run_market_passes",
        "historical_run_markets",
        "historical_run_worlds",
        "historical_run_sentiments",
        "historical_run_market_decisions",
        "historical_run_event_decisions",
        "historical_run_failures",
        "historical_backtest_stage_work",
    ]
    async with conn.transaction():
        for table in run_specific_tables:
            await conn.execute(
                f"DELETE FROM {SCHEMA}.{table} WHERE run_id = $1",
                run_id,
            )
        await conn.execute(
            f"""
            UPDATE {SCHEMA}.historical_backtest_runs
            SET status='purged', current_stage='purged', error=NULL, finished_at=NOW()
            WHERE run_id=$1
            """,
            run_id,
        )
    return output_dir
