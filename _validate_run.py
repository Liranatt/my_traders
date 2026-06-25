"""Validate the latest pipeline run: completion, market timeline (2023-2026 gaps), v9 worlds,
and Polymarket (probability) + Yahoo (price + fundamentals) coverage for the world assets."""
from __future__ import annotations

import asyncio
import json
import sys

from database.db_connection import connect
from database.backtesting.schema import SCHEMA


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    c = await connect()
    try:
        run = await c.fetchrow(f"""SELECT run_id, status, current_stage, error, started_at, finished_at, config
            FROM {SCHEMA}.historical_backtest_runs ORDER BY started_at DESC LIMIT 1""")
        rid = run["run_id"]
        cfg = run["config"] if isinstance(run["config"], dict) else json.loads(run["config"])
        print("="*90)
        print(f"RUN {rid}")
        print(f"  status={run['status']}  stage={run['current_stage']}  error={run['error']}")
        print(f"  started={run['started_at']}  finished={run['finished_at']}")
        print(f"  config window: start={cfg.get('start')}  end={cfg.get('end')}  cutoff={cfg.get('historical_data_cutoff')}")
        print(f"  asset_world_prompt_version={cfg.get('asset_world_prompt_version')}")
        sw = await c.fetch(f"SELECT stage,status,COUNT(*) n FROM {SCHEMA}.historical_backtest_stage_work WHERE run_id=$1 GROUP BY stage,status ORDER BY stage", rid)
        print("  stages:", {f"{r['stage']}:{r['status']}": r["n"] for r in sw})

        # ---- MARKETS timeline ----
        print("\n" + "="*90 + "\nMARKETS in this run (by created_at month) -- gaps flagged")
        rows = await c.fetch(f"""SELECT to_char(created_at,'YYYY-MM') ym, COUNT(*) n
            FROM {SCHEMA}.historical_run_markets WHERE run_id=$1 GROUP BY ym ORDER BY ym""", rid)
        months = {r["ym"]: r["n"] for r in rows}
        if months:
            ymin, ymax = min(months), max(months)
            print(f"  range: {ymin} .. {ymax}   total markets={sum(months.values())}")
            # walk every month between min and max; flag missing
            import datetime as dt
            y, m = map(int, ymin.split("-")); ey, em = map(int, ymax.split("-"))
            line = []
            while (y, m) <= (ey, em):
                key = f"{y:04d}-{m:02d}"
                line.append(f"{key}={months.get(key,0)}")
                if months.get(key, 0) == 0:
                    line[-1] += "<<GAP"
                m += 1
                if m > 12: m = 1; y += 1
            for i in range(0, len(line), 6):
                print("   ", "  ".join(line[i:i+6]))
        # raw probability data range (what Polymarket data we actually hold)
        pr = await c.fetchrow(f"SELECT MIN(hour_ts) mn, MAX(hour_ts) mx, COUNT(DISTINCT market_id) m FROM {SCHEMA}.historical_probability_points")
        print(f"\n  ALL Polymarket probability data: {pr['mn']} .. {pr['mx']}  ({pr['m']} markets)")

        # ---- WORLDS ----
        print("\n" + "="*90 + "\nWORLDS")
        w = await c.fetchrow(f"""SELECT COUNT(*) linked,
            COUNT(*) FILTER (WHERE a.world_id IS NULL) empty
            FROM {SCHEMA}.historical_run_worlds rw
            JOIN {SCHEMA}.historical_asset_worlds aw ON aw.world_id=rw.world_id
            LEFT JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id=aw.world_id
            WHERE rw.run_id=$1""", rid)
        wv = await c.fetch(f"""SELECT aw.prompt_version, COUNT(DISTINCT rw.world_id) n
            FROM {SCHEMA}.historical_run_worlds rw JOIN {SCHEMA}.historical_asset_worlds aw ON aw.world_id=rw.world_id
            WHERE rw.run_id=$1 GROUP BY aw.prompt_version""", rid)
        nworlds = await c.fetchval(f"SELECT COUNT(*) FROM {SCHEMA}.historical_run_worlds WHERE run_id=$1", rid)
        empties = await c.fetchval(f"""SELECT COUNT(*) FROM {SCHEMA}.historical_run_worlds rw
            JOIN {SCHEMA}.historical_asset_worlds aw ON aw.world_id=rw.world_id
            WHERE rw.run_id=$1 AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.historical_asset_world_assets a WHERE a.world_id=aw.world_id)""", rid)
        cs = await c.fetchrow(f"""SELECT COUNT(*) total, COUNT(connection_strength) graded
            FROM {SCHEMA}.historical_run_worlds rw JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id=rw.world_id
            WHERE rw.run_id=$1""", rid)
        print(f"  worlds linked={nworlds}  empty(no assets)={empties}  prompt_versions={[(r['prompt_version'][-22:], r['n']) for r in wv]}")
        print(f"  asset picks={cs['total']}  connection_strength populated={cs['graded']}")
        # sample 4 non-empty worlds
        sample = await c.fetch(f"""SELECT m.question, a.symbol, a.connection_strength, LEFT(a.reason,70) reason
            FROM {SCHEMA}.historical_run_worlds rw
            JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id=rw.world_id
            JOIN {SCHEMA}.historical_run_markets m ON m.run_id=rw.run_id AND m.market_id=rw.market_id
            WHERE rw.run_id=$1 ORDER BY random() LIMIT 8""", rid)
        print("  sample picks:")
        for r in sample:
            print(f"    {r['symbol']:6} cs={r['connection_strength']}  {str(r['question'])[:48]:48} | {r['reason']}")

        # ---- POLYMARKET coverage for THIS run's markets ----
        print("\n" + "="*90 + "\nPOLYMARKET (probability) COVERAGE for this run's markets")
        cov = await c.fetchrow(f"""WITH mk AS (SELECT DISTINCT market_id FROM {SCHEMA}.historical_run_markets WHERE run_id=$1)
            SELECT COUNT(*) total, COUNT(*) FILTER (WHERE EXISTS
              (SELECT 1 FROM {SCHEMA}.historical_probability_points p WHERE p.market_id=mk.market_id)) with_prob
            FROM mk""", rid)
        print(f"  run markets={cov['total']}  with probability points={cov['with_prob']}  missing={cov['total']-cov['with_prob']}")

        # ---- YAHOO coverage for the world assets ----
        print("\n" + "="*90 + "\nYAHOO (price + fundamentals) COVERAGE for world assets")
        syms = await c.fetch(f"""SELECT DISTINCT a.symbol FROM {SCHEMA}.historical_run_worlds rw
            JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id=rw.world_id WHERE rw.run_id=$1""", rid)
        syms = [r["symbol"] for r in syms]
        price = await c.fetch(f"""SELECT symbol, COUNT(*) n, MIN(ts) mn, MAX(ts) mx FROM {SCHEMA}.historical_price_bars
            WHERE resolution='1d' AND symbol=ANY($1::text[]) GROUP BY symbol""", syms)
        priced = {r["symbol"] for r in price}
        missing_px = [s for s in syms if s not in priced]
        prng = await c.fetchrow(f"SELECT MIN(ts) mn, MAX(ts) mx FROM {SCHEMA}.historical_price_bars WHERE resolution='1d' AND symbol=ANY($1::text[])", syms)
        fund = await c.fetchval(f"SELECT COUNT(DISTINCT symbol) FROM {SCHEMA}.historical_asset_fundamentals WHERE symbol=ANY($1::text[])", syms)
        print(f"  distinct world-asset symbols={len(syms)}")
        print(f"  with daily prices={len(priced)}  missing prices={len(missing_px)} {missing_px[:20]}")
        print(f"  price date range (these symbols): {prng['mn']} .. {prng['mx']}")
        print(f"  with fundamentals={fund}/{len(syms)}")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
