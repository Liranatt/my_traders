"""Is the edge missing, or did the LLM pick the wrong world? Three controls, offline.

Study 0  Hedge-vs-sector split by archetype. Earnings worlds are ~1 name (tight); military
         worlds are ~3-5 (broad). If tight mappings show alpha-vs-sector and broad ones don't,
         the LLM breadth is the problem. If even tight ones are ~0, the edge isn't there.

Study A  Oracle sector-timing. Does the sector ETF beat its OWN unconditional drift after a
         crossing? Tests whether the oracle predicts anything at all, even at the sector level.

Study B  LLM mapping skill. Do the mapped assets move MORE / in a BETTER direction (vs sector)
         than RANDOM same-sector names on the same event window? This is the paper's own Stage-II
         RL reward (mapped assets should show elevated event-driven move). If mapped == random,
         the LLM is doing sector selection, not asset selection -> the world is the problem.

Usage:  .venv/Scripts/python.exe -m general_testing.oracle_control_study [run_id]
"""
from __future__ import annotations

import asyncio
import random
import sys
from collections import defaultdict

import numpy as np

from general_testing.idea_backtests import DEFAULT_RUN, fwd_return, line, load, stats

H = 10  # trading-day horizon for the controls


def _unconditional_h_return(series, sym, h):
    if sym not in series:
        return None
    _, closes = series[sym]
    if len(closes) <= h:
        return None
    rets = closes[h:] / closes[:-h] - 1.0
    return float(rets.mean())


async def main():
    run_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RUN
    obs, sector, prob_t0, series = await load(run_id)
    priced = set(series)
    sector_members = defaultdict(list)
    for sym, etf in sector.items():
        if sym in priced and etf in priced:
            sector_members[etf].append(sym)
    cohort_by_event = defaultdict(set)
    for o in obs:
        cohort_by_event[o["event_id"]].add(o["symbol"])
    world_size = [len(v) for v in cohort_by_event.values()]
    print(f"candidates={len(obs)} events={len(cohort_by_event)} "
          f"avg_world_size={np.mean(world_size):.2f} median={int(np.median(world_size))} max={max(world_size)}")

    # ---- Study 0: hedge vs sector, split by archetype (tight vs broad worlds) ----
    print("\n" + "=" * 92)
    print(f"STUDY 0  —  alpha-vs-sector by archetype  (does a TIGHTER LLM world help?)  H={H}d")
    print("=" * 92)
    by_arch_out, by_arch_hed, by_arch_world = defaultdict(list), defaultdict(list), defaultdict(list)
    for o in obs:
        a = fwd_return(series, o["symbol"], o["first_pass_at"], H)
        if a is None:
            continue
        etf = sector.get(o["symbol"])
        e = fwd_return(series, etf, o["first_pass_at"], H) if etf else None
        arch = o["event_archetype"]
        by_arch_out[arch].append(a)
        by_arch_world[arch].append(len(cohort_by_event[o["event_id"]]))
        if e is not None:
            by_arch_hed[arch].append(a - e)
    for arch in sorted(by_arch_out, key=lambda k: -len(by_arch_out[k])):
        w = np.mean(by_arch_world[arch])
        print(f"\n  {arch}   (avg world size {w:.1f})")
        line("outright long", stats(by_arch_out[arch]))
        line("hedged vs sector", stats(by_arch_hed[arch]))

    # ---- Study A: oracle sector-timing (abnormal vs the ETF's own drift) ----
    print("\n" + "=" * 92)
    print(f"STUDY A  —  does a crossing predict the SECTOR move? (abnormal vs unconditional)  H={H}d")
    print("=" * 92)
    uncond = {etf: _unconditional_h_return(series, etf, H) for etf in set(sector.values()) if etf in priced}
    abnormal, raw_sector = [], []
    for o in obs:
        etf = sector.get(o["symbol"])
        if not etf or etf not in priced or uncond.get(etf) is None:
            continue
        sret = fwd_return(series, etf, o["first_pass_at"], H)
        if sret is None:
            continue
        raw_sector.append(sret)
        abnormal.append(sret - uncond[etf])
    line("sector ETF raw fwd return", stats(raw_sector))
    line("sector ABNORMAL (vs drift)", stats(abnormal))
    print("   -> abnormal ~ 0 => the crossing does NOT time the sector; +ve => real timing value")

    # ---- Study B: LLM mapping skill vs random same-sector ----
    print("\n" + "=" * 92)
    print(f"STUDY B  —  do LLM picks beat RANDOM same-sector names? (idio move vs sector)  H={H}d")
    print("=" * 92)
    rng = random.Random(42)
    m_idio, m_abs, r_idio, r_abs = [], [], [], []
    for o in obs:
        etf = sector.get(o["symbol"])
        if not etf or etf not in priced:
            continue
        a = fwd_return(series, o["symbol"], o["first_pass_at"], H)
        e = fwd_return(series, etf, o["first_pass_at"], H)
        if a is None or e is None:
            continue
        m_idio.append(a - e)
        m_abs.append(abs(a - e))
        pool = [s for s in sector_members.get(etf, []) if s not in cohort_by_event[o["event_id"]]]
        if pool:
            ra = fwd_return(series, rng.choice(pool), o["first_pass_at"], H)
            if ra is not None:
                r_idio.append(ra - e)
                r_abs.append(abs(ra - e))
    line("MAPPED idio vs sector (signed)", stats(m_idio))
    line("RANDOM same-sector (signed)", stats(r_idio))
    line("MAPPED |idio| (reactivity)", stats(m_abs))
    line("RANDOM |idio| (reactivity)", stats(r_abs))
    if m_abs and r_abs:
        print(f"   -> reactivity ratio mapped/random = {np.mean(m_abs)/np.mean(r_abs):.2f}x "
              f"(>1 => LLM finds the names that actually move on the event)")


if __name__ == "__main__":
    asyncio.run(main())
