"""Objective audit of the LLM's asset worlds: read the actual questions + the names Gemini
chose + its reasons, and measure -- independent of our semantic rules -- whether the chosen
names actually REACTED to the event (the only objective test of a "right world").

Reactivity per name = max(|max_change|, |min_change|) realized in the post-crossing window
(stored in the observation's research_data; no price work needed). A world whose names didn't
move is, objectively, a wrong/irrelevant world regardless of how plausible the reasons sound.

Usage:  .venv/Scripts/python.exe -m general_testing.world_quality_audit [run_id]
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict

import numpy as np

from database.db_connection import connect
from database.backtesting.schema import SCHEMA

RID = "29e46597-b51c-4eac-8edb-5ab78ff76e82"
DEAD = 0.03  # |move| below this in the whole window = the name basically didn't react


def reactivity(rd):
    if not rd:
        return None
    d = rd if isinstance(rd, dict) else json.loads(rd)
    mx, mn = d.get("maximum_change"), d.get("minimum_change")
    if mx is None and mn is None:
        return None
    return max(abs(mx or 0.0), abs(mn or 0.0))


async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run_id = sys.argv[1] if len(sys.argv) > 1 else RID
    c = await connect()
    try:
        rows = await c.fetch(f"""
            SELECT m.market_id, m.question, a.symbol, a.asset_name, a.reason,
                   o.research_data, o.event_archetype
            FROM {SCHEMA}.historical_run_worlds rw
            JOIN {SCHEMA}.historical_asset_worlds w ON w.world_id=rw.world_id
            JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id=w.world_id
            JOIN {SCHEMA}.historical_run_markets m ON m.run_id=rw.run_id AND m.market_id=rw.market_id
            LEFT JOIN {SCHEMA}.historical_ml_observations o
                   ON o.run_id=rw.run_id AND o.market_id=rw.market_id AND o.symbol=a.symbol
            WHERE rw.run_id=$1""", run_id)
    finally:
        await c.close()

    worlds = defaultdict(lambda: {"q": None, "arch": None, "assets": []})
    for r in rows:
        w = worlds[r["market_id"]]
        w["q"] = r["question"]
        w["arch"] = r["event_archetype"] or w["arch"]
        w["assets"].append((r["symbol"], reactivity(r["research_data"]), (r["reason"] or "")[:55]))

    sizes, reacts, untradeable = [], [], 0
    dead_world = live_world = 0
    by_arch = defaultdict(list)
    for w in worlds.values():
        sizes.append(len(w["assets"]))
        react_vals = [x for _, x, _ in w["assets"] if x is not None]
        untradeable += sum(1 for _, x, _ in w["assets"] if x is None)
        reacts.extend(react_vals)
        if react_vals:
            frac_dead = sum(1 for v in react_vals if v < DEAD) / len(react_vals)
            (dead_world := dead_world + 1) if frac_dead >= 0.6 else (live_world := live_world + 1)
            if w["arch"]:
                by_arch[w["arch"]].append(np.mean(react_vals))

    n = len(worlds)
    print(f"worlds={n}  total name-picks={sum(sizes)}  avg_world_size={np.mean(sizes):.1f} "
          f"median={int(np.median(sizes))} max={max(sizes)}")
    print(f"name-picks with NO tradable observation (untradeable/unpriced) = {untradeable} "
          f"({untradeable/sum(sizes):.0%})")
    if reacts:
        print(f"per-name reactivity |move|: median={np.median(reacts):.1%} "
              f"share moving <3% (dead) = {np.mean([v<DEAD for v in reacts]):.0%}")
    print(f"worlds that are mostly-dead (>=60% names moved <3%) = {dead_world} of "
          f"{dead_world+live_world} ({dead_world/(dead_world+live_world):.0%})")

    print("\n-- mean name reactivity by archetype (objective 'did the world react') --")
    for a in sorted(by_arch, key=lambda k: -np.mean(by_arch[k])):
        print(f"   {a[:40]:40} worlds={len(by_arch[a]):4} mean_name_|move|={np.mean(by_arch[a]):.1%}")

    def dump(title, items):
        print("\n" + "=" * 96 + f"\n{title}\n" + "=" * 96)
        for w in items:
            rv = [x for _, x, _ in w["assets"] if x is not None]
            tag = f"avg|move|={np.mean(rv):.1%}" if rv else "no-data"
            print(f"\n  [{w['arch']}]  {tag}\n  Q: {w['q'][:88]}")
            for sym, react, reason in sorted(w["assets"], key=lambda t: -(t[1] or -1))[:8]:
                rs = f"{react:.0%}" if react is not None else "  -"
                print(f"     {sym:6} |move|={rs:>4}  {reason}")

    scored = [w for w in worlds.values() if [x for _, x, _ in w["assets"] if x is not None]]
    scored.sort(key=lambda w: np.mean([x for _, x, _ in w["assets"] if x is not None]))
    dump("MOST 'DEAD' WORLDS (names barely moved -> objectively irrelevant picks)", scored[:6])
    dump("MOST 'LIVE' WORLDS (names actually reacted)", scored[-6:])


if __name__ == "__main__":
    asyncio.run(main())
