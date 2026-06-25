"""Throwaway DB exploration #2: pick the canonical 1733 run + measure feasibility. Safe to delete."""
from __future__ import annotations

import asyncio
import json

from database.db_connection import connect
from database.backtesting.schema import SCHEMA

TRAIN_END = "2025-12-31"
VAL_START = "2026-01-10"
VAL_END = "2026-03-31"
TEST_START = "2026-04-10"


async def main():
    conn = await connect()
    try:
        print("=== runs with exactly 1733 valid: metadata + embargoed splits ===")
        runs = await conn.fetch(
            f"""WITH c AS (
                  SELECT run_id, COUNT(*) FILTER (WHERE valid_for_training) v
                  FROM {SCHEMA}.historical_ml_observations GROUP BY run_id)
                SELECT c.run_id, c.v, r.started_at, r.finished_at, r.status, r.current_stage
                FROM c JOIN {SCHEMA}.historical_backtest_runs r USING(run_id)
                WHERE c.v = 1733 ORDER BY r.started_at DESC""",
        )
        for r in runs:
            print(f"  {r['run_id']}  started={r['started_at']:%Y-%m-%d %H:%M}  "
                  f"status={r['status']:9} stage={r['current_stage']}")
        if not runs:
            print("  none found"); return

        for r in runs[:3]:
            rid = r["run_id"]
            s = await conn.fetchrow(
                f"""SELECT
                      MIN(first_pass_at) lo, MAX(first_pass_at) hi,
                      COUNT(*) FILTER (WHERE first_pass_at <= '{TRAIN_END}') train,
                      COUNT(*) FILTER (WHERE first_pass_at >= '{VAL_START}' AND first_pass_at <= '{VAL_END}') val,
                      COUNT(*) FILTER (WHERE first_pass_at >= '{TEST_START}') test,
                      COUNT(*) total
                    FROM {SCHEMA}.historical_ml_observations
                    WHERE run_id=$1 AND valid_for_training""", rid)
            print(f"\n  run {rid}")
            print(f"    range {s['lo']:%Y-%m-%d} -> {s['hi']:%Y-%m-%d}   "
                  f"train={s['train']} val={s['val']} test={s['test']}  "
                  f"embargoed_out={s['total']-s['train']-s['val']-s['test']}")

        # Pick the most recent finished run as canonical for feasibility checks.
        canon = next((r["run_id"] for r in runs if r["status"] == "completed"), runs[0]["run_id"])
        print(f"\n=== feasibility on canonical run {canon} ===")

        # Candidate-level: entry bar (>= t_theta) and exit bar (>= min(t_e-1td, entry+10td)) existence.
        feas = await conn.fetchrow(
            f"""WITH obs AS (
                  SELECT event_id, market_id, symbol, first_pass_at t_theta, label_available_at t_e
                  FROM {SCHEMA}.historical_ml_observations
                  WHERE run_id=$1 AND valid_for_training),
                bars AS (
                  SELECT symbol, ts FROM {SCHEMA}.historical_price_bars WHERE resolution='1d'),
                entry AS (
                  SELECT o.*, MIN(b.ts) entry_ts
                  FROM obs o LEFT JOIN bars b ON b.symbol=o.symbol AND b.ts >= o.t_theta
                  GROUP BY o.event_id,o.market_id,o.symbol,o.t_theta,o.t_e)
                SELECT COUNT(*) total,
                       COUNT(entry_ts) has_entry,
                       COUNT(*) FILTER (WHERE entry_ts IS NOT NULL AND entry_ts < t_e) entry_before_te
                FROM entry""", canon)
        print(f"  candidates={feas['total']}  has_entry_bar={feas['has_entry']}  "
              f"entry_before_t_e={feas['entry_before_te']}")

        # realized move key coverage
        mv = await conn.fetchrow(
            f"""SELECT
                  COUNT(*) n,
                  COUNT(*) FILTER (WHERE (research_data->>'maximum_change') IS NOT NULL) has_max,
                  COUNT(*) FILTER (WHERE (research_data->>'minimum_change') IS NOT NULL) has_min,
                  COUNT(*) FILTER (WHERE regression_target IS NOT NULL) has_reg,
                  COUNT(*) FILTER (WHERE classification_target IS NOT NULL) has_cls
                FROM {SCHEMA}.historical_ml_observations
                WHERE run_id=$1 AND valid_for_training""", canon)
        print(f"  realized: n={mv['n']} max_change={mv['has_max']} min_change={mv['has_min']} "
              f"reg_target={mv['has_reg']} cls_target={mv['has_cls']}")

        # t_e horizon distribution: how far is label_available_at beyond first_pass_at?
        hz = await conn.fetch(
            f"""SELECT width_bucket(EXTRACT(EPOCH FROM (label_available_at-first_pass_at))/86400.0,
                                    0, 60, 6) b, COUNT(*) n
                FROM {SCHEMA}.historical_ml_observations
                WHERE run_id=$1 AND valid_for_training GROUP BY b ORDER BY b""", canon)
        print("  t_e - t_theta (calendar days) histogram buckets of 10d:")
        for h in hz:
            print(f"    bucket {h['b']}: {h['n']}")

        # one market's probability path around t_theta (daily resample preview)
        smp = await conn.fetchrow(
            f"""SELECT market_id, symbol, first_pass_at FROM {SCHEMA}.historical_ml_observations
                WHERE run_id=$1 AND valid_for_training ORDER BY first_pass_at LIMIT 1""", canon)
        pts = await conn.fetch(
            f"""SELECT hour_ts, probability FROM {SCHEMA}.historical_probability_points
                WHERE market_id=$1 ORDER BY hour_ts LIMIT 6""", smp["market_id"])
        print(f"\n  sample prob path market={smp['market_id']} sym={smp['symbol']} t_theta={smp['first_pass_at']:%Y-%m-%d %H:%M}")
        for p in pts:
            print(f"    {p['hour_ts']:%Y-%m-%d %H:%M}  p={p['probability']:.3f}")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
