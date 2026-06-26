"""Standalone opportunity-cost backtest.

Runs CEM optimization with money starting in SPY and QQQ,
with IB transaction costs + slippage on all 4 legs.

Usage:
    python -u run_opp_cost.py
"""
from __future__ import annotations

import asyncio, sys, time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from pipeline.strategy import DEFAULT_POLICY, simulate_one, score_sharpe_per_day

PROJECT = Path(__file__).resolve().parent
REL_COL = "feat_connection_strength"

# ── Portfolio knobs ──────────────────────────────────────────────────

PORTFOLIO_BOUNDS = dict(
    atr_mult=(1.5, 4.0), lock_activate=(0.02, 0.10), theta_out=(0.45, 0.60),
    enter_strong=(0.60, 0.85), enter_floor=(0.55, 0.80), hold_days=(1, 5),
    max_prob_surge=(0.20, 0.80), max_price_runup=(0.02, 0.20),
    position_size_pct=(0.03, 0.20), max_concurrent=(3, 15),
)
PORT_DEFAULT = {**DEFAULT_POLICY, "position_size_pct": 0.10, "max_concurrent": 10}


def port_policy_from_vec(vec):
    names = list(PORTFOLIO_BOUNDS.keys())
    p = {}
    for i, n in enumerate(names):
        lo, hi = PORTFOLIO_BOUNDS[n]
        p[n] = float(np.clip(vec[i], lo, hi))
    p["hold_days"] = int(round(p["hold_days"]))
    p["max_concurrent"] = int(round(p["max_concurrent"]))
    if p["enter_strong"] < p["enter_floor"]:
        p["enter_strong"] = p["enter_floor"]
    return p


# ── IB cost model ────────────────────────────────────────────────────

def ib_cost(shares: int, price: float, is_sell: bool) -> float:
    if shares <= 0 or price <= 0:
        return 0.0
    trade_val = shares * price
    commission = max(0.35, min(shares * 0.0035, trade_val * 0.01))
    sec = trade_val * 0.0000278 if is_sell else 0.0
    slippage = trade_val * 0.0005
    return commission + sec + slippage


def _bench_close_on(prices, sym, date):
    bars = prices.get(sym, [])
    c = None
    for t, _h, _l, cl in bars:
        if t <= date:
            c = cl
        else:
            break
    return c


def _asset_close_on(prices, sym, date):
    return _bench_close_on(prices, sym, date)


# ── Opportunity-cost portfolio sim ───────────────────────────────────

