"""Check what hour_ts values actually look like to see if we're using post-market prob data."""
import asyncio
import pandas as pd
from database.db_connection import connect
from database.backtesting.schema import SCHEMA

async def check():
    c = await connect()
    try:
        # Get a sample of prob points to see what times they fall on
        rows = await c.fetch(f"""
            SELECT market_id, hour_ts AT TIME ZONE 'UTC' AS hour_utc,
                   (hour_ts AT TIME ZONE 'UTC')::date AS d, probability
            FROM {SCHEMA}.historical_probability_points
            ORDER BY hour_ts DESC LIMIT 30
        """)
        print("=== Sample probability timestamps ===")
        for r in rows:
            print(f"  market={r['market_id'][:30]:30s}  hour_utc={r['hour_utc']}  date={r['d']}  prob={r['probability']:.3f}")

        # Check: what's the latest hour_ts per day? Is it midnight? 11pm? 4pm?
        rows2 = await c.fetch(f"""
            SELECT EXTRACT(HOUR FROM hour_ts AT TIME ZONE 'UTC') AS hr, COUNT(*) AS cnt
            FROM {SCHEMA}.historical_probability_points
            GROUP BY 1 ORDER BY 1
        """)
        print("\n=== Distribution of hour_ts (UTC) ===")
        for r in rows2:
            print(f"  Hour {int(r['hr']):2d}:00 UTC  ->  {r['cnt']} readings")
    finally:
        await c.close()

asyncio.run(check())
