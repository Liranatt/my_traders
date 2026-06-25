"""Naive opportunity test on the NEW two-pass worlds (no model, no hedge).

For every world asset of a run (filtered by final relevance = question_relevance x connection_strength,
stored in historical_asset_world_assets.connection_strength), enter at the first daily close on/after
the market's 55% crossing and measure, over [crossing, resolution]:
  - ORACLE  = max(best long exit, best short exit)   -> the ceiling if you nail direction + exit
  - long_hold = (last close - entry)/entry            -> naive "long the beneficiary", hold to exit
  - long_best = best long exit                        -> naive long with a perfect exit
  - hit       = long_hold > 0                          -> how often naive-long is even directionally right

Usage:
  .venv/Scripts/python.exe -m general_testing.naive_oracle_test <run_id> [--min-relevance 0.5]
"""
from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from datetime import datetime, timedelta, timezone

from database.db_connection import connect
from database.backtesting.schema import SCHEMA

TRAIN_END = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
VAL_START = datetime(2026, 1, 10, tzinfo=timezone.utc)
VAL_END = datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc)
TEST_START = datetime(2026, 4, 10, tzinfo=timezone.utc)


def split_for(t) -> str:
    if t <= TRAIN_END:
        return "train"
    if VAL_START <= t <= VAL_END:
        return "val"
    if t >= TEST_START:
        return "test"
    return "embargo"


async def load_trades(c, run_id: str, min_rel: float):
    rows = await c.fetch(f"""
        SELECT rw.market_id, rw.pass_number, a.symbol, a.connection_strength AS relevance,
               p.above_at AS t_theta, m.end_at AS t_e, m.question,
               COALESCE(o.event_archetype, 'unknown') AS archetype
        FROM {SCHEMA}.historical_run_worlds rw
        JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id = rw.world_id
        JOIN {SCHEMA}.historical_run_market_passes p
          ON p.run_id = rw.run_id AND p.market_id = rw.market_id AND p.pass_number = rw.pass_number
        JOIN {SCHEMA}.historical_run_markets m ON m.run_id = rw.run_id AND m.market_id = rw.market_id
        LEFT JOIN {SCHEMA}.historical_ml_observations o
          ON o.run_id = rw.run_id AND o.market_id = rw.market_id AND o.symbol = a.symbol
        WHERE rw.run_id = $1 AND a.connection_strength >= $2
    """, run_id, min_rel)
    return rows


async def price_window(c, symbol, start, end):
    rows = await c.fetch(f"""
        SELECT ts, close FROM {SCHEMA}.historical_price_bars
        WHERE symbol=$1 AND resolution='1d' AND ts >= $2 AND ts <= $3 ORDER BY ts
    """, symbol, start - timedelta(days=1), end + timedelta(days=1))
    return [(r["ts"], float(r["close"])) for r in rows]


def summarize(label, trades):
    if not trades:
        print(f"{label:34} (no trades)")
        return
    oracle = [t["oracle"] for t in trades]
    longhold = [t["long_hold"] for t in trades]
    longbest = [t["long_best"] for t in trades]
    hit = [t["long_hold"] > 0 for t in trades]
    print(f"{label:34} n={len(trades):4}  oracle med={statistics.median(oracle)*100:5.1f}% mean={statistics.mean(oracle)*100:5.1f}%  "
          f"long_hold mean={statistics.mean(longhold)*100:+5.2f}%  long_best mean={statistics.mean(longbest)*100:+5.1f}%  "
          f"hit={sum(hit)/len(hit)*100:4.0f}%")


async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id")
    ap.add_argument("--min-relevance", type=float, default=0.5)
    args = ap.parse_args()

    c = await connect()
    try:
        rows = await load_trades(c, args.run_id, args.min_relevance)
        print(f"run={args.run_id}  world picks with final relevance >= {args.min_relevance}: {len(rows)}\n")
        trades = []
        for r in rows:
            if r["t_theta"] is None:
                continue
            bars = await price_window(c, r["symbol"], r["t_theta"], r["t_e"])
            win = [(ts, px) for ts, px in bars if ts >= r["t_theta"]]
            if len(win) < 2:
                continue
            entry = win[0][1]
            if entry <= 0:
                continue
            hi = max(px for _, px in win); lo = min(px for _, px in win); last = win[-1][1]
            up = (hi - entry) / entry; down = (entry - lo) / entry
            trades.append({
                "archetype": str(r["archetype"]),
                "split": split_for(r["t_theta"]),
                "relevance": float(r["relevance"]) if r["relevance"] is not None else None,
                "oracle": max(up, down),
                "long_hold": (last - entry) / entry,
                "long_best": up,
            })
    finally:
        await c.close()

    print(f"priced trades: {len(trades)}\n")
    summarize("ALL (relevance>=cutoff)", trades)
    print("\nby split (OOS = test):")
    for sp in ("train", "val", "test"):
        summarize(f"  {sp}", [t for t in trades if t["split"] == sp])
    print("\nby archetype:")
    for arch in sorted({t["archetype"] for t in trades}):
        summarize(f"  {arch[:30]}", [t for t in trades if t["archetype"] == arch])
    print("\nReminder: ORACLE assumes perfect direction+exit (a ceiling). long_hold is the naive,")
    print("real-world 'go long the beneficiary and hold to resolution' P&L; hit = % of those that were up.")


if __name__ == "__main__":
    asyncio.run(main())
