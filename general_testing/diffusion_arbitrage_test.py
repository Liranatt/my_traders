"""Is there a real Polymarket->equity diffusion-lag arbitrage, on the MECHANICALLY-CONNECTED
names only (the earnings company itself; the oil names on supply-threat conflicts)? No defense
primes (spurious), no peer-pumping -- only the single name the question is actually about.

For each connected candidate we split the asset's move into PRE-crossing (T0 -> T_theta, what
the market already priced) and POST-crossing (T_theta -> resolution, the lag we could capture),
and ask: does the asset reprice in the OUTCOME direction AFTER Polymarket crosses, and does the
oracle predict it? That is the arbitrage: Polymarket catching it before the market.

Also re-checks how these names did in the original ML and momentum branches (unhedged).

Usage:  .venv/Scripts/python.exe -m general_testing.diffusion_arbitrage_test
"""
from __future__ import annotations

import asyncio
import json

import numpy as np

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from general_testing.idea_backtests import close_before, load

RID = "29e46597-b51c-4eac-8edb-5ab78ff76e82"  # baseline: ML + momentum, long-only, UNHEDGED
CONNECTED = (
    "earnings_beat+direct_company",
    "military_escalation+energy_beneficiary",
    "oil_supply_disruption+energy_beneficiary",
)


def _j(v):
    return v if isinstance(v, dict) else json.loads(v)


def stat(xs):
    a = np.array([x for x in xs if x is not None], float)
    return a if a.size else np.array([])


async def main():
    obs, sector, prob_t0, series = await load(RID)
    c = await connect()
    try:
        outcome = {r["market_id"]: r["final_outcome"] for r in await c.fetch(
            f"SELECT market_id, final_outcome FROM {SCHEMA}.historical_run_markets WHERE run_id=$1", RID)}
        # original-experiment P&L (unhedged) for the connected names, by branch
        print("=== how the CONNECTED names did in the original ML & momentum branches (unhedged) ===")
        for r in await c.fetch(f"""
            SELECT o.event_archetype arch, t.strategy_branch br, COUNT(*) n,
                   ROUND(SUM(t.net_profit)::numeric,0) net,
                   ROUND((COUNT(*) FILTER (WHERE t.net_profit>0)::numeric/COUNT(*)),3) win
            FROM {SCHEMA}.historical_trades t
            JOIN {SCHEMA}.historical_ml_observations o
              ON o.run_id=t.run_id AND o.event_id=t.event_id AND o.symbol=t.symbol
            WHERE t.run_id=$1 AND o.event_archetype = ANY($2::text[])
            GROUP BY 1,2 ORDER BY 1,2""", RID, list(CONNECTED)):
            print(f"   {r['arch'][:38]:38} {r['br']:14} n={r['n']:3} net=${r['net']:>7} win={r['win']}")
    finally:
        await c.close()

    # diffusion-lag arbitrage test on connected candidates
    rows = {a: [] for a in ("earnings", "oil")}
    for o in obs:
        if o["event_archetype"] not in CONNECTED:
            continue
        bucket = "earnings" if o["event_archetype"].startswith("earnings") else "oil"
        sym, t0, tth, te = o["symbol"], o["t0"], o["first_pass_at"], o["label_available_at"]
        p_t0 = close_before(series, sym, t0)
        p_th = close_before(series, sym, tth)
        p_te = close_before(series, sym, te)
        if not p_t0 or not p_th or not p_te or p_t0 <= 0 or p_th <= 0:
            continue
        pre = p_th / p_t0 - 1.0                     # priced-in before the crossing
        post = p_te / p_th - 1.0                    # the lag we could capture
        rd = _j(o["research_data"])
        surge = (rd.get("current_probability") or 0) - (prob_t0.get(o["market_id"]) or 0)
        outc = outcome.get(o["market_id"])
        osign = 1.0 if outc == "Yes" else (-1.0 if outc == "No" else None)
        rows[bucket].append({"pre": pre, "post": post, "surge": surge, "osign": osign})

    print("\n=== diffusion-lag arbitrage test (connected names only) ===")
    for bucket, rs in rows.items():
        if not rs:
            continue
        pre = stat([r["pre"] for r in rs])
        post = stat([r["post"] for r in rs])
        # does the asset move in the OUTCOME direction AFTER the crossing?
        aligned = [np.sign(r["post"]) == r["osign"] for r in rs if r["osign"] is not None and r["post"] != 0]
        # of the total move, how much happens AFTER the crossing (vs already priced in)?
        post_share = [abs(r["post"]) / (abs(r["pre"]) + abs(r["post"])) for r in rs
                      if (abs(r["pre"]) + abs(r["post"])) > 0]
        yes = [r["post"] for r in rs if r["osign"] == 1]
        no = [r["post"] for r in rs if r["osign"] == -1]
        sg = np.array([r["surge"] for r in rs]); ps = np.array([r["post"] for r in rs])
        corr = np.corrcoef(sg, ps)[0, 1] if sg.size > 2 and sg.std() > 0 else float("nan")
        print(f"\n  [{bucket}]  n={len(rs)}")
        print(f"    mean |pre move|  (priced in before crossing) = {np.abs(pre).mean():.1%}")
        print(f"    mean |post move| (the lag after crossing)    = {np.abs(post).mean():.1%}")
        print(f"    share of total move that is AFTER crossing   = {np.mean(post_share):.0%}")
        print(f"    asset repriced in the OUTCOME direction post-crossing = {np.mean(aligned):.0%} of the time")
        print(f"    mean post-move | outcome=YES = {np.mean(yes):+.2%}   | outcome=NO = {np.mean(no):+.2%}")
        print(f"    corr(oracle surge, post-move) = {corr:+.3f}")


asyncio.run(main())
