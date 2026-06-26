import asyncio
import json
import logging
import math
import os
from pathlib import Path
from datetime import datetime, timezone
import pandas as pd
import numpy as np

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from database.backtesting.polymarket import PolymarketHistoryClient

try:
    import yfinance as yf
except ImportError:
    yf = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "data" / "candidates.parquet"

def sign(x: float) -> float:
    return 1.0 if x > 0 else (-1.0 if x < 0 else 0.0)

async def main():
    conn = await connect()
    try:
        # 1. Fetch 2025+ markets from database (historical_run_markets + historical_asset_worlds)
        # We only care about runs that aren't purged, and relevance >= 0.5
        db_rows = await conn.fetch(f"""
            SELECT DISTINCT ON (w.market_id)
                w.market_id, w.event_id, w.llm_output, m.title as question, m.created_at, m.end_at
            FROM {SCHEMA}.historical_asset_worlds w
            JOIN {SCHEMA}.source_events m ON m.event_id = w.event_id
            ORDER BY w.market_id, w.as_of DESC
        """)
        
        candidates = []
        
        # Helper to parse llm_output
        def parse_assets(llm_output):
            if isinstance(llm_output, str):
                try:
                    llm_output = json.loads(llm_output)
                except:
                    return []
            if not llm_output:
                return []
            
            # Extract the list of worlds
            if isinstance(llm_output, dict):
                if "worlds" in llm_output:
                    worlds_list = llm_output["worlds"]
                else:
                    worlds_list = [llm_output]
            elif isinstance(llm_output, list):
                worlds_list = llm_output
            else:
                return []
                
            out = []
            for w in worlds_list:
                if not isinstance(w, dict): continue
                rel = w.get("question_relevance", 1.0)
                arch = w.get("universe_name", "unknown")
                for a in w.get("assets", []):
                    cs = a.get("connection_strength", 1.0)
                    out.append({
                        "symbol": a.get("symbol"),
                        "relevance": rel * cs,
                        "archetype": arch
                    })
            return out

        for r in db_rows:
            assets = parse_assets(r["llm_output"])
            for a in assets:
                if a["relevance"] >= 0.5 and a["symbol"]:
                    candidates.append({
                        "market_id": r["market_id"],
                        "event_id": r["event_id"],
                        "question": r["question"],
                        "t0": pd.Timestamp(r["created_at"]).tz_convert("UTC"),
                        "t_e": pd.Timestamp(r["end_at"]).tz_convert("UTC"),
                        "symbol": a["symbol"],
                        "feat_archetype": a["archetype"],
                        "relevance": a["relevance"],
                        "source": "db"
                    })
                    
        # 2. Add 2023-2024 markets from scratch/worlds_23_24.json
        worlds_json = REPO_ROOT / "scratch" / "worlds_23_24.json"
        if worlds_json.exists():
            with open(worlds_json, "r") as f:
                worlds_2324 = json.load(f)
            
            # Fetch created/end dates from DB for these markets since they exist in historical_run_markets
            mkt_ids = [w.get("request_id", "").split(":")[0] for w in worlds_2324 if w.get("request_id")]
            if mkt_ids:
                dates_rows = await conn.fetch(f"SELECT market_id, created_at, end_at, event_id, question FROM {SCHEMA}.historical_run_markets WHERE market_id = ANY($1::text[])", mkt_ids)
                dates_map = {dr["market_id"]: dr for dr in dates_rows}
                
                for w in worlds_2324:
                    mid = w.get("request_id", "").split(":")[0]
                    if mid in dates_map:
                        dr = dates_map[mid]
                        assets = parse_assets(w)
                        for a in assets:
                            if a["relevance"] >= 0.5 and a["symbol"]:
                                candidates.append({
                                    "market_id": mid,
                                    "event_id": dr["event_id"],
                                    "question": dr["question"],
                                    "t0": pd.Timestamp(dr["created_at"]).tz_convert("UTC"),
                                    "t_e": pd.Timestamp(dr["end_at"]).tz_convert("UTC"),
                                    "symbol": a["symbol"],
                                    "feat_archetype": a["archetype"],
                                    "relevance": a["relevance"],
                                    "source": "json"
                                })

        # deduplicate
        df_cands = pd.DataFrame(candidates)
        df_cands = df_cands.drop_duplicates(subset=["market_id", "symbol"])
        logger.info(f"Loaded {len(df_cands)} valid market-asset pairs with relevance >= 0.5")

        # 3. Load probabilities
        market_ids = df_cands["market_id"].unique().tolist()
        logger.info(f"Loading probability points for {len(market_ids)} markets")
        
        prob_rows = await conn.fetch(f"""
            SELECT market_id, hour_ts, probability 
            FROM {SCHEMA}.historical_probability_points 
            WHERE market_id = ANY($1::text[])
            ORDER BY market_id, hour_ts
        """, market_ids)
        
        probs = {}
        for pr in prob_rows:
            mid = pr["market_id"]
            if mid not in probs:
                probs[mid] = []
            probs[mid].append((pd.Timestamp(pr["hour_ts"]).tz_convert("UTC"), float(pr["probability"])))
            
        # Check missing probs and fetch if needed
        missing_mids = [m for m in market_ids if m not in probs or len(probs[m]) == 0]
        if missing_mids:
            logger.info(f"Fetching missing probabilities for {len(missing_mids)} markets...")
            poly = PolymarketHistoryClient()
            for mid in missing_mids:
                row = df_cands[df_cands["market_id"] == mid].iloc[0]
                # Try to fetch using client. We need a SourceMarket mock.
                from main_backtesting.models import SourceMarket
                sm = SourceMarket(
                    market_id=mid, event_id=row["event_id"], event_title="", question="",
                    created_at=row["t0"].to_pydatetime(), end_at=row["t_e"].to_pydatetime(),
                    tags=[], raw_market={}, yes_token_id="", condition_id=None, final_outcome=None
                )
                pts = await poly.hourly_probabilities(sm, start=sm.created_at, end=sm.end_at)
                probs[mid] = [(pd.Timestamp(pt.timestamp).tz_convert("UTC"), float(pt.probability)) for pt in pts]
            await poly.close()

        # 4. Find t_theta
        t_thetas = {}
        for mid, pts in probs.items():
            t_theta = next((t for t, p in pts if p >= 0.55), None)
            if t_theta:
                t_thetas[mid] = t_theta
                
        df_cands["t_theta"] = df_cands["market_id"].map(t_thetas)
        df_cands = df_cands.dropna(subset=["t_theta"]).copy()
        logger.info(f"Filtered down to {len(df_cands)} candidates that actually reached 55% probability.")
        
        # 5. Load prices
        symbols = df_cands["symbol"].unique().tolist()
        logger.info(f"Loading price histories for {len(symbols)} symbols")
        price_rows = await conn.fetch(f"""
            SELECT symbol, ts, close 
            FROM {SCHEMA}.historical_price_bars 
            WHERE resolution = '1d' AND symbol = ANY($1::text[])
            ORDER BY symbol, ts
        """, symbols)
        
        prices = {}
        for pr in price_rows:
            s = pr["symbol"]
            if s not in prices:
                prices[s] = []
            prices[s].append((pd.Timestamp(pr["ts"]).tz_convert("UTC").normalize(), float(pr["close"])))
            
        missing_syms = [s for s in symbols if s not in prices or len(prices[s]) == 0]
        if missing_syms and yf is not None:
            logger.info(f"Fetching missing prices for {len(missing_syms)} symbols using yfinance...")
            yf_df = yf.download(missing_syms, start="2023-01-01", end="2026-12-31", progress=False)
            if not yf_df.empty:
                if len(missing_syms) == 1:
                    s = missing_syms[0]
                    closes = yf_df["Close"].dropna()
                    prices[s] = [(pd.Timestamp(t).tz_convert("UTC").normalize() if getattr(t, 'tzinfo', None) else pd.Timestamp(t).tz_localize("UTC").normalize(), float(v)) for t, v in closes.items()]
                else:
                    for s in missing_syms:
                        if "Close" in yf_df and s in yf_df["Close"]:
                            closes = yf_df["Close"][s].dropna()
                            prices[s] = [(pd.Timestamp(t).tz_convert("UTC").normalize() if getattr(t, 'tzinfo', None) else pd.Timestamp(t).tz_localize("UTC").normalize(), float(v)) for t, v in closes.items()]

        # 6. Compute features
        final_records = []
        for _, row in df_cands.iterrows():
            mid = row["market_id"]
            sym = row["symbol"]
            t_theta = row["t_theta"]
            t0 = row["t0"]
            
            pts = probs.get(mid, [])
            p_t0 = pts[0][1] if pts else 0.5
            p_theta = next((p for t, p in pts if t >= t_theta), 0.55)
            
            bars = prices.get(sym, [])
            if not bars:
                continue
                
            # prices at t0 and t_theta
            bar_t0 = next((v for t, v in reversed(bars) if t <= t0), None)
            if not bar_t0:
                bar_t0 = bars[0][1] if bars else None
                
            bar_theta = next((v for t, v in reversed(bars) if t <= t_theta), None)
            
            if not bar_t0 or not bar_theta:
                continue
                
            feat_runup = (bar_theta / bar_t0) - 1.0
            
            # fill dummy values for RF so it doesn't crash if turned back on
            rec = {
                "run_id": "direct",
                "event_id": row["event_id"],
                "market_id": mid,
                "symbol": sym,
                "pass_number": 0,
                "t0": t0,
                "t_theta": t_theta,
                "t_e": row["t_e"],
                "entry_price": bar_theta,
                "sector_etf": "SPY",
                "y_hedged": 0.0,
                "realized_dir": 1.0,
                "realized_abs_move": 0.0,
                "alpha_score": 0.0,
                "alpha_dir": 1,
                
                # Critical features for Liran Strategy
                "feat_archetype": row["feat_archetype"],
                "feat_prob_surge_since_t0": (p_theta - p_t0),
                "feat_runup_since_t0": feat_runup,
                "question": row.get("question", ""),
                
                # Dummy fill for legacy RF features
                "feat_prob_at_trigger": p_theta,
                "feat_prob_slope_24h": 0.0,
                "feat_prob_volatility": 0.0,
                "feat_time_to_resolution_days": (row["t_e"] - t_theta).total_seconds() / 86400,
                "feat_crossing_latency_days": (t_theta - t0).total_seconds() / 86400,
                "feat_pre_entry_volume_log": 0.0,
                "feat_connection_strength": row["relevance"],
                "feat_world_size": 1,
                "feat_asset_role": "ambiguous",
                "feat_asset_2w_trend": 0.0,
                "feat_sector_1m_trend": 0.0,
                "feat_spy_2w_trend": 0.0,
                "feat_ytd_change": 0.0,
                "feat_debt_to_equity": 0.0,
                "feat_cash_to_marketcap": 0.0,
                "feat_beta": 1.0,
                "feat_profit_margin": 0.0,
                "feat_log_market_cap": 20.0,
                "feat_sector": "Unknown",
                "feat_ml_class_prob": 0.5,
                "feat_ml_pred_peak": 0.0,
                "feat_ml_dir": "1",
                "feat_momentum_roc": 0.0,
                "pre_drop_earnings_defense": False,
                "asset_return": 0.0,
                "sector_return": 0.0
            }
            
            # Assign split based on chronological time like the original
            train_end = pd.Timestamp("2025-12-31 23:59:59").tz_localize("UTC")
            val_start = pd.Timestamp("2026-01-10").tz_localize("UTC")
            val_end = pd.Timestamp("2026-03-31 23:59:59").tz_localize("UTC")
            test_start = pd.Timestamp("2026-04-10").tz_localize("UTC")
            
            if t_theta <= train_end:
                rec["split"] = "train"
            elif val_start <= t_theta <= val_end:
                rec["split"] = "val"
            elif t_theta >= test_start:
                rec["split"] = "test"
            else:
                rec["split"] = "embargo"
                
            final_records.append(rec)
            
        final_df = pd.DataFrame.from_records(final_records)
        
        # Save to parquet
        logger.info(f"Final dataset built with {len(final_df)} trades!")
        final_df.to_parquet(DEFAULT_OUTPUT, engine="pyarrow", compression="snappy")
        logger.info(f"Saved to {DEFAULT_OUTPUT}")

    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
