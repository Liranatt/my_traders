from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import asyncpg

from database.backtesting.repositories._shared import SCHEMA, json_text, json_value
from LLM.build_world import AssetWorld
from main_backtesting.models import IBTradableAsset


async def create_asset_selection_experiment(
    conn: asyncpg.Connection,
    *,
    experiment_id: UUID,
    source_run_id: UUID,
    query_limit: int,
    sample_seed: int,
    model_name: str,
    catalog_hash: str,
    catalog_asset_count: int,
    output_dir: Path,
) -> None:
    await conn.execute(
        f"""
        INSERT INTO {SCHEMA}.historical_asset_selection_experiments (
            experiment_id, source_run_id, status, query_limit, sample_seed,
            model_name, catalog_hash, catalog_asset_count, output_dir
        )
        VALUES ($1,$2,'running',$3,$4,$5,$6,$7,$8)
        """,
        experiment_id,
        source_run_id,
        query_limit,
        sample_seed,
        model_name,
        catalog_hash,
        catalog_asset_count,
        str(output_dir),
    )


async def asset_selection_experiment(
    conn: asyncpg.Connection,
    experiment_id: UUID,
) -> asyncpg.Record:
    row = await conn.fetchrow(
        f"""
        SELECT * FROM {SCHEMA}.historical_asset_selection_experiments
        WHERE experiment_id=$1
        """,
        experiment_id,
    )
    if row is None:
        raise ValueError(f"Asset-selection experiment does not exist: {experiment_id}")
    return row


async def update_asset_selection_experiment(
    conn: asyncpg.Connection,
    experiment_id: UUID,
    *,
    status: str,
    error: str | None = None,
) -> None:
    await conn.execute(
        f"""
        UPDATE {SCHEMA}.historical_asset_selection_experiments
        SET status=$2, error=$3,
            finished_at=CASE WHEN $2 IN ('complete','failed') THEN NOW() ELSE NULL END
        WHERE experiment_id=$1
        """,
        experiment_id,
        status,
        error,
    )