def sim_portfolio_opp_cost(df, prices, probs, policy,
                           bench_sym="SPY", initial=100_000):
    ps = policy.get("position_size_pct", 0.10)
    mc = policy.get("max_concurrent", 10)

    empty_stats = {"initial": initial, "final": initial, "total_return": 0,
                    "max_dd": 0, "n_trades": 0, "total_pnl": 0,
                    "avg_pnl": 0, "win_rate": 0, "total_txn_cost": 0,
                    "bench_sym": bench_sym}
    if df.empty:
        return pd.DataFrame(), [], empty_stats, policy

    df_s = df.sort_values("t_theta").copy()
    all_trades = []
    for _, row in df_s.iterrows():
        trade = simulate_one(row, prices, probs, policy)
        if trade is not None:
            trade["_entry_ts"] = pd.Timestamp(trade["entry_date"], tz="UTC")
            trade["_exit_ts"] = pd.Timestamp(trade["exit_date"], tz="UTC")
            all_trades.append(trade)
    all_trades.sort(key=lambda t: t["_entry_ts"])

    bench_bars = prices.get(bench_sym, [])
    if not bench_bars:
        return pd.DataFrame(), [], empty_stats, policy

    cal_dates = sorted(set(t for t, *_ in bench_bars))
    if all_trades:
        first_date = min(t["_entry_ts"] for t in all_trades)
        last_date = max(t["_exit_ts"] for t in all_trades)
        cal_dates = [d for d in cal_dates if d >= first_date - pd.Timedelta(days=5)
                     and d <= last_date + pd.Timedelta(days=5)]
    if not cal_dates:
        return pd.DataFrame(), [], empty_stats, policy

    first_bench_close = _bench_close_on(prices, bench_sym, cal_dates[0])
    if first_bench_close is None or first_bench_close <= 0:
        first_bench_close = bench_bars[0][3]
    bench_shares = int(initial / first_bench_close)
    cash = initial - bench_shares * first_bench_close
    buy_cost_init = ib_cost(bench_shares, first_bench_close, False)
    cash -= buy_cost_init
    total_txn = buy_cost_init

    open_pos = []
    completed = []
    eq_log = []
    trade_idx = 0

    for day in cal_dates:
        bc = _bench_close_on(prices, bench_sym, day)
        if bc is None:
            continue

        still_open = []
        for pos in open_pos:
            if pos["_exit_ts"] <= day:
                sym = pos["symbol"]
                xp = pos["exit_price"]
                qty = pos["_qty"]
                sell_cost = ib_cost(qty, xp, True)
                net_proceeds = qty * xp - sell_cost
                total_txn += sell_cost
                rebuy_shares = int(net_proceeds / bc) if bc > 0 else 0
                rebuy_cost = ib_cost(rebuy_shares, bc, False)
                total_txn += rebuy_cost
                cash += net_proceeds - rebuy_shares * bc - rebuy_cost
                bench_shares += rebuy_shares
                entry_cost = pos["_entry_cost_total"]
                exit_val = qty * xp
                pnl = exit_val - pos["_cost_basis"]
                pos["pnl"] = round(pnl, 2)
                pos["pnl_pct"] = round(pnl / pos["_cost_basis"] * 100, 2) if pos["_cost_basis"] > 0 else 0
                pos["exit_value"] = round(exit_val, 2)
                pos["txn_cost"] = round(entry_cost + sell_cost + rebuy_cost, 2)
                completed.append(pos)
            else:
                still_open.append(pos)
        open_pos = still_open

        while trade_idx < len(all_trades):
            trade = all_trades[trade_idx]
            if trade["_entry_ts"] > day:
                break
            trade_idx += 1
            if trade["_entry_ts"] < day:
                continue
            if len(open_pos) >= mc:
                continue

            total_eq = bench_shares * bc + sum(
                p["_qty"] * (_asset_close_on(prices, p["symbol"], day) or p["entry_price"])
                for p in open_pos) + cash
            alloc = total_eq * ps
            ep = trade["entry_price"]
            if ep <= 0 or alloc < ep:
                continue

            sell_bench_qty = min(int(alloc / bc), bench_shares) if bc > 0 else 0
            if sell_bench_qty < 1:
                continue
            sell_bench_cost = ib_cost(sell_bench_qty, bc, True)
            total_txn += sell_bench_cost
            net_from_bench = sell_bench_qty * bc - sell_bench_cost
            bench_shares -= sell_bench_qty

            alpha_qty = int(net_from_bench / ep)
            if alpha_qty < 1:
                bench_shares += sell_bench_qty
                cash += sell_bench_cost
                total_txn -= sell_bench_cost
                continue
            buy_alpha_cost = ib_cost(alpha_qty, ep, False)
            total_txn += buy_alpha_cost
            cost_basis = alpha_qty * ep + buy_alpha_cost
            cash += net_from_bench - alpha_qty * ep - buy_alpha_cost

            pos = {**trade, "_qty": alpha_qty, "_cost_basis": round(cost_basis, 2),
                   "_entry_cost_total": round(sell_bench_cost + buy_alpha_cost, 2)}
            open_pos.append(pos)

        open_val = sum(
            p["_qty"] * (_asset_close_on(prices, p["symbol"], day) or p["entry_price"])
            for p in open_pos)
        equity = bench_shares * bc + open_val + cash
        eq_log.append({"date": str(day.date()), "equity": round(equity, 2),
                       "positions": len(open_pos),
                       "bench_shares": bench_shares,
                       "capital": round(cash, 2)})

    for pos in open_pos:
        xp = pos["exit_price"]
        qty = pos["_qty"]
        sell_cost = ib_cost(qty, xp, True)
        net = qty * xp - sell_cost
        total_txn += sell_cost
        last_bc = bench_bars[-1][3]
        rebuy = int(net / last_bc) if last_bc > 0 else 0
        rebuy_c = ib_cost(rebuy, last_bc, False)
        total_txn += rebuy_c
        cash += net - rebuy * last_bc - rebuy_c
        bench_shares += rebuy
        pnl = qty * xp - pos["_cost_basis"]
        pos["pnl"] = round(pnl, 2)
        pos["pnl_pct"] = round(pnl / pos["_cost_basis"] * 100, 2) if pos["_cost_basis"] > 0 else 0
        pos["exit_value"] = round(qty * xp, 2)
        pos["txn_cost"] = round(pos["_entry_cost_total"] + sell_cost + rebuy_c, 2)
        completed.append(pos)

    final_bc = bench_bars[-1][3]
    final_eq = bench_shares * final_bc + cash
    tot_ret = (final_eq / initial - 1) * 100

    if eq_log:
        ev = [e["equity"] for e in eq_log]
        pk = np.maximum.accumulate(ev)
        dd = [(v - p) / p * 100 if p > 0 else 0 for v, p in zip(ev, pk)]
        max_dd = min(dd) if dd else 0
    else:
        max_dd = 0

    tdf = pd.DataFrame(completed) if completed else pd.DataFrame()
    stats = {
        "initial": initial, "final": round(final_eq, 2),
        "total_return": round(tot_ret, 2), "max_dd": round(max_dd, 2),
        "n_trades": len(completed),
        "total_pnl": round(sum(t["pnl"] for t in completed), 2) if completed else 0,
        "avg_pnl": round(float(np.mean([t["pnl"] for t in completed])), 2) if completed else 0,
        "win_rate": round(float(np.mean([t["pnl"] > 0 for t in completed]) * 100), 1) if completed else 0,
        "total_txn_cost": round(total_txn, 2),
        "bench_sym": bench_sym,
    }
    return tdf, eq_log, stats, policy


