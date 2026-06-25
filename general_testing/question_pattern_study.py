"""Which QUESTIONS / kinds of questions drive the biggest positive vs negative idiosyncratic
(sector-hedged) moves? Pure description to surface a pattern. Outsider script, read-only.

Move metric per candidate = hedged forward return over H trading days from T_theta
(asset return minus its sector ETF return; SPY fallback). Then we slice it by:
  - the single most positive / most negative questions (concrete examples)
  - keywords in the question text (which words systematically associate with +/- moves)
  - semantic archetype x yes-outcome-polarity
  - whether the event finally resolved YES vs NO
  - near-term deadline phrasing vs far

Usage:  .venv/Scripts/python.exe -m general_testing.question_pattern_study [run_id] [--h 10]
"""
from __future__ import annotations

import asyncio
import re
import sys
from collections import defaultdict

import numpy as np

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from general_testing.idea_backtests import DEFAULT_RUN, fwd_return, load
from main_backtesting.semantic_groups import classify_assignment

H = 10
STOP = set(
    """a an the to of in on by at for and or is be will would could this that these those
    before after again another over under between through during within into out as it its
    no not more most than then so any all each new next st nd rd th day days week month year
    january february march april may june july august september october november december
    monday tuesday wednesday thursday friday saturday sunday q1 q2 q3 q4 vs""".split()
)
DEADLINE_SOON = ("today", "tomorrow", "this week", "by friday", "by sunday", "by monday",
                 "by tuesday", "by wednesday", "by thursday", "by saturday", "this month")


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= 3 and w not in STOP}


def desc(vals: list[float]) -> str:
    a = np.array(vals, dtype=float)
    return (f"n={a.size:4} mean={a.mean()*100:+6.2f}% median={np.median(a)*100:+6.2f}% "
            f"hit={(a>0).mean():.0%} |move|={np.abs(a).mean()*100:5.2f}%")


async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = [a for a in sys.argv[1:]]
    run_id = next((a for a in args if not a.startswith("--")), DEFAULT_RUN)
    obs, sector, prob_t0, series = await load(run_id)

    conn = await connect()
    try:
        qmap = {
            r["market_id"]: (r["question"], r["final_outcome"])
            for r in await conn.fetch(
                f"SELECT market_id, question, final_outcome FROM {SCHEMA}.historical_run_markets WHERE run_id=$1",
                run_id,
            )
        }
    finally:
        await conn.close()

    rows = []  # (question, symbol, archetype, polarity, outcome, hedged_move)
    for o in obs:
        q, outcome = qmap.get(o["market_id"], (None, None))
        if not q:
            continue
        a = fwd_return(series, o["symbol"], o["first_pass_at"], H)
        etf = sector.get(o["symbol"])
        e = fwd_return(series, etf, o["first_pass_at"], H) if etf else fwd_return(series, "SPY", o["first_pass_at"], H)
        if a is None or e is None:
            continue
        pol = classify_assignment(q, symbol=o["symbol"], asset_name="").yes_outcome_polarity
        rows.append((q, o["symbol"], o["event_archetype"], pol, outcome, a - e))
    print(f"run={run_id}  candidates with hedged {H}d move = {len(rows)}\n")

    # ---- 1. most positive / most negative individual questions ----
    rows.sort(key=lambda r: r[5])
    print("=" * 100)
    print(f"MOST NEGATIVE hedged {H}d moves (question | symbol | archetype | resolved)")
    print("=" * 100)
    for q, sym, arch, pol, outc, mv in rows[:15]:
        print(f"  {mv*100:+6.1f}%  {sym:5} {str(outc):4} {arch[:26]:26} {q[:60]}")
    print("\n" + "=" * 100)
    print(f"MOST POSITIVE hedged {H}d moves")
    print("=" * 100)
    for q, sym, arch, pol, outc, mv in rows[-15:][::-1]:
        print(f"  {mv*100:+6.1f}%  {sym:5} {str(outc):4} {arch[:26]:26} {q[:60]}")

    # ---- 2. keyword -> mean hedged move ----
    kw = defaultdict(list)
    for q, sym, arch, pol, outc, mv in rows:
        for w in tokens(q):
            kw[w].append(mv)
    ranked = sorted(
        ((w, np.mean(v), len(v)) for w, v in kw.items() if len(v) >= 12),
        key=lambda x: x[1],
    )
    print("\n" + "=" * 100)
    print("KEYWORDS most associated with NEGATIVE hedged move (min 12 occurrences)")
    print("=" * 100)
    for w, m, n in ranked[:15]:
        print(f"  {m*100:+6.2f}%  n={n:4}  {w}")
    print("\nKEYWORDS most associated with POSITIVE hedged move")
    print("=" * 100)
    for w, m, n in ranked[::-1][:15]:
        print(f"  {m*100:+6.2f}%  n={n:4}  {w}")

    # ---- 3. archetype x polarity ----
    print("\n" + "=" * 100)
    print("BY archetype x yes-outcome-polarity")
    print("=" * 100)
    grp = defaultdict(list)
    for q, sym, arch, pol, outc, mv in rows:
        grp[(arch, pol)].append(mv)
    for key in sorted(grp, key=lambda k: -np.mean(grp[k])):
        if len(grp[key]) >= 8:
            print(f"  {str(key[0])[:34]:34} pol={key[1]:9}  {desc(grp[key])}")

    # ---- 4. resolved YES vs NO, and near-term-deadline phrasing ----
    print("\n" + "=" * 100)
    print("BY final outcome  and  by deadline phrasing")
    print("=" * 100)
    by_outcome = defaultdict(list)
    for q, sym, arch, pol, outc, mv in rows:
        by_outcome[str(outc)].append(mv)
    for k in sorted(by_outcome, key=lambda k: -np.mean(by_outcome[k])):
        print(f"  resolved={k:6} {desc(by_outcome[k])}")
    near, far = [], []
    for q, sym, arch, pol, outc, mv in rows:
        (near if any(d in q.lower() for d in DEADLINE_SOON) else far).append(mv)
    print(f"  deadline=SOON  {desc(near)}")
    print(f"  deadline=FAR   {desc(far)}")


if __name__ == "__main__":
    asyncio.run(main())
