"""Trace the ACTUAL direction decision for specific symbols: what the model predicted, what
information it was based on, what the rule did, and whether it was right. Read-only.

Usage:  .venv/Scripts/python.exe -m general_testing.decision_trace [run_id] SNOW WIX ...
"""
from __future__ import annotations

import asyncio
import json
import sys

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from general_testing.idea_backtests import DEFAULT_RUN

FEATURE_ORDER = [
    "polymarket_probability_at_trigger", "polymarket_probability_slope_24h",
    "polymarket_probability_volatility", "polymarket_crossing_latency_days",
    "polymarket_time_to_resolution_days", "polymarket_pre_entry_volume_log",
    "asset_ytd_change", "asset_two_week_trend", "sector_one_month_trend", "spy_two_week_trend",
]


def _j(v):
    return v if isinstance(v, dict) else json.loads(v)


async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = sys.argv[1:]
    run_id = next((a for a in args if "-" in a and len(a) > 20), DEFAULT_RUN)
    symbols = [a.upper() for a in args if a.upper() != run_id.upper() and len(a) <= 6]
    if not symbols:
        symbols = ["SNOW", "WIX"]

    conn = await connect()
    try:
        for sym in symbols:
            rows = await conn.fetch(
                f"""SELECT o.event_id, o.first_pass_at, o.features, o.classification_target AS actual_dir,
                           o.regression_target AS actual_peak, m.question,
                           p.direction AS pred_dir, p.classification_probability AS p_up,
                           p.predicted_peak_percent AS pred_peak, p.directions_agree,
                           p.remaining_gap, p.realized_move_at_entry, p.actual_direction,
                           p.classification_correct,
                           t.direction AS trade_dir, t.net_profit, t.exit_reason
                    FROM {SCHEMA}.historical_ml_observations o
                    JOIN {SCHEMA}.historical_run_markets m ON m.run_id=o.run_id AND m.market_id=o.market_id
                    LEFT JOIN {SCHEMA}.historical_ml_predictions p
                           ON p.run_id=o.run_id AND p.event_id=o.event_id AND p.symbol=o.symbol
                    LEFT JOIN {SCHEMA}.historical_trades t
                           ON t.run_id=o.run_id AND t.event_id=o.event_id AND t.symbol=o.symbol
                              AND t.portfolio='machine_learning'
                    WHERE o.run_id=$1 AND o.symbol=$2
                    ORDER BY o.first_pass_at""",
                run_id, sym,
            )
            if not rows:
                print(f"\n### {sym}: no observations in this run")
                continue
            for r in rows:
                f = _j(r["features"])
                print("\n" + "=" * 96)
                print(f"### {sym}   {r['question']}")
                print(f"    crossing T_theta = {r['first_pass_at']:%Y-%m-%d %H:%M}")
                print("    --- information the decision saw (features at T_theta) ---")
                for name in FEATURE_ORDER:
                    if name in f and f[name] is not None:
                        print(f"        {name:38} {float(f[name]):+.4f}")
                print("    --- what the model PREDICTED ---")
                if r["pred_dir"] is None:
                    print("        (no ML prediction — candidate fell to the no-model fallback)")
                else:
                    print(f"        classifier P(up)      = {r['p_up']:.3f}  ->  direction = {r['pred_dir'].upper()}")
                    print(f"        ridge predicted peak  = {r['pred_peak']:+.2%}")
                    print(f"        directions_agree      = {r['directions_agree']}   remaining_gap = {r['remaining_gap']:+.2%}")
                print("    --- what ACTUALLY happened ---")
                print(f"        actual direction      = {r['actual_direction']}   (label dir={r['actual_dir']}, peak={float(r['actual_peak']):+.2%})")
                print(f"        classification_correct= {r['classification_correct']}")
                if r["trade_dir"] is None:
                    print("        TRADE: none opened (gated out — see rule below)")
                else:
                    print(f"        TRADE: {r['trade_dir'].upper()} net=${float(r['net_profit'] or 0):+.2f} exit={r['exit_reason']}")

        # population context: classifier confusion on this archetype
        print("\n" + "=" * 96)
        print("CONTEXT — earnings_beat classifier direction vs reality (this run)")
        c = await conn.fetchrow(
            f"""SELECT COUNT(*) n,
                       AVG((classification_correct)::int)::float acc,
                       AVG((direction='long')::int)::float pred_long_share,
                       AVG((actual_direction='long')::int)::float actual_long_share
                FROM {SCHEMA}.historical_ml_predictions p
                JOIN {SCHEMA}.historical_ml_observations o
                  ON o.run_id=p.run_id AND o.event_id=p.event_id AND o.symbol=p.symbol
                WHERE p.run_id=$1 AND o.event_archetype='earnings_beat+direct_company'
                  AND p.classification_correct IS NOT NULL""",
            run_id,
        )
        if c and c["n"]:
            print(f"   n={c['n']}  accuracy={c['acc']:.3f}  predicted-long={c['pred_long_share']:.0%}  "
                  f"actually-long={c['actual_long_share']:.0%}")
    finally:
        await conn.close()


asyncio.run(main())
