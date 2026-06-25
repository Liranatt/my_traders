"""How do the LLM's GOOD picks perform across our experiments?

"Good pick" = a name that actually reacted to its event (realized |move| in the post-crossing
window). Restricting to reactive names is a fair test of direction skill: conditioning on the
MAGNITUDE of a move does not bias WHICH WAY it went, so direction accuracy stays honest. The
question this answers: is the dead edge a WORLD-QUALITY problem (good picks have signal, junk
dilutes it) or a DIRECTION problem (even the good picks can't be called)?

Uses the shorts_hedged run (both directions + per-trade sector-hedged idiosyncratic P&L).

Usage:  .venv/Scripts/python.exe -m general_testing.right_picks_performance
"""
from __future__ import annotations

import asyncio
import json

import numpy as np

from database.db_connection import connect
from database.backtesting.schema import SCHEMA

SH = "339084de-3ab2-4548-b06b-350feff0a376"  # shorts_hedged (both dirs, hedged)


def react(rd):
    d = rd if isinstance(rd, dict) else json.loads(rd)
    mx, mn = d.get("maximum_change"), d.get("minimum_change")
    if mx is None and mn is None:
        return None
    return max(abs(mx or 0.0), abs(mn or 0.0))


def row(label, recs):
    if not recs:
        print(f"   {label:34} n=0"); return
    net = sum(r["net"] for r in recs)
    acc = [r["correct"] for r in recs if r["correct"] is not None]
    win = np.mean([r["net"] > 0 for r in recs])
    a = f"{np.mean(acc):.3f}" if acc else "  -"
    print(f"   {label:34} n={len(recs):4} dir_acc={a} hedged_net=${net:>7,.0f} "
          f"idio_win={win:.3f} mean=${net/len(recs):+6.1f}")


async def main():
    c = await connect()
    try:
        rows = await c.fetch(f"""
            SELECT t.direction, t.net_profit::float net, o.event_archetype arch,
                   o.research_data rd, (o.features->>'asset_two_week_trend')::float mom,
                   p.classification_correct correct
            FROM {SCHEMA}.historical_trades t
            JOIN {SCHEMA}.historical_ml_observations o
              ON o.run_id=t.run_id AND o.event_id=t.event_id AND o.symbol=t.symbol
            LEFT JOIN {SCHEMA}.historical_ml_predictions p
              ON p.run_id=t.run_id AND p.market_id=t.market_id
                 AND p.pass_number=t.pass_number AND p.symbol=t.symbol
            WHERE t.run_id=$1 AND t.portfolio='machine_learning'""", SH)
    finally:
        await c.close()

    recs = []
    for r in rows:
        rv = react(r["rd"])
        recs.append({"dir": r["direction"], "net": r["net"], "arch": r["arch"],
                     "react": rv, "mom": r["mom"], "correct": r["correct"]})
    scored = [r for r in recs if r["react"] is not None]
    cut = float(np.median([r["react"] for r in scored]))
    print(f"trades={len(recs)}  median realized |move| = {cut:.1%}\n")

    print("=== 1) ALL picks vs the LLM's GOOD picks (reactive names), by reactivity bucket ===")
    buckets = [("dead   |move|<5%", lambda r: r["react"] < 0.05),
               ("mid    5-15%", lambda r: 0.05 <= r["react"] < 0.15),
               ("LIVE   |move|>=15%", lambda r: r["react"] >= 0.15)]
    for label, f in buckets:
        row(label, [r for r in scored if f(r)])

    print("\n=== 2) direction accuracy: does the classifier call the GOOD picks any better? ===")
    row("all scored picks", scored)
    row("reactive picks (|move|>=15%)", [r for r in scored if r["react"] >= 0.15])
    row("most reactive (|move|>=25%)", [r for r in scored if r["react"] >= 0.25])

    print("\n=== 3) the edge from forensics, restricted to GOOD picks: momentum-confirmed LONGS ===")
    longs = [r for r in scored if r["dir"] == "long"]
    row("all longs", longs)
    row("longs, momentum>0", [r for r in longs if (r["mom"] or 0) > 0])
    row("longs, momentum>0 & reactive", [r for r in longs if (r["mom"] or 0) > 0 and r["react"] >= 0.10])

    print("\n=== 4) by archetype, GOOD picks only (reactive, |move|>=10%) ===")
    for arch in sorted({r["arch"] for r in scored}):
        for d in ("long", "short"):
            sub = [r for r in scored if r["arch"] == arch and r["dir"] == d and r["react"] >= 0.10]
            if len(sub) >= 6:
                row(f"{arch[:24]} {d}", sub)
    finally_note = (
        "\nReading it: if dir_acc stays ~0.4-0.5 on the LIVE/reactive picks, the LLM's GOOD\n"
        "picks still can't be directionally called -> direction is the bottleneck, not worlds.\n"
        "If a clean subset (e.g. momentum>0 & reactive) shows +hedged_net with idio_win>0.5,\n"
        "that's a real, narrow edge the junk picks were masking."
    )
    print(finally_note)


asyncio.run(main())