async def source_run_experiment_queries(
    conn: asyncpg.Connection,
    *,
    source_run_id: UUID,
    limit: int,
    sample_seed: int,
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT p.market_id, rm.event_id, p.pass_number, p.above_at AS as_of,
                   COALESCE(d.event_title, rm.question) AS event_title,
                   rm.question, rm.created_at AS market_created_at,
                   rm.end_at AS market_end_at, rm.final_outcome, d.llm_input
            FROM {SCHEMA}.historical_run_market_passes p
            JOIN {SCHEMA}.historical_run_markets rm
              ON rm.run_id=p.run_id AND rm.market_id=p.market_id
            LEFT JOIN {SCHEMA}.historical_run_market_decisions rd
              ON rd.run_id=p.run_id AND rd.market_id=p.market_id
            LEFT JOIN {SCHEMA}.historical_market_decisions d
              ON d.input_hash=rd.input_hash
            WHERE p.run_id=$1
            ORDER BY MD5(p.market_id || ':' || p.pass_number::TEXT || ':' || $3::INTEGER::TEXT),
                     p.market_id, p.pass_number
            LIMIT $2
            """,
            source_run_id,
            limit,
            sample_seed,
        )
    )


def market_tags_from_llm_input(value: Any, market_id: str) -> list[str]:
    parsed = json_value(value)
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, dict):
        return []
    payload = parsed.get("payload", {})
    markets = payload.get("markets", []) if isinstance(payload, dict) else []
    for market in markets:
        if isinstance(market, dict) and str(market.get("market_id")) == str(market_id):
            tags = market.get("tags", [])
            return [str(tag) for tag in tags] if isinstance(tags, list) else []
    return []


async def save_experiment_queries(
    conn: asyncpg.Connection,
    *,
    experiment_id: UUID,
    rows: list[asyncpg.Record],
) -> None:
    await conn.executemany(
        f"""
        INSERT INTO {SCHEMA}.historical_asset_selection_experiment_queries (
            experiment_id, query_index, market_id, event_id, pass_number, as_of,
            event_title, question, tags, market_created_at, market_end_at, final_outcome
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::JSONB,$10,$11,$12)
        ON CONFLICT (experiment_id, query_index) DO NOTHING
        """,
        [
            (
                experiment_id,
                index,
                row["market_id"],
                row["event_id"],
                row["pass_number"],
                row["as_of"],
                row["event_title"],
                row["question"],
                json_text(market_tags_from_llm_input(row["llm_input"], row["market_id"])),
                row["market_created_at"],
                row["market_end_at"],
                row["final_outcome"],
            )
            for index, row in enumerate(rows, 1)
        ],
    )


async def experiment_queries(
    conn: asyncpg.Connection,
    experiment_id: UUID,
) -> list[asyncpg.Record]:
    return list(
        await conn.fetch(
            f"""
            SELECT * FROM {SCHEMA}.historical_asset_selection_experiment_queries
            WHERE experiment_id=$1
            ORDER BY query_index
            """,
            experiment_id,
        )
    )


async def completed_experiment_arms(
    conn: asyncpg.Connection,
    experiment_id: UUID,
) -> set[tuple[int, str]]:
    rows = await conn.fetch(
        f"""
        SELECT query_index, arm
        FROM {SCHEMA}.historical_asset_selection_experiment_results
        WHERE experiment_id=$1 AND status='complete'
        """,
        experiment_id,
    )
    return {(int(row["query_index"]), str(row["arm"])) for row in rows}


async def save_experiment_result(
    conn: asyncpg.Connection,
    *,
    experiment_id: UUID,
    query_index: int,
    arm: str,
    status: str,
    duration_seconds: float,
    candidate_count: int | None,
    method_input: dict[str, Any],
    method_output: dict[str, Any],
    world: AssetWorld | None,
    catalog_by_symbol: dict[str, IBTradableAsset],
    error: str | None = None,
) -> None:
    async with conn.transaction():
        await conn.execute(
            f"""
            INSERT INTO {SCHEMA}.historical_asset_selection_experiment_results (
                experiment_id, query_index, arm, status, duration_seconds,
                candidate_count, universe_name, universe_reason,
                method_input, method_output, error
            )
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::JSONB,$10::JSONB,$11)
            ON CONFLICT (experiment_id, query_index, arm) DO UPDATE SET
                status=EXCLUDED.status,
                duration_seconds=EXCLUDED.duration_seconds,
                candidate_count=EXCLUDED.candidate_count,
                universe_name=EXCLUDED.universe_name,
                universe_reason=EXCLUDED.universe_reason,
                method_input=EXCLUDED.method_input,
                method_output=EXCLUDED.method_output,
                error=EXCLUDED.error,
                created_at=NOW()
            """,
            experiment_id,
            query_index,
            arm,
            status,
            duration_seconds,
            candidate_count,
            world.universe_name if world else None,
            world.universe_reason if world else None,
            json_text(method_input),
            json_text(method_output),
            error,
        )
        await conn.execute(
            f"""
            DELETE FROM {SCHEMA}.historical_asset_selection_experiment_assets
            WHERE experiment_id=$1 AND query_index=$2 AND arm=$3
            """,
            experiment_id,
            query_index,
            arm,
        )
        if world is not None:
            rows = []
            for asset in world.assets:
                metadata = catalog_by_symbol.get(asset.symbol)
                rows.append(
                    (
                        experiment_id,
                        query_index,
                        arm,
                        asset.symbol,
                        asset.asset_name,
                        asset.asset_class,
                        asset.relationship_type,
                        asset.reason,
                        metadata.primary_exchange if metadata else None,
                        metadata.stock_type if metadata else None,
                        metadata.industry if metadata else None,
                        metadata.category if metadata else None,
                        metadata.subcategory if metadata else None,
                    )
                )
            await conn.executemany(
                f"""
                INSERT INTO {SCHEMA}.historical_asset_selection_experiment_assets (
                    experiment_id, query_index, arm, symbol, asset_name, asset_class,
                    relationship_type, reason, primary_exchange, stock_type,
                    industry, category, subcategory
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
                """,
                rows,
            )
