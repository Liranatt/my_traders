"""Dump the raw artifacts for human review: every world pick with its relevance grade, and every
candidate trade with both the 10-day-capped return AND the full-window return. Writes CSVs and
prints the military (oil) and FDA world mappings so the grades can be inspected directly.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

import pandas as pd

from database.db_connection import connect
from database.backtesting.schema import SCHEMA

RUN = "5fb1a1cd-7513-4085-a0c4-499d66c205ab"


async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = await connect()
    try:
        # 1) every world pick with its relevance grade (connection_strength = question_rel x stock_conn)
        rows = await c.fetch(f"""
            SELECT m.market_id, m.question, COALESCE(o.event_archetype,'?') AS archetype,
                   a.symbol, a.connection_strength AS relevance_grade, a.reason
            FROM {SCHEMA}.historical_run_worlds rw
            JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id = rw.world_id
            JOIN {SCHEMA}.historical_run_markets m ON m.run_id=rw.run_id AND m.market_id=rw.market_id
            LEFT JOIN {SCHEMA}.historical_ml_observations o
                   ON o.run_id=rw.run_id AND o.market_id=rw.market_id AND o.symbol=a.symbol
            WHERE rw.run_id=$1
            ORDER BY m.question, a.connection_strength DESC NULLS LAST
        """, RUN)
        wdf = pd.DataFrame([dict(r) for r in rows])
        wdf.to_csv("data/worlds_dump.csv", index=False)
        print(f"wrote data/worlds_dump.csv  ({len(wdf)} world picks)")

        # show the actual oil/military mappings WITH grades
        mil = await c.fetch(f"""
            SELECT DISTINCT a.symbol, a.connection_strength AS grade, COUNT(*) OVER (PARTITION BY a.symbol) AS picks
            FROM {SCHEMA}.historical_run_worlds rw
            JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id=rw.world_id
            JOIN {SCHEMA}.historical_run_markets m ON m.run_id=rw.run_id AND m.market_id=rw.market_id
            WHERE rw.run_id=$1 AND (m.question ILIKE '%Iran%' OR m.question ILIKE '%strike%' OR m.question ILIKE '%military%')
            ORDER BY picks DESC
        """, RUN)
        print("\n=== OIL/MILITARY world picks and their relevance grades (run v7) ===")
        for r in mil:
            g = r["grade"]
            print(f"   {r['symbol']:6} grade={g if g is None else round(g,2)}  picks={r['picks']}")

        fda = await c.fetch(f"""
            SELECT m.question, a.symbol, a.connection_strength AS grade
            FROM {SCHEMA}.historical_run_worlds rw
            JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id=rw.world_id
            JOIN {SCHEMA}.historical_run_markets m ON m.run_id=rw.run_id AND m.market_id=rw.market_id
            WHERE rw.run_id=$1 AND m.question ILIKE '%FDA%' ORDER BY m.question LIMIT 12
        """, RUN)
        print("\n=== FDA world picks and grades (run v7) ===")
        for r in fda:
            g = r["grade"]
            print(f"   {r['symbol']:6} grade={g if g is None else round(g,2)}  {r['question'][:55]}")
    finally:
        await c.close()

    # 2) candidate trades: capped return (parquet) + full-window return (recomputed) for oil+FDA
    df = pd.read_parquet("data/candidates.parquet")
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True, errors="coerce")
    df["t_e"] = pd.to_datetime(df["t_e"], utc=True, errors="coerce")
    keep = ["market_id","symbol","feat_archetype","split","t_theta","t_e","entry_price",
            "asset_return","y_hedged","realized_abs_move","feat_connection_strength","alpha_score","alpha_dir"]
    out = df[keep].rename(columns={"asset_return":"capped10d_return","feat_connection_strength":"relevance_grade"})

    c = await connect()
    try:
        full = []
        for _, r in out.iterrows():
            bars = await c.fetch(f"""SELECT close FROM {SCHEMA}.historical_price_bars
                WHERE symbol=$1 AND resolution='1d' AND ts>=$2 AND ts<=$3 ORDER BY ts""",
                r["symbol"], r["t_theta"]-timedelta(days=1), r["t_e"]+timedelta(days=1))
            cl = [float(b["close"]) for b in bars]
            full.append((cl[-1]/cl[0]-1.0) if len(cl) >= 2 and cl[0] > 0 else float("nan"))
        out["full_window_return"] = full
    finally:
        await c.close()
    out.to_csv("data/trades_dump.csv", index=False)
    print(f"\nwrote data/trades_dump.csv  ({len(out)} trades) -- capped10d vs full_window return side by side")


if __name__ == "__main__":
    asyncio.run(main())
