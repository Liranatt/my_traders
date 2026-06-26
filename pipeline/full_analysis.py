"""Comprehensive backtest analysis.

Splits earnings vs non-earnings, compares vs SPY/QQQ,
runs RF with reduced features, simulates $100K portfolio with CEM sizing.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from pipeline.strategy import (
    DEFAULT_POLICY, RL_BOUNDS, run_backtest, simulate_one,
    policy_from_vector, score_sharpe_per_day, score_mean_return,
    calc_atr, entry_day,
)
from pipeline.data_loader import NUM_FEATURES_LEAN, CAT_FEATURES_LEAN, TARGET
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline as SkPipeline

PROJECT = Path(__file__).resolve().parents[1]
RELEVANCE_COL = "feat_connection_strength"


# ── data loading ──────────────────────────────────────────────────

async def load_paths(df):
    c = await connect()
    try:
        syms = sorted(set(df["symbol"].unique()) | {"SPY", "QQQ"})
        mkts = sorted(df["market_id"].unique())
        bars = await c.fetch(
            f"SELECT symbol, ts, high, low, close FROM {SCHEMA}.historical_price_bars "
            f"WHERE resolution='1d' AND symbol=ANY($1::text[]) ORDER BY symbol, ts", syms)
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


def benchmark_return(prices, sym, entry_date, exit_date):
    e = pd.Timestamp(entry_date, tz="UTC")
    x = pd.Timestamp(exit_date, tz="UTC")
    bars = prices.get(sym, [])
    pe = next((c for t, h, l, c in bars if t >= e), None)
    px = next((c for t, h, l, c in bars if t >= x), None)
    if pe and px and pe > 0:
        return (px / pe - 1.0) * 100
    return None


def add_benchmarks(tdf, prices):
    tdf = tdf.copy()
    tdf["spy_return"] = tdf.apply(
        lambda r: benchmark_return(prices, "SPY", r["entry_date"], r["exit_date"]), axis=1)
    tdf["qqq_return"] = tdf.apply(
        lambda r: benchmark_return(prices, "QQQ", r["entry_date"], r["exit_date"]), axis=1)
    tdf["excess_spy"] = tdf["return_pct"] - tdf["spy_return"]
    tdf["excess_qqq"] = tdf["return_pct"] - tdf["qqq_return"]
    tdf["holding_days"] = (pd.to_datetime(tdf["exit_date"]) - pd.to_datetime(tdf["entry_date"])).dt.days
    return tdf


# ── printing ──────────────────────────────────────────────────────

def print_split_table(tdf, label=""):
    if label:
        print(f"\n  {label}")
    print(f"  {'split':5} {'n':>5} {'mean':>8} {'win%':>6} {'med':>7} {'sharpe':>7} {'hold_d':>7} "
          f"{'vs_SPY':>8} {'vs_QQQ':>8}")
    print(f"  {'-'*72}")
    for sp in ("train", "val", "test"):
        s = tdf[tdf.split == sp] if "split" in tdf.columns else pd.DataFrame()
        if s.empty:
            print(f"  {sp:5}     0")
            continue
        n = len(s)
        mr = s["return_pct"].mean()
        wr = (s["return_pct"] > 0).mean() * 100
        med = s["return_pct"].median()
        sh = score_sharpe_per_day(s)
        hd = s["holding_days"].mean()
        vs_spy = s["excess_spy"].mean() if "excess_spy" in s else 0
        vs_qqq = s["excess_qqq"].mean() if "excess_qqq" in s else 0
        print(f"  {sp:5} {n:5d} {mr:+8.2f}% {wr:5.0f}% {med:+7.2f}% {sh:+7.2f} {hd:6.1f}d "
              f"{vs_spy:+8.2f}% {vs_qqq:+8.2f}%")
    if not tdf.empty and "exit_reason" in tdf.columns:
        reasons = tdf["exit_reason"].apply(lambda x: x.split("_")[0] if "_" in x else x.split("<")[0] if "<" in x else x)
        print(f"  exits: {reasons.value_counts().to_dict()}")


def print_detailed_trades(tdf, label="", max_show=30):
    if label:
        print(f"\n  {label} ({len(tdf)} trades)")
    for _, t in tdf.head(max_show).iterrows():
        arch = str(t.get("archetype", ""))[:45]
        spy = t.get("excess_spy", 0)
        print(f"    {t['symbol']:6s}  ret={t['return_pct']:+7.2f}%  hold={t['holding_days']:2.0f}d  "
              f"spy_ex={spy:+6.2f}%  exit={str(t['exit_reason'])[:22]:22s}  "
              f"{t['split']:5s}  {str(t['entry_date'])[:10]}  {arch}")


# ── portfolio simulation ──────────────────────────────────────────

PORTFOLIO_BOUNDS = dict(
    atr_mult=(1.5, 4.0),
    lock_activate=(0.02, 0.10),
    theta_out=(0.45, 0.60),
    enter_strong=(0.60, 0.85),
    enter_floor=(0.55, 0.80),
    hold_days=(1, 5),
    max_prob_surge=(0.20, 0.80),
    max_price_runup=(0.02, 0.20),
    position_size_pct=(0.03, 0.20),
    max_concurrent=(3, 15),
)

PORTFOLIO_DEFAULT = {
    **DEFAULT_POLICY,
    "position_size_pct": 0.10,
    "max_concurrent": 10,
}


def portfolio_policy_from_vector(vec):
    names = list(PORTFOLIO_BOUNDS.keys())
    p = {}
    for i, name in enumerate(names):
        lo, hi = PORTFOLIO_BOUNDS[name]
        p[name] = float(np.clip(vec[i], lo, hi))
    p["hold_days"] = int(round(p["hold_days"]))
    p["max_concurrent"] = int(round(p["max_concurrent"]))
    if p["enter_strong"] < p["enter_floor"]:
        p["enter_strong"] = p["enter_floor"]
    return p


def simulate_portfolio(df, prices, probs, policy, initial_capital=100_000):
    """Walk through trades chronologically, respecting capital and concurrency."""
    df_sorted = df.sort_values("t_theta").copy()
    capital = initial_capital
    open_positions = []
    completed = []
    equity_log = []
    pos_size_pct = policy.get("position_size_pct", 0.10)
    max_conc = policy.get("max_concurrent", 10)

    for _, row in df_sorted.iterrows():
        t_theta = pd.Timestamp(row["t_theta"]).tz_convert("UTC")

        closed = []
        for pos in open_positions:
            if pd.Timestamp(pos["exit_date"], tz="UTC") <= t_theta:
                capital += pos["exit_value"]
                completed.append(pos)
                closed.append(pos)
        for pos in closed:
            open_positions.remove(pos)

        if len(open_positions) >= max_conc:
            continue

        trade = simulate_one(row, prices, probs, policy)
        if trade is None:
            continue

        alloc = capital * pos_size_pct
        entry_p = trade["entry_price"]
        if entry_p <= 0 or alloc < entry_p:
            continue
        qty = int(alloc / entry_p)
        if qty < 1:
            continue

        cost = qty * entry_p
        exit_p = trade["exit_price"]
        exit_value = qty * exit_p
        pnl = exit_value - cost

        capital -= cost

        pos = {**trade,
               "qty": qty,
               "cost": round(cost, 2),
               "exit_value": round(exit_value, 2),
               "pnl": round(pnl, 2),
               "pnl_pct": round(pnl / cost * 100, 2),
               }
        open_positions.append(pos)

        open_value = sum(p["cost"] * (1 + p["return_pct"] / 100) for p in open_positions)
        equity_log.append({"date": str(t_theta.date()), "equity": round(capital + open_value, 2),
                           "open_positions": len(open_positions), "capital": round(capital, 2)})

    for pos in open_positions:
        capital += pos["exit_value"]
        completed.append(pos)

    tdf = pd.DataFrame(completed) if completed else pd.DataFrame()
    eq = pd.DataFrame(equity_log) if equity_log else pd.DataFrame()
    final_equity = capital
    total_return = (final_equity / initial_capital - 1) * 100

    if not eq.empty:
        eq_vals = eq["equity"]
        peak = eq_vals.cummax()
        dd = (eq_vals - peak) / peak * 100
        max_dd = dd.min()
    else:
        max_dd = 0

    stats = {
        "initial": initial_capital,
        "final": round(final_equity, 2),
        "total_return_pct": round(total_return, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "n_trades": len(completed),
        "total_pnl": round(sum(t["pnl"] for t in completed), 2),
        "avg_pnl": round(np.mean([t["pnl"] for t in completed]), 2) if completed else 0,
        "win_rate": round(np.mean([t["pnl"] > 0 for t in completed]) * 100, 1) if completed else 0,
    }
    return tdf, eq, stats


def portfolio_cem(df, prices, probs, n_iter=10, pop_size=40, elite_frac=0.25, seed=42):
    """CEM search with portfolio-level knobs."""
    rng = np.random.default_rng(seed)
    names = list(PORTFOLIO_BOUNDS.keys())
    dim = len(names)
    elite_k = max(2, int(pop_size * elite_frac))

    mean = np.array([PORTFOLIO_DEFAULT[n] for n in names], dtype=float)
    std = np.array([(PORTFOLIO_BOUNDS[n][1] - PORTFOLIO_BOUNDS[n][0]) / 4.0 for n in names], dtype=float)

    df_train = df[df["split"] == "train"]
    best_score = -999.0
    best_policy = None

    for it in range(n_iter):
        samples = rng.normal(mean, std, size=(pop_size, dim))
        policies = [portfolio_policy_from_vector(s) for s in samples]

        scores = []
        for p in policies:
            tdf, _, stats = simulate_portfolio(df_train, prices, probs, p)
            if tdf.empty or stats["n_trades"] < 5:
                scores.append(-999.0)
                continue
            sharpe = score_sharpe_per_day(tdf)
            dd_pen = abs(stats["max_drawdown_pct"]) * 0.3
            scores.append(sharpe - dd_pen)
        scores = np.array(scores)

        elite_idx = np.argsort(scores)[-elite_k:]
        elite = samples[elite_idx]
        mean = elite.mean(axis=0)
        std = elite.std(axis=0) + 1e-4

        it_best = scores.max()
        if it_best > best_score:
            best_score = it_best
            best_policy = policies[elite_idx[-1]]

        key_params = f"pos_size={mean[names.index('position_size_pct')]:.1%} max_conc={mean[names.index('max_concurrent')]:.0f}"
        print(f"  iter {it:2d}/{n_iter}  best={it_best:+.3f}  {key_params}")

    print(f"\n  CONVERGED: pos_size={best_policy['position_size_pct']:.1%}  "
          f"max_conc={best_policy['max_concurrent']}  "
          f"atr={best_policy['atr_mult']:.2f}  runup={best_policy['max_price_runup']:.3f}")
    return best_policy


# ── main ──────────────────────────────────────────────────────────

def main():
    df = pd.read_parquet(PROJECT / "data" / "candidates.parquet")
    df = df[df[RELEVANCE_COL].astype(float) > 0.5].copy()
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True)
    df["t_e"] = pd.to_datetime(df["t_e"], utc=True)

    is_earnings = df["feat_archetype"].str.lower().str.contains("earning", na=False)
    df_earn = df[is_earnings].copy()
    df_other = df[~is_earnings].copy()

    print(f"Total candidates: {len(df)}")
    print(f"  Earnings:     {len(df_earn)} ({len(df_earn)/len(df)*100:.0f}%)")
    print(f"  Non-earnings: {len(df_other)} ({len(df_other)/len(df)*100:.0f}%)")
    for sp in ("train", "val", "test"):
        ne = len(df_earn[df_earn.split == sp])
        no = len(df_other[df_other.split == sp])
        print(f"    {sp}: earn={ne}  other={no}")

    print("\nLoading price/prob paths + SPY/QQQ from DB...")
    P, PR = asyncio.run(load_paths(df))

    # ━━━━━━━━━━ SECTION 1: EARNINGS vs NON-EARNINGS BASELINE ━━━━━━━━━━

    for label, subset in [("ALL", df), ("EARNINGS ONLY", df_earn), ("NON-EARNINGS ONLY", df_other)]:
        print(f"\n{'='*75}")
        print(f"  BASELINE — {label} (default policy)")
        print(f"{'='*75}")
        tdf = run_backtest(subset, P, PR, DEFAULT_POLICY)
        if tdf.empty:
            print("  no trades")
            continue
        tdf = add_benchmarks(tdf, P)
        print_split_table(tdf)

        tdf.to_csv(PROJECT / "data" / f"trades_{label.lower().replace(' ','_')}.csv", index=False)

    # ━━━━━━━━━━ SECTION 2: CEM SHARPE on each subset ━━━━━━━━━━

    cem_sharpe = {'atr_mult': 3.205, 'lock_activate': 0.02, 'theta_out': 0.594,
                  'enter_strong': 0.709, 'enter_floor': 0.709, 'hold_days': 2,
                  'max_prob_surge': 0.237, 'max_price_runup': 0.03}

    for label, subset in [("ALL", df), ("EARNINGS ONLY", df_earn), ("NON-EARNINGS ONLY", df_other)]:
        print(f"\n{'='*75}")
        print(f"  CEM SHARPE POLICY — {label}")
        print(f"{'='*75}")
        tdf = run_backtest(subset, P, PR, cem_sharpe)
        if tdf.empty:
            print("  no trades")
            continue
        tdf = add_benchmarks(tdf, P)
        print_split_table(tdf)

    # ━━━━━━━━━━ SECTION 3: RF WITH REDUCED FEATURES ━━━━━━━━━━

    print(f"\n{'='*75}")
    print(f"  RF — LEAN MODEL (7 features, no categoricals)")
    print(f"  Features: {NUM_FEATURES_LEAN}")
    print(f"{'='*75}")

    for label, subset in [("ALL", df), ("EARNINGS ONLY", df_earn), ("NON-EARNINGS ONLY", df_other)]:
        train_sub = subset[subset["split"] == "train"]
        if len(train_sub) < 15:
            print(f"\n  {label}: too few train samples ({len(train_sub)})")
            continue

        pipe = SkPipeline([
            ("imp", SimpleImputer(strategy="median")),
            ("rf", RandomForestRegressor(
                n_estimators=100, max_depth=6, min_samples_leaf=15,
                random_state=42, n_jobs=-1)),
        ])
        X_train = train_sub[NUM_FEATURES_LEAN]
        y_train = train_sub[TARGET]
        pipe.fit(X_train, y_train)

        print(f"\n  {label} (train={len(train_sub)}):")
        for sp in ("train", "val", "test"):
            sp_df = subset[subset["split"] == sp]
            if sp_df.empty:
                continue
            preds = pd.Series(pipe.predict(sp_df[NUM_FEATURES_LEAN]), index=sp_df.index)
            actual = sp_df[TARGET]
            corr = float(actual.corr(preds)) if len(sp_df) > 2 else 0
            dir_acc = float(((preds > 0) == (actual > 0)).mean())
            print(f"    {sp}: n={len(sp_df):4d}  corr={corr:+.3f}  dir_acc={dir_acc:.1%}")

        preds_all = pd.Series(pipe.predict(subset[NUM_FEATURES_LEAN]), index=subset.index)
        subset_rf = subset[preds_all > 0].copy()
        tdf_rf = run_backtest(subset_rf, P, PR, DEFAULT_POLICY)
        if not tdf_rf.empty:
            tdf_rf = add_benchmarks(tdf_rf, P)
            print_split_table(tdf_rf, f"  RF>0 filtered — {label}")

    # ━━━━━━━━━━ SECTION 4: PORTFOLIO SIM $100K ━━━━━━━━━━

    print(f"\n{'='*75}")
    print(f"  PORTFOLIO SIMULATION — $100,000")
    print(f"{'='*75}")

    print("\n  Running portfolio CEM (train only)...")
    best_port_policy = portfolio_cem(df, P, PR, n_iter=8, pop_size=30, seed=42)

    for label, subset in [("ALL", df), ("NON-EARNINGS", df_other)]:
        print(f"\n  --- {label} with optimised portfolio policy ---")
        tdf_p, eq_p, stats = simulate_portfolio(subset, P, PR, best_port_policy)
        print(f"    Initial:      ${stats['initial']:>12,}")
        print(f"    Final:        ${stats['final']:>12,.2f}")
        print(f"    Total return: {stats['total_return_pct']:+.2f}%")
        print(f"    Max drawdown: {stats['max_drawdown_pct']:.2f}%")
        print(f"    Trades taken: {stats['n_trades']}")
        print(f"    Total P&L:    ${stats['total_pnl']:>12,.2f}")
        print(f"    Avg P&L/trade:${stats['avg_pnl']:>8,.2f}")
        print(f"    Win rate:     {stats['win_rate']:.1f}%")

        if not tdf_p.empty:
            tdf_p["holding_days"] = (pd.to_datetime(tdf_p["exit_date"]) - pd.to_datetime(tdf_p["entry_date"])).dt.days
            for sp in ("train", "val", "test"):
                s = tdf_p[tdf_p.split == sp]
                if s.empty:
                    continue
                print(f"    {sp}: n={len(s):3d}  avg_pnl=${s['pnl'].mean():+.2f}  "
                      f"win={((s['pnl']>0).mean()*100):.0f}%  hold={s['holding_days'].mean():.1f}d")

    # Also run with default policy for comparison
    print(f"\n  --- ALL with DEFAULT portfolio policy (10% per trade, max 10) ---")
    tdf_def, _, stats_def = simulate_portfolio(df, P, PR, PORTFOLIO_DEFAULT)
    print(f"    Final: ${stats_def['final']:>12,.2f}  return: {stats_def['total_return_pct']:+.2f}%  "
          f"DD: {stats_def['max_drawdown_pct']:.2f}%  trades: {stats_def['n_trades']}")

    # ━━━━━━━━━━ SECTION 5: DETAILED TRADE ANALYSIS ━━━━━━━━━━

    print(f"\n{'='*75}")
    print(f"  DETAILED TRADE ANALYSIS")
    print(f"{'='*75}")

    tdf_all = run_backtest(df, P, PR, DEFAULT_POLICY)
    tdf_all = add_benchmarks(tdf_all, P)

    # Holding days distribution
    print(f"\n  HOLDING DAYS DISTRIBUTION:")
    print(f"  {'bucket':8s} {'n':>5} {'mean_ret':>9} {'win%':>6} {'vs_SPY':>8} {'vs_QQQ':>8}")
    print(f"  {'-'*50}")
    bins = [0, 3, 7, 14, 21, 30, 999]
    labels_hd = ["0-3d", "4-7d", "8-14d", "15-21d", "22-30d", "30d+"]
    tdf_all["bucket"] = pd.cut(tdf_all["holding_days"], bins=bins, labels=labels_hd)
    for b in labels_hd:
        sub = tdf_all[tdf_all.bucket == b]
        if sub.empty:
            continue
        print(f"  {b:8s} {len(sub):5d} {sub.return_pct.mean():+9.2f}% {(sub.return_pct>0).mean()*100:5.0f}% "
              f"{sub.excess_spy.mean():+8.2f}% {sub.excess_qqq.mean():+8.2f}%")

    # Trades held 22+ days — FULL DETAIL
    long = tdf_all[tdf_all["holding_days"] >= 22].sort_values("holding_days", ascending=False)
    print(f"\n  TRADES HELD 22+ DAYS ({len(long)} trades):")
    if long.empty:
        print("    none")
    else:
        for _, t in long.iterrows():
            print(f"    {t['symbol']:6s}  {t['holding_days']:2.0f}d  ret={t['return_pct']:+7.2f}%  "
                  f"spy={t['spy_return']:+6.2f}%  excess={t['excess_spy']:+6.2f}%  "
                  f"exit={str(t['exit_reason'])[:22]:22s}  {t['split']:5s}  "
                  f"entry={str(t['entry_date'])[:10]}  {str(t.get('archetype',''))[:50]}")

    # Return outliers with full detail
    mean_r = tdf_all["return_pct"].mean()
    std_r = tdf_all["return_pct"].std()
    outliers = tdf_all[(tdf_all["return_pct"] > mean_r + 2 * std_r) |
                        (tdf_all["return_pct"] < mean_r - 2 * std_r)].copy()
    outliers = outliers.sort_values("return_pct")

    print(f"\n  RETURN OUTLIERS (>{mean_r + 2*std_r:.1f}% or <{mean_r - 2*std_r:.1f}%) — {len(outliers)} trades:")
    is_earn_out = outliers["archetype"].str.lower().str.contains("earning", na=False)
    print(f"    Earnings outliers: {is_earn_out.sum()}  |  Non-earnings: {(~is_earn_out).sum()}")

    print(f"\n    WORST 10:")
    for _, t in outliers.head(10).iterrows():
        print(f"      {t['symbol']:6s}  ret={t['return_pct']:+7.2f}%  hold={t['holding_days']:2.0f}d  "
              f"spy_ex={t['excess_spy']:+6.2f}%  exit={str(t['exit_reason'])[:18]:18s}  "
              f"{str(t.get('archetype',''))[:50]}")

    print(f"\n    BEST 10:")
    for _, t in outliers.tail(10).iterrows():
        print(f"      {t['symbol']:6s}  ret={t['return_pct']:+7.2f}%  hold={t['holding_days']:2.0f}d  "
              f"spy_ex={t['excess_spy']:+6.2f}%  exit={str(t['exit_reason'])[:18]:18s}  "
              f"{str(t.get('archetype',''))[:50]}")

    # Non-earnings archetype breakdown
    tdf_other_bt = tdf_all[~tdf_all["archetype"].str.lower().str.contains("earning", na=False)]
    print(f"\n  NON-EARNINGS ARCHETYPE BREAKDOWN ({len(tdf_other_bt)} trades):")
    arch_stats = tdf_other_bt.groupby("archetype").agg(
        n=("return_pct", "count"),
        mean_ret=("return_pct", "mean"),
        win_pct=("return_pct", lambda x: (x > 0).mean() * 100),
        excess_spy=("excess_spy", "mean"),
    ).sort_values("n", ascending=False).head(15)
    print(f"  {'archetype':50s} {'n':>4} {'mean':>8} {'win%':>6} {'vs_SPY':>8}")
    for arch, row in arch_stats.iterrows():
        print(f"  {str(arch)[:50]:50s} {row['n']:4.0f} {row['mean_ret']:+8.2f}% {row['win_pct']:5.0f}% "
              f"{row['excess_spy']:+8.2f}%")

    print(f"\n{'='*75}")
    print(f"  DONE")
    print(f"{'='*75}")


if __name__ == "__main__":
    main()
