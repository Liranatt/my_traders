"""Dig into the actual per-archetype TEST trades: for every trade in the test bucket, show the
question, whether the event resolved YES, the Polymarket prob behaviour, and the realized price
path (entry -> peak -> trough -> exit) so we can see WHY each one won or lost. No averaging.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta

import pandas as pd

from database.db_connection import connect
from database.backtesting.schema import SCHEMA

RUN = "5fb1a1cd-7513-4085-a0c4-499d66c205ab"


def ratio_split(df, r_train, r_val):
    order = df.sort_values("t_theta").index
    n = len(order); n_tr, n_va = int(round(n*r_train)), int(round(n*r_val))
    s = pd.Series("test", index=df.index); s.loc[order[:n_tr]]="train"; s.loc[order[n_tr:n_tr+n_va]]="val"
    return s


async def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    df = pd.read_parquet("data/candidates.parquet")
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True, errors="coerce")
    df["t_e"] = pd.to_datetime(df["t_e"], utc=True, errors="coerce")
    arch = df["feat_archetype"].fillna("")
    targets = {
        "FDA": (arch.str.contains("fda", case=False), 0.50, 0.15),
        "OIL_MILITARY": (arch.str.contains("military|energy", case=False, regex=True), 0.70, 0.15),
    }
    c = await connect()
    try:
        for name, (mask, rtr, rva) in targets.items():
            g = df[mask].copy()
            g["s"] = ratio_split(g, rtr, rva)
            te = g[g.s == "test"].sort_values("t_theta")
            print(f"\n{'='*100}\n{name} TEST trades: n={len(te)}  naive_long_mean={te['asset_return'].mean()*100:+.2f}%")
            print(f"{'date':10} {'sym':6} {'longret':>8} {'peak':>6} {'trough':>6} {'|mv|':>5} {'p_in':>5} {'p_max':>5} {'p_end':>5} {'YES?':>4}  question")
            for _, r in te.iterrows():
                m = await c.fetchrow(f"SELECT question, final_outcome FROM {SCHEMA}.historical_run_markets WHERE run_id=$1 AND market_id=$2", RUN, r["market_id"])
                bars = await c.fetch(f"""SELECT ts, close FROM {SCHEMA}.historical_price_bars
                    WHERE symbol=$1 AND resolution='1d' AND ts>=$2 AND ts<=$3 ORDER BY ts""",
                    r["symbol"], r["t_theta"]-timedelta(days=1), r["t_e"]+timedelta(days=1))
                win = [(b["ts"], float(b["close"])) for b in bars if b["ts"] >= r["t_theta"]]
                if len(win) >= 2:
                    entry = win[0][1]; hi=max(p for _,p in win); lo=min(p for _,p in win); last=win[-1][1]
                    peak=(hi-entry)/entry*100; trough=(entry-lo)/entry*-100; longret=(last-entry)/entry*100
                else:
                    peak=trough=longret=float("nan")
                probs = await c.fetch(f"SELECT probability FROM {SCHEMA}.historical_probability_points WHERE market_id=$1 ORDER BY hour_ts", r["market_id"])
                pv=[float(p["probability"]) for p in probs if p["probability"] is not None]
                # prob near entry: closest to t_theta
                p_in = r.get("feat_prob_at_trigger", float("nan"))
                p_max = max(pv) if pv else float("nan"); p_end = pv[-1] if pv else float("nan")
                yes = (m["final_outcome"] or "")[:3] if m else "?"
                q = (m["question"] if m else "")[:54]
                print(f"{str(r['t_theta'].date()):10} {r['symbol']:6} {longret:+8.1f} {peak:6.1f} {trough:6.1f} "
                      f"{r['realized_abs_move']*100:5.1f} {p_in:5.2f} {p_max:5.2f} {p_end:5.2f} {yes:>4}  {q}")
    finally:
        await c.close()


if __name__ == "__main__":
    asyncio.run(main())
