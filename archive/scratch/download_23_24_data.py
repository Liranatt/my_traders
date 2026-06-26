import asyncio
import json
import numpy as np
import pandas as pd
from datetime import datetime, timezone

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from database.backtesting.market_data import YFinanceClient
from database.backtesting.polymarket import PolymarketHistoryClient
from main_backtesting.config import BacktestConfig

async def download_missing_data():
    conn = await connect()
    try:
        with open(r'C:\Users\Liran\.gemini\antigravity-ide\brain\d591379e-aa8e-44ce-ac80-8615368439cf\scratch\worlds_23_24.json', 'r') as f:
            worlds = json.load(f)
            
        market_ids = []
        symbols = set()
        
        for w in worlds:
            # request_id is market_id:pass_number
            mid = w.get("request_id", "").split(":")[0]
            if mid:
                market_ids.append(mid)
            for a in w.get("assets", []):
                if a.get("symbol"):
                    symbols.add(a["symbol"])
                    
        print(f"Found {len(market_ids)} markets and {len(symbols)} symbols.")
        
        # 1. Download probabilities
        print("Downloading probabilities...")
        poly = PolymarketHistoryClient(chunk_days=30)
        # We need the event start/end dates to know what to download
        rows = await conn.fetch(f"SELECT market_id, created_at, end_at FROM {SCHEMA}.historical_run_markets WHERE market_id = ANY($1::text[])", market_ids)
        mkt_dates = {r['market_id']: (r['created_at'], r['end_at']) for r in rows}
        
        # actually PolymarketHistoryClient.download_market_probabilities expects (conn, market_id, t_start, t_end)
        for mid in market_ids:
            if mid in mkt_dates:
                start, end = mkt_dates[mid]
                if start and end:
                    print(f"Downloading {mid} from {start} to {end}")
                    await poly.download_market_probabilities(conn, mid, start, end)
                    
        # 2. Download prices
        print("Downloading prices...")
        yf_client = YFinanceClient(concurrency=10)
        # We just need to download from 2023-01-01 to 2024-12-31 for these symbols
        await yf_client.download_and_save_prices(
            conn, 
            symbols=list(symbols), 
            resolution='1d', 
            start_date=datetime(2023, 1, 1, tzinfo=timezone.utc), 
            end_date=datetime(2025, 1, 1, tzinfo=timezone.utc)
        )
        print("Data download complete.")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(download_missing_data())