# ── CEM optimizer ────────────────────────────────────────────────────

def port_cem_opp_cost(df, prices, probs, bench_sym="SPY",
                      reward_mode="sharpe", n_iter=8, pop=30, seed=42):
    rng = np.random.default_rng(seed)
    names = list(PORTFOLIO_BOUNDS.keys())
    dim = len(names)
    ek = max(2, int(pop * 0.25))
    mean = np.array([PORT_DEFAULT[n] for n in names], float)
    std = np.array([(PORTFOLIO_BOUNDS[n][1] - PORTFOLIO_BOUNDS[n][0]) / 4
                    for n in names], float)
    df_tr = df[df["split"] == "train"]
    best_s, best_p = -999, None

    for it in range(n_iter):
        samps = rng.normal(mean, std, size=(pop, dim))
        pols = [port_policy_from_vec(s) for s in samps]
        scores = []
        for p in pols:
            t, _, st, _ = sim_portfolio_opp_cost(
                df_tr, prices, probs, p, bench_sym=bench_sym)
            if t.empty or st["n_trades"] < 3:
                scores.append(-999)
                continue
            sh = score_sharpe_per_day(t)
            if reward_mode == "max_pnl":
                scores.append(st["total_return"])
            elif reward_mode == "min_dd":
                scores.append(sh - 1.5 * abs(st["max_dd"]))
            else:
                scores.append(sh - 0.3 * abs(st["max_dd"]))
        scores = np.array(scores)
        ei = np.argsort(scores)[-ek:]
        elite = samps[ei]
        mean = elite.mean(0)
        std = elite.std(0) + 1e-4
        ib = scores.max()
        if ib > best_s:
            best_s = ib
            best_p = pols[ei[-1]]
        print(f"  CEM-OC({bench_sym},{reward_mode}) iter {it+1}/{n_iter} best={ib:+.3f}")
    return best_p


# ── Data loading ─────────────────────────────────────────────────────

async def load_paths(df):
    c = await connect()
    try:
        syms = sorted(set(df["symbol"].unique()) | {"SPY", "QQQ"})
        mkts = sorted(df["market_id"].unique())
        bars = await c.fetch(
            f"SELECT symbol,ts,high,low,close FROM {SCHEMA}.historical_price_bars "
            f"WHERE resolution='1d' AND symbol=ANY($1::text[]) ORDER BY symbol,ts", syms)
        prob_rows = await c.fetch(
            f"""SELECT DISTINCT ON (market_id,(hour_ts AT TIME ZONE 'UTC')::date)
                market_id,(hour_ts AT TIME ZONE 'UTC')::date AS d, probability
                FROM {SCHEMA}.historical_probability_points
                WHERE market_id=ANY($1::text[])
                  AND EXTRACT(HOUR FROM hour_ts AT TIME ZONE 'UTC') <= 20
                ORDER BY market_id,(hour_ts AT TIME ZONE 'UTC')::date, hour_ts DESC""", mkts)
    finally:
        await c.close()
    P = {}
    for b in bars:
        P.setdefault(b["symbol"], []).append((
            pd.Timestamp(b["ts"]).tz_convert("UTC").normalize(),
            float(b["high"]), float(b["low"]), float(b["close"])))
    PR = {}
    for p in prob_rows:
        PR.setdefault(p["market_id"], []).append((
            pd.Timestamp(p["d"]).tz_localize("UTC"), float(p["probability"])))
    for d in (P, PR):
        for k in d:
            d[k].sort()
    return P, PR


# ── Main ─────────────────────────────────────────────────────────────

