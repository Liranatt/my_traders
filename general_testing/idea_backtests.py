"""Counterfactual backtests for three NEW strategy ideas, run as OUTSIDERS to the engine.

Reads an existing kept run's candidate universe (semantic-beneficiary observations at their
oracle-crossing T_theta) + prices + oracle paths straight from Postgres, and evaluates three
alternatives to the outright-long book WITHOUT touching simulation.py / the strategies:

  Idea 1  beta/sector hedge   long beneficiary minus short its sector ETF (fallback SPY).
                              Isolates the idiosyncratic event drift from ETF-driven beta
                              (paper Sec 2.2-2.3). Diagnostic: alpha or just beta?
  Idea 2  model-free gap      gate on oracle-surge-since-T0 vs asset-runup-since-T0. Trade
                              only when the oracle repriced but the asset still lags
                              (Sec 9.5 arbitrage gap, measured not predicted -> no overfit).
  Idea 3  cross-sectional RV  within an event's mapped cohort, long laggards / short leaders
                              by run-up (Boguth reversal of non-fundamental comovement).

Baseline for all three: outright long the beneficiary from T_theta. Direction is the
semantic long-only book's (every candidate here is a positive-polarity beneficiary).

Usage:  .venv/Scripts/python.exe -m general_testing.idea_backtests [run_id]
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict

import numpy as np

from database.db_connection import connect
from database.backtesting.schema import SCHEMA

DEFAULT_RUN = "29e46597-b51c-4eac-8edb-5ab78ff76e82"  # baseline proxy (full data kept)
HORIZONS = (3, 5, 10, 15)  # trading days forward from entry
TRADE_NOTIONAL = 1000.0


def _j(value):
    return value if isinstance(value, dict) else json.loads(value)


async def load(run_id: str):
    conn = await connect()
    try:
        obs = await conn.fetch(
            f"""SELECT o.event_id, o.market_id, o.symbol, o.first_pass_at, o.label_available_at,
                       o.event_archetype, o.research_data, o.classification_target,
                       o.regression_target, m.created_at AS t0
                FROM {SCHEMA}.historical_ml_observations o
                JOIN {SCHEMA}.historical_run_markets m
                  ON m.run_id=o.run_id AND m.market_id=o.market_id
                WHERE o.run_id=$1 AND o.valid_for_training""",
            run_id,
        )
        sector = {
            r["symbol"]: r["sector_etf"]
            for r in await conn.fetch(
                f"SELECT symbol, sector_etf FROM {SCHEMA}.historical_asset_metadata WHERE sector_etf IS NOT NULL"
            )
        }
        # earliest oracle probability per market = prob at (or near) T0
        prob_t0 = {
            r["market_id"]: float(r["probability"])
            for r in await conn.fetch(
                f"""SELECT DISTINCT ON (market_id) market_id, probability
                    FROM {SCHEMA}.historical_probability_points
                    ORDER BY market_id, hour_ts"""
            )
        }
        symbols = {r["symbol"] for r in obs}
        symbols |= {sector.get(s) for s in symbols if sector.get(s)}
        symbols |= {"SPY"}
        symbols = [s for s in symbols if s]
        bars = await conn.fetch(
            f"""SELECT symbol, ts, close FROM {SCHEMA}.historical_price_bars
                WHERE resolution='1d' AND symbol = ANY($1::text[])
                ORDER BY symbol, ts""",
            symbols,
        )
    finally:
        await conn.close()

    series: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    by_sym: dict[str, list] = defaultdict(list)
    for b in bars:
        by_sym[b["symbol"]].append((np.datetime64(b["ts"].replace(tzinfo=None)), float(b["close"])))
    for sym, rows in by_sym.items():
        rows.sort()
        series[sym] = (
            np.array([d for d, _ in rows], dtype="datetime64[ns]"),
            np.array([c for _, c in rows], dtype=float),
        )
    return obs, sector, prob_t0, series


def _entry_idx(series, sym, t):
    if sym not in series:
        return None
    dates, _ = series[sym]
    i = int(np.searchsorted(dates, np.datetime64(t.replace(tzinfo=None)), side="left"))
    return i if i < len(dates) else None


def fwd_return(series, sym, t_theta, horizon):
    """Close-to-close return from first bar >= T_theta to `horizon` bars later."""
    i = _entry_idx(series, sym, t_theta)
    if i is None:
        return None
    dates, closes = series[sym]
    j = i + horizon
    if j >= len(closes) or closes[i] <= 0:
        return None
    return closes[j] / closes[i] - 1.0


def close_before(series, sym, t):
    if sym not in series:
        return None
    dates, closes = series[sym]
    i = int(np.searchsorted(dates, np.datetime64(t.replace(tzinfo=None)), side="right")) - 1
    return float(closes[i]) if i >= 0 else None


def stats(returns: list[float]) -> dict:
    a = np.array([r for r in returns if r is not None], dtype=float)
    if a.size == 0:
        return {"n": 0}
    return {
        "n": int(a.size),
        "mean_pct": float(a.mean() * 100),
        "median_pct": float(np.median(a) * 100),
        "hit": float((a > 0).mean()),
        "std_pct": float(a.std() * 100),
        "sharpe_trade": float(a.mean() / a.std()) if a.std() > 0 else float("nan"),
        "net_per_1k": float(a.sum() * TRADE_NOTIONAL),
    }


def line(label, s):
    if s.get("n", 0) == 0:
        print(f"   {label:26} n=0")
        return
    print(
        f"   {label:26} n={s['n']:4} mean={s['mean_pct']:+6.2f}% hit={s['hit']:.0%} "
        f"std={s['std_pct']:5.2f}% sharpe/trade={s['sharpe_trade']:+5.2f} net/$1k=${s['net_per_1k']:+,.0f}"
    )


def idea_1_and_baseline(obs, sector, series):
    print("\n" + "=" * 96)
    print("IDEA 1 + BASELINE  —  outright long vs sector-hedged vs SPY-hedged  (per horizon)")
    print("=" * 96)
    for h in HORIZONS:
        outright, sec_hedge, spy_hedge = [], [], []
        for o in obs:
            sym = o["symbol"]
            a = fwd_return(series, sym, o["first_pass_at"], h)
            if a is None:
                continue
            outright.append(a)
            etf = sector.get(sym)
            e = fwd_return(series, etf, o["first_pass_at"], h) if etf else None
            sec_hedge.append(a - e if e is not None else a - (fwd_return(series, "SPY", o["first_pass_at"], h) or 0.0))
            spy = fwd_return(series, "SPY", o["first_pass_at"], h)
            if spy is not None:
                spy_hedge.append(a - spy)
        print(f"\n-- horizon = {h} trading days --")
        line("outright long", stats(outright))
        line("hedged vs sector ETF", stats(sec_hedge))
        line("hedged vs SPY", stats(spy_hedge))


def idea_2_model_free_gap(obs, prob_t0, series):
    print("\n" + "=" * 96)
    print("IDEA 2  —  model-free arbitrage gap: oracle-surge-since-T0 vs asset-runup-since-T0")
    print("=" * 96)
    rows = []
    for o in obs:
        rd = _j(o["research_data"])
        anchor = rd.get("anchor_close_price") or rd.get("event_open_price")
        p_now = rd.get("current_probability")
        p0 = prob_t0.get(o["market_id"])
        c_t0 = close_before(series, o["symbol"], o["t0"])
        if not anchor or p_now is None or p0 is None or not c_t0 or c_t0 <= 0:
            continue
        oracle_surge = float(p_now) - float(p0)          # how much the oracle repriced
        asset_runup = float(anchor) / float(c_t0) - 1.0  # how much the asset already moved
        fwd = fwd_return(series, o["symbol"], o["first_pass_at"], 10)  # forward 10d drift
        if fwd is None:
            continue
        rows.append((oracle_surge, asset_runup, fwd))
    if not rows:
        print("   no rows")
        return
    arr = np.array(rows)
    # gap score = z(oracle_surge) - z(asset_runup): big when oracle moved but asset lagged
    def z(x):
        return (x - x.mean()) / (x.std() if x.std() > 0 else 1.0)
    gap = z(arr[:, 0]) - z(arr[:, 1])
    order = np.argsort(gap)
    n = len(order)
    terciles = {"gap CLOSED (asset led)": order[: n // 3],
                "middle": order[n // 3: 2 * n // 3],
                "gap OPEN (oracle led, asset lags)": order[2 * n // 3:]}
    print("\n   forward 10d return of the long, split by the measured gap:")
    for label, idx in terciles.items():
        line(label, stats(list(arr[idx, 2])))
    # correlation check
    c = np.corrcoef(gap, arr[:, 2])[0, 1]
    print(f"\n   corr(gap_score, forward_10d_return) = {c:+.3f}  (positive => the gap predicts drift)")


def idea_3_cross_sectional(obs, sector, series):
    print("\n" + "=" * 96)
    print("IDEA 3  —  within-event cross-sectional: rank cohort by run-up since T0, per horizon")
    print("           (my reversal hypothesis vs the data; + is the leader effect ALPHA or BETA?)")
    print("=" * 96)
    for h in HORIZONS:
        by_event = defaultdict(list)
        for o in obs:
            rd = _j(o["research_data"])
            anchor = rd.get("anchor_close_price") or rd.get("event_open_price")
            c_t0 = close_before(series, o["symbol"], o["t0"])
            fwd = fwd_return(series, o["symbol"], o["first_pass_at"], h)
            if not anchor or not c_t0 or c_t0 <= 0 or fwd is None:
                continue
            etf = sector.get(o["symbol"])
            efwd = fwd_return(series, etf, o["first_pass_at"], h) if etf else None
            hedged = fwd - efwd if efwd is not None else None
            runup = float(anchor) / float(c_t0) - 1.0
            by_event[o["event_id"]].append((runup, fwd, hedged))
        lead_rets, lag_rets, spreads = [], [], []
        lead_hedged, lag_hedged, hedged_reversal = [], [], []
        used = 0
        for members in by_event.values():
            if len(members) < 3:
                continue
            used += 1
            members.sort(key=lambda m: m[0])  # ascending run-up: laggards first, leaders last
            k = max(1, len(members) // 3)
            laggards, leaders = members[:k], members[-k:]
            lead_rets.append(np.mean([f for _, f, _ in leaders]))
            lag_rets.append(np.mean([f for _, f, _ in laggards]))
            spreads.append(np.mean([f for _, f, _ in leaders]) - np.mean([f for _, f, _ in laggards]))
            lh = [hh for _, _, hh in leaders if hh is not None]
            gh = [hh for _, _, hh in laggards if hh is not None]
            if lh:
                lead_hedged.append(np.mean(lh))
            if gh:
                lag_hedged.append(np.mean(gh))
            if lh and gh:  # beta-neutral Boguth reversal: long laggard, short leader, both hedged
                hedged_reversal.append(np.mean(gh) - np.mean(lh))
        print(f"\n-- horizon = {h} trading days  (events used = {used}) --")
        line("LEADER raw (long top run-up)", stats(lead_rets))
        line("laggard raw (long bot run-up)", stats(lag_rets))
        line("raw momentum spread lead-lag", stats(spreads))
        line("LEADER hedged vs sector", stats(lead_hedged))
        line("laggard hedged vs sector", stats(lag_hedged))
        line("HEDGED reversal lag-lead", stats(hedged_reversal))


async def main():
    run_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_RUN
    print(f"loading candidate universe for run {run_id} ...")
    obs, sector, prob_t0, series = await load(run_id)
    print(f"candidates={len(obs)}  symbols_with_prices={len(series)}  "
          f"sector_etf_mapped={sum(1 for o in obs if sector.get(o['symbol']))}")
    idea_1_and_baseline(obs, sector, series)
    idea_2_model_free_gap(obs, prob_t0, series)
    idea_3_cross_sectional(obs, sector, series)


if __name__ == "__main__":
    asyncio.run(main())
