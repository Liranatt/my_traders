from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

import asyncpg

from database.backtesting.repositories._shared import SCHEMA
from database.backtesting.security_master import SecurityMasterEntry, SecurityResolution


async def security_master_entries(conn: asyncpg.Connection) -> list[SecurityMasterEntry]:
    rows = await conn.fetch(
        f"""
        SELECT official_symbol, yfinance_symbol, security_name, exchange, is_etf, source
        FROM {SCHEMA}.historical_us_security_master
        ORDER BY official_symbol
        """
    )
    return [SecurityMasterEntry(**dict(row)) for row in rows]


async def save_security_master_entries(
    conn: asyncpg.Connection,
    entries: Iterable[SecurityMasterEntry],
) -> None:
    rows = [
        (
            entry.official_symbol,
            entry.yfinance_symbol,
            entry.security_name,
            entry.exchange,
            entry.is_etf,
            entry.source,
        )
        for entry in entries
    ]
    async with conn.transaction():
        await conn.executemany(
            f"""
            INSERT INTO {SCHEMA}.historical_us_security_master (
                official_symbol, yfinance_symbol, security_name, exchange, is_etf, source
            )
            VALUES ($1,$2,$3,$4,$5,$6)
            ON CONFLICT (official_symbol) DO UPDATE SET
                yfinance_symbol = EXCLUDED.yfinance_symbol,
                security_name = EXCLUDED.security_name,
                exchange = EXCLUDED.exchange,
                is_etf = EXCLUDED.is_etf,
                source = EXCLUDED.source,
                updated_at = NOW()
            """,
            rows,
        )


async def save_run_asset_resolution(
    conn: asyncpg.Connection,
    *,
    run_id: UUID,
    resolution: SecurityResolution,
) -> None:
    entry = resolution.entry
    await conn.execute(
        f"""
        INSERT INTO {SCHEMA}.historical_run_asset_resolutions (
            run_id, original_symbol, resolved_symbol, official_symbol,
            security_name, exchange, is_etf, match_method, rejection_reason
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        ON CONFLICT (run_id, original_symbol) DO UPDATE SET
            resolved_symbol = EXCLUDED.resolved_symbol,
            official_symbol = EXCLUDED.official_symbol,
            security_name = EXCLUDED.security_name,
            exchange = EXCLUDED.exchange,
            is_etf = EXCLUDED.is_etf,
            match_method = EXCLUDED.match_method,
            rejection_reason = EXCLUDED.rejection_reason,
            updated_at = NOW()
        """,
        run_id,
        resolution.original_symbol,
        entry.yfinance_symbol if entry else None,
        entry.official_symbol if entry else None,
        entry.security_name if entry else None,
        entry.exchange if entry else None,
        entry.is_etf if entry else None,
        resolution.match_method,
        resolution.rejection_reason,
    )


async def run_asset_resolutions(conn: asyncpg.Connection, run_id: UUID) -> list[Any]:
    return await conn.fetch(
        f"""
        SELECT *
        FROM {SCHEMA}.historical_run_asset_resolutions
        WHERE run_id = $1
        ORDER BY original_symbol
        """,
        run_id,
    )