def main():
    t0 = time.time()

    print("=" * 70)
    print("  OPPORTUNITY-COST BACKTEST")
    print("  $100k starts invested in SPY / QQQ")
    print("  IB tiered costs + 5bp slippage on all 4 legs")
    print("=" * 70)

    print("\nLoading candidates...")
    df = pd.read_parquet(PROJECT / "data" / "candidates.parquet")
    df = df[df[REL_COL].astype(float) > 0.5].copy()
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True)
    df["t_e"] = pd.to_datetime(df["t_e"], utc=True)
    print(f"  {len(df)} candidates loaded")

    print("Loading prices/probs from DB...")
    P, PR = asyncio.run(load_paths(df))
    print(f"  {len(P)} symbols, {len(PR)} markets")

    # Compute buy-and-hold benchmarks
    for bsym in ("SPY", "QQQ"):
        bars = P.get(bsym, [])
        if len(bars) >= 2:
            first_c = bars[0][3]
            last_c = bars[-1][3]
            bnh_ret = (last_c / first_c - 1) * 100
            print(f"  {bsym} buy-and-hold: ${first_c:.2f} -> ${last_c:.2f}  ({bnh_ret:+.1f}%)")

    results = []

    for bsym in ("SPY", "QQQ"):
        print(f"\n{'=' * 70}")
        print(f"  BENCHMARK: {bsym}")
        print(f"{'=' * 70}")

        # 1. CEM search (3 objectives)
        print(f"\n  --- CEM Sharpe ({bsym}) ---")
        pol_sharpe = port_cem_opp_cost(df, P, PR, bsym, "sharpe",
                                        n_iter=8, pop=30, seed=42)
        print(f"\n  --- CEM Max P&L ({bsym}) ---")
        pol_maxpnl = port_cem_opp_cost(df, P, PR, bsym, "max_pnl",
                                        n_iter=8, pop=30, seed=55)
        print(f"\n  --- CEM Min-DD ({bsym}) ---")
        pol_mindd = port_cem_opp_cost(df, P, PR, bsym, "min_dd",
                                       n_iter=8, pop=30, seed=99)

        # 2. Run full dataset sims
        for name, policy in [
            (f"{bsym} -- Default", PORT_DEFAULT),
            (f"{bsym} -- CEM Sharpe", pol_sharpe),
            (f"{bsym} -- CEM Max P&L", pol_maxpnl),
            (f"{bsym} -- CEM Min-DD", pol_mindd),
        ]:
            print(f"\n  Running: {name}...")
            tdf, eq, stats, pol = sim_portfolio_opp_cost(
                df, P, PR, policy, bench_sym=bsym)
            results.append({"name": name, "stats": stats, "policy": pol,
                            "tdf": tdf, "eq": eq})

    # ── Print results ────────────────────────────────────────────────

    print(f"\n\n{'=' * 70}")
    print("  RESULTS SUMMARY")
    print(f"{'=' * 70}")
    print(f"\n  {'Experiment':30s}  {'Return':>10s}  {'Max DD':>8s}  {'Trades':>7s}  "
          f"{'Win%':>6s}  {'Avg P&L':>9s}  {'Txn Cost':>10s}  {'Final $':>12s}")
    print(f"  {'-' * 100}")

    for r in results:
        s = r["stats"]
        print(f"  {r['name']:30s}  {s['total_return']:+9.2f}%  "
              f"{s['max_dd']:+7.2f}%  {s['n_trades']:6d}   "
              f"{s['win_rate']:5.1f}%  ${s['avg_pnl']:>8.2f}  "
              f"${s['total_txn_cost']:>9.2f}  ${s['final']:>11,.2f}")

    # ── Print CEM-learned policies ───────────────────────────────────

    print(f"\n\n{'=' * 70}")
    print("  CEM-LEARNED POLICIES")
    print(f"{'=' * 70}")
    for r in results:
        if "Default" in r["name"]:
            continue
        print(f"\n  {r['name']}:")
        pol = r["policy"]
        for k, v in pol.items():
            if k.startswith("_"):
                continue
            print(f"    {k:20s} = {v}")

    # ── Print trade details per experiment ───────────────────────────

    for r in results:
        tdf = r["tdf"]
        if tdf.empty:
            continue
        print(f"\n\n{'=' * 70}")
        print(f"  TRADE DETAILS: {r['name']}")
        print(f"{'=' * 70}")
        print(f"\n  {'Symbol':8s}  {'Entry':>12s}  {'Exit':>12s}  {'Ret%':>8s}  "
              f"{'P&L $':>10s}  {'TxnCost':>8s}  {'Reason':20s}")
        print(f"  {'-' * 85}")
        for _, t in tdf.iterrows():
            sym = str(t.get("symbol", ""))[:8]
            ed = str(t.get("entry_date", ""))[:10]
            xd = str(t.get("exit_date", ""))[:10]
            ret = t.get("return_pct", t.get("pnl_pct", 0))
            pnl = t.get("pnl", 0)
            txn = t.get("txn_cost", 0)
            reason = str(t.get("exit_reason", ""))[:20]
            print(f"  {sym:8s}  {ed:>12s}  {xd:>12s}  {ret:+7.2f}%  "
                  f"${pnl:>9.2f}  ${txn:>7.2f}  {reason:20s}")

    elapsed = time.time() - t0
    print(f"\n\nDone in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
