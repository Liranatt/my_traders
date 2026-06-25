
from __future__ import annotations

import asyncio
import sys
from copy import deepcopy

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.compose import ColumnTransformer

from database.db_connection import connect
from database.backtesting.schema import SCHEMA

# ──────────── features ────────────
NUM = [
    "feat_prob_at_trigger", "feat_prob_slope_24h", "feat_prob_volatility", "feat_prob_surge_since_t0",
    "feat_time_to_resolution_days", "feat_crossing_latency_days", "feat_pre_entry_volume_log",
    "feat_runup_since_t0", "feat_asset_2w_trend", "feat_sector_1m_trend", "feat_spy_2w_trend", "feat_ytd_change",
    "feat_debt_to_equity", "feat_cash_to_marketcap", "feat_beta", "feat_profit_margin", "feat_log_market_cap",
    "feat_connection_strength", "feat_world_size", "feat_runup_rank", "feat_size_rank",
]
CAT = ["feat_archetype", "feat_sector"]
TARGET = "asset_return"            # raw % change, NO hedge
RELEVANCE = "feat_connection_strength"

# ──────────── default policy (baseline) ────────────
DEFAULT_POLICY = dict(
    atr_mult=2.5,              # ATR trailing stop multiplier
    lock_activate=0.03,        # profit-lock activation threshold
    theta_out=0.55,            # Polymarket exit
    enter_strong=0.75,         # instant entry threshold
    enter_floor=0.70,          # confirmation entry threshold
    hold_days=1,               # days to hold above enter_floor before entry
)

# ──────────── RL bounds (hard limits -- stop loss CANNOT be disabled) ────────────
RL_BOUNDS = dict(
    atr_mult=    (1.5, 4.0),      # ATR multiplier: 1.5x to 4.0x
    lock_activate=(0.02, 0.10),   # profit-lock activation: 2% to 10%
    theta_out=   (0.45, 0.60),    # Poly exit threshold
    enter_strong=(0.60, 0.85),    # instant-entry probability
    enter_floor= (0.55, 0.80),    # confirmation-entry probability
    hold_days=   (1, 5),          # confirmation days (integer)
)


def rf_model():
    pre = ColumnTransformer([
        ("num", SimpleImputer(strategy="median"), NUM),
        ("cat", Pipeline([("imp", SimpleImputer(strategy="constant", fill_value="?")),
                          ("oh", OneHotEncoder(handle_unknown="ignore", sparse_output=False))]), CAT),
    ])
    return Pipeline([("pre", pre), ("rf", RandomForestRegressor(
        n_estimators=300, max_depth=6, min_samples_leaf=8, random_state=42, n_jobs=4))])


async def price_prob_paths(df):
    """Daily close path per symbol and daily prob path per market, from t_theta..t_e."""
    c = await connect()
    try:
        syms = sorted(df["symbol"].unique())
        mkts = sorted(df["market_id"].unique())
        bars = await c.fetch(f"""SELECT symbol, ts, high, low, close FROM {SCHEMA}.historical_price_bars
            WHERE resolution='1d' AND symbol=ANY($1::text[]) ORDER BY symbol, ts""", syms)
        probs = await c.fetch(f"""SELECT DISTINCT ON (market_id,(hour_ts AT TIME ZONE 'UTC')::date)
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
            float(b["high"]), float(b["low"]), float(b["close"])
        ))
    PR = {}
    for p in probs:
        PR.setdefault(p["market_id"], []).append((pd.Timestamp(p["d"]).tz_localize("UTC"), float(p["probability"])))
    for d in (P, PR):
        for k in d:
            d[k].sort()
    return P, PR


def calc_atr(bars):
    """Calculate Average True Range from a list of (t, h, l, c) bars."""
    if len(bars) < 2:
        return 0.0
    trs = []
    for i in range(1, len(bars)):
        h, l, c = bars[i][1], bars[i][2], bars[i][3]
        pc = bars[i-1][3]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    return sum(trs) / len(trs)


def entry_day(prob_path, t_theta, policy):
    """Apply the entry rule; return (entry_ts, entry_prob) or None."""
    pts = [(t, v) for t, v in prob_path if t >= t_theta - pd.Timedelta(days=1)]
    if not pts:
        return None
    p0 = next((v for t, v in pts if t >= t_theta), pts[0][1])
    if p0 >= policy["enter_strong"]:
        return pts[0][0], p0
    held = 0
    for t, v in pts:
        if v >= policy["enter_floor"]:
            held += 1
            if held > policy["hold_days"]:
                return t, v
        else:
            held = 0
    return None


def simulate_one(row, P, PR, policy):
    """Simulate a single trade using the given policy dict."""
    sym, mkt = row["symbol"], row["market_id"]
    t_theta = pd.Timestamp(row["t_theta"]).tz_convert("UTC")
    t_e = pd.Timestamp(row["t_e"]).tz_convert("UTC")
    closes = P.get(sym, [])
    
    # Grab a wider window to calculate ATR before entry
    win = [(t, h, l, c) for t, h, l, c in closes if t_theta - pd.Timedelta(days=30) <= t <= t_e]
    if len(win) < 2:
        return None
        
    ent = entry_day(PR.get(mkt, []), t_theta, policy)
    if ent is None:
        return None
    entry_ts = ent[0]
    
    entry_idx = next((i for i, b in enumerate(win) if b[0] >= entry_ts), -1)
    if entry_idx == -1: return None
    path = win[entry_idx:]
    if len(path) < 2: return None
    
    # Calculate 14-day ATR up to the entry day
    hist_bars = win[max(0, entry_idx - 15) : entry_idx + 1]
    atr = calc_atr(hist_bars)
    
    entry_price = path[0][3]  # close on entry day
    if atr == 0 or entry_price == 0:
        return None
    atr_pct = atr / entry_price

    prob_path = {t.normalize(): v for t, v in PR.get(mkt, [])}
    resolution_cut = t_e - pd.Timedelta(days=1)
    target = float(row["rf_target"])
    
    atr_mult = policy["atr_mult"]
    lock_activate = policy["lock_activate"]
    theta_out = policy["theta_out"]
    peak = 0.0
    
    for i, (t, h, l, c) in enumerate(path):
        ret_c = c / entry_price - 1.0
        ret_h = h / entry_price - 1.0
        ret_l = l / entry_price - 1.0
        
        reason = None
        if i > 0:
            stop_dist = atr_mult * atr_pct
            
            # 1. Did the low violate the trailing stop from previous peak?
            if ret_l <= peak - stop_dist:
                reason = f"trailing_{atr_mult:.1f}ATR"
                c = max(l, entry_price * (1.0 + peak - stop_dist))
                ret_c = c / entry_price - 1.0
                
            # 2. Or did the low violate the profit lock from previous peak?
            elif peak >= lock_activate:
                hard_floor_pct = int(peak * 100)
                hard_floor = hard_floor_pct / 100.0
                if ret_l < hard_floor:
                    reason = f"profit_lock_{hard_floor_pct}%"
                    c = max(l, entry_price * (1.0 + hard_floor))
                    ret_c = c / entry_price - 1.0
            
            # 3. EOD exits
            if reason is None:
                if ret_c >= target and target > 0:
                    reason = "rf_target"
                elif prob_path.get(t.normalize(), 1.0) < theta_out:
                    reason = f"poly<{theta_out}"
                elif t >= resolution_cut:
                    reason = "resolution-1d"
                    
        if reason:
            lo = min(ll / entry_price - 1.0 for _, _, ll, _ in path[: i + 1])
            return dict(market_id=mkt, symbol=sym, archetype=row["feat_archetype"],
                        relevance=round(float(row[RELEVANCE]), 3), split=row["split"],
                        entry_date=str(entry_ts.date()), entry_prob=round(ent[1], 3),
                        entry_price=round(entry_price, 2), exit_date=str(t.date()),
                        exit_price=round(c, 2), exit_reason=reason, rf_target=round(target, 4),
                        peak_pct=round(peak * 100, 2), trough_pct=round(lo * 100, 2),
                        return_pct=round(ret_c * 100, 2))

        # Update peak AFTER checking stops (prevent intraday lookahead bias)
        if i == 0:
            peak = 0.0  # entry is at the close
        else:
            peak = max(peak, ret_h)
    t, h, l, c = path[-1]
    ret_c = c / entry_price - 1.0
    lo = min(ll / entry_price - 1.0 for _, _, ll, _ in path)
    return dict(market_id=mkt, symbol=sym, archetype=row["feat_archetype"],
                relevance=round(float(row[RELEVANCE]), 3), split=row["split"],
                entry_date=str(entry_ts.date()), entry_prob=round(ent[1], 3),
                entry_price=round(entry_price, 2), exit_date=str(t.date()),
                exit_price=round(c, 2), exit_reason="end_of_window", rf_target=round(target, 4),
                peak_pct=round(peak * 100, 2), trough_pct=round(lo * 100, 2),
                return_pct=round(ret_c * 100, 2))


def run_backtest(df, P, PR, policy, split_filter=None):
    """Run the full backtest for a given policy, optionally filtering to a split."""
    subset = df if split_filter is None else df[df["split"] == split_filter]
    trades = [t for t in (simulate_one(r, P, PR, policy) for _, r in subset.iterrows()) if t]
    return pd.DataFrame(trades) if trades else pd.DataFrame()


def score_mean_return(tdf):
    """Reward = mean return % on the trades."""
    if tdf.empty:
        return -999.0
    return float(tdf["return_pct"].mean())


def score_sharpe_per_day(tdf):
    """Reward = annualised Sharpe estimated from per-trade daily returns.
    Each trade contributes ret / holding_days as a daily return sample."""
    if len(tdf) < 3:
        return -999.0
    entry = pd.to_datetime(tdf["entry_date"])
    exit_ = pd.to_datetime(tdf["exit_date"])
    days = (exit_ - entry).dt.days.clip(lower=1)
    daily_ret = tdf["return_pct"].values / days.values     # % per day
    mu = daily_ret.mean()
    sigma = daily_ret.std()
    if sigma < 1e-9:
        return -999.0
    return float(mu / sigma * np.sqrt(252))                # annualised


def policy_from_vector(vec):
    """Convert a numpy array [trail, lock_activate, theta_out, enter_strong, enter_floor, hold_days]
    into a clipped policy dict. All values are hard-bounded so stops can NEVER be disabled."""
    names = list(RL_BOUNDS.keys())
    p = {}
    for i, name in enumerate(names):
        lo, hi = RL_BOUNDS[name]
        p[name] = float(np.clip(vec[i], lo, hi))
    p["hold_days"] = int(round(p["hold_days"]))
    # enforce: enter_strong >= enter_floor (otherwise entry logic breaks)
    if p["enter_strong"] < p["enter_floor"]:
        p["enter_strong"] = p["enter_floor"]
    return p


def cem_search(df, P, PR, reward_fn, label, n_iter=10, pop_size=40, elite_frac=0.25, seed=42):
    """Cross-Entropy Method search over the 6 policy knobs.
    Trains on 'train' split ONLY. Reports val/test at the end."""
    rng = np.random.default_rng(seed)
    names = list(RL_BOUNDS.keys())
    dim = len(names)
    elite_k = max(2, int(pop_size * elite_frac))

    # initialise distribution from DEFAULT_POLICY
    mean = np.array([DEFAULT_POLICY[n] for n in names], dtype=float)
    std = np.array([(RL_BOUNDS[n][1] - RL_BOUNDS[n][0]) / 4.0 for n in names], dtype=float)

    print(f"\n{'='*70}")
    print(f"  RL/CEM EXPERIMENT: {label}")
    print(f"  Reward function: {reward_fn.__name__}")
    print(f"  Population: {pop_size}  |  Elite: {elite_k}  |  Iterations: {n_iter}")
    print(f"  Parameter bounds (stop loss CANNOT be disabled):")
    for n in names:
        print(f"    {n:15s}: [{RL_BOUNDS[n][0]:.3f}, {RL_BOUNDS[n][1]:.3f}]  (start={DEFAULT_POLICY[n]:.3f})")
    print(f"{'='*70}\n")

    best_score = -999.0
    best_policy = None

    for it in range(n_iter):
        # sample & clip
        samples = rng.normal(mean, std, size=(pop_size, dim))
        policies = [policy_from_vector(s) for s in samples]

        # evaluate on TRAIN only
        scores = []
        for p in policies:
            tdf = run_backtest(df, P, PR, p, split_filter="train")
            scores.append(reward_fn(tdf))
        scores = np.array(scores)

        # select elites
        elite_idx = np.argsort(scores)[-elite_k:]
        elite = samples[elite_idx]
        mean = elite.mean(axis=0)
        std = elite.std(axis=0) + 1e-4   # prevent collapse

        it_best = scores.max()
        if it_best > best_score:
            best_score = it_best
            best_policy = policies[elite_idx[-1]]

        p_str = "  ".join(f"{n}={mean[i]:.3f}" for i, n in enumerate(names))
        print(f"  iter {it:2d}/{n_iter}  best_train={it_best:+.3f}  mean_params: {p_str}")

    print(f"\n  CONVERGED POLICY: {best_policy}")
    print(f"  Best train {reward_fn.__name__}: {best_score:+.3f}\n")

    # ── final evaluation on all splits ──
    print(f"  {'split':5s} {'n':>5s} {'mean_ret':>10s} {'win%':>6s} {'median':>8s} {'reward':>10s}")
    print(f"  {'-'*50}")
    for sp in ("train", "val", "test"):
        tdf = run_backtest(df, P, PR, best_policy, split_filter=sp)
        if tdf.empty:
            print(f"  {sp:5s}     0  (no trades)")
            continue
        n = len(tdf)
        mr = tdf.return_pct.mean()
        wr = (tdf.return_pct > 0).mean() * 100
        med = tdf.return_pct.median()
        rw = reward_fn(tdf)
        print(f"  {sp:5s} {n:5d} {mr:+10.2f}% {wr:5.0f}% {med:+8.2f}% {rw:+10.3f}")
    print(f"\n  exit reasons: {run_backtest(df, P, PR, best_policy).exit_reason.value_counts().to_dict()}")
    print(f"{'='*70}\n")

    return best_policy


def print_results(tdf):
    """Print the standard summary table."""
    for sp in ("train", "val", "test"):
        s = tdf[tdf.split == sp]
        if len(s):
            print(f"  {sp:5} n={len(s):4}  mean_return={s.return_pct.mean():+.2f}%  "
                  f"win%={(s.return_pct>0).mean()*100:3.0f}%  median={s.return_pct.median():+.2f}%")
    print(f"\n  exit reasons: {tdf.exit_reason.value_counts().to_dict()}")


# ──────────── NULL / MARKET BASELINES (is the edge real, or just beta?) ────────────
async def load_spy():
    c = await connect()
    try:
        bars = await c.fetch(f"SELECT ts, close FROM {SCHEMA}.historical_price_bars "
                             f"WHERE symbol='SPY' AND resolution='1d' ORDER BY ts")
    finally:
        await c.close()
    items = sorted((pd.Timestamp(b["ts"]).tz_convert("UTC").normalize(), float(b["close"])) for b in bars)
    return items


def spy_return(spy, entry_date, exit_date):
    """SPY return over the SAME holding window as a trade (first close >= each date)."""
    e = pd.Timestamp(entry_date, tz="UTC"); x = pd.Timestamp(exit_date, tz="UTC")
    pe = next((c for t, c in spy if t >= e), None)
    px = next((c for t, c in spy if t >= x), None)
    return (px / pe - 1.0) if (pe and px and pe > 0) else None


def buy_hold_trades(df, P, PR):
    """Same ENTRY, but disable every fancy exit -> just long-and-hold to resolution-1d."""
    bh = df.copy(); bh["rf_target"] = 999.0
    pol = {**DEFAULT_POLICY, "atr_mult": 999.0, "lock_activate": 999.0, "theta_out": 0.0}
    return run_backtest(bh, P, PR, pol)


def hold_with_stop(df, P, PR, stop_pct):
    """Just hold to resolution, but exit if the stock drops `stop_pct` below entry (hard stop)."""
    rows = []
    for _, row in df.iterrows():
        sym, mkt = row["symbol"], row["market_id"]
        t_theta = pd.Timestamp(row["t_theta"]).tz_convert("UTC")
        t_e = pd.Timestamp(row["t_e"]).tz_convert("UTC")
        win = [b for b in P.get(sym, []) if t_theta - pd.Timedelta(days=2) <= b[0] <= t_e]
        if len(win) < 2:
            continue
        ent = entry_day(PR.get(mkt, []), t_theta, DEFAULT_POLICY)
        if ent is None:
            continue
        path = [b for b in win if b[0] >= ent[0]]
        if len(path) < 2 or path[0][3] <= 0:
            continue
        entry_price = path[0][3]
        resolution_cut = t_e - pd.Timedelta(days=1)
        ret, reason = path[-1][3] / entry_price - 1.0, "resolution-1d"
        for i, (t, h, l, c) in enumerate(path):
            if i > 0 and (l / entry_price - 1.0) <= -stop_pct:    # hard stop hit intraday
                ret, reason = -stop_pct, f"stop_{stop_pct*100:.0f}%"
                break
            if t >= resolution_cut:
                ret, reason = c / entry_price - 1.0, "resolution-1d"
                break
        rows.append(dict(split=row["split"], return_pct=round(ret * 100, 2), exit_reason=reason))
    return pd.DataFrame(rows)


def hold_with_stop_report(df, P, PR):
    print("\n" + "=" * 78)
    print("  JUST HOLD TO RESOLUTION + HARD STOP LOSS (no take-profit, no trailing)")
    print("=" * 78)
    print(f"  {'stop':>6} {'split':5} {'n':>5} {'mean_ret':>9} {'win%':>6} {'median':>8} {'stopped%':>9}")
    for stop in (0.05, 0.07, 0.10, 0.15, 1.0):  # 1.0 = no stop (pure hold)
        t = hold_with_stop(df, P, PR, stop)
        label = "none" if stop >= 1.0 else f"{stop*100:.0f}%"
        for sp in ("train", "val", "test"):
            s = t[t.split == sp]
            if len(s):
                stopped = s.exit_reason.str.startswith("stop").mean() * 100
                print(f"  {label:>6} {sp:5} {len(s):5d} {s.return_pct.mean():+8.2f}% "
                      f"{(s.return_pct>0).mean()*100:5.0f}% {s.return_pct.median():+8.2f}% {stopped:8.0f}%")
        print()


def compare_baselines(df, P, PR, spy, policy=DEFAULT_POLICY):
    strat = run_backtest(df, P, PR, policy)
    bh = buy_hold_trades(df, P, PR)
    print("\n" + "=" * 78)
    print("  BASELINE COMPARISON  (mean return % per trade, by split)")
    print("  strategy = your full rules | buy&hold = same entry, exit only at resolution")
    print("  SPY = market over the SAME windows | excess = strategy - SPY (real edge if > 0)")
    print("=" * 78)
    print(f"  {'split':5} {'strategy':>22} {'buy&hold':>12} {'SPY(same win)':>14} {'excess vs SPY':>14}")
    for sp in ("train", "val", "test"):
        s = strat[strat.split == sp]; b = bh[bh.split == sp]
        if not len(s):
            continue
        spy_r = [r for r in (spy_return(spy, t.entry_date, t.exit_date) for t in s.itertuples()) if r is not None]
        spy_m = np.mean(spy_r) * 100 if spy_r else float("nan")
        sm = s.return_pct.mean(); bm = b.return_pct.mean() if len(b) else float("nan")
        print(f"  {sp:5} {sm:+7.2f}% (n={len(s):3}, win {(s.return_pct>0).mean()*100:3.0f}%) "
              f"{bm:+10.2f}% {spy_m:+13.2f}% {sm - spy_m:+13.2f}%")
    print("=" * 78)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run_rl = "--rl" in sys.argv
    path = next((a for a in sys.argv[1:] if not a.startswith("--")), "data/candidates.parquet")

    df = pd.read_parquet(path)
    df = df[df[RELEVANCE].astype(float) > 0.5].copy()
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True)
    df["t_e"] = pd.to_datetime(df["t_e"], utc=True)
    tr = df[df["split"] == "train"]
    print(f"universe (relevance>0.5): {len(df)}  | train={len(tr)} val={(df.split=='val').sum()} test={(df.split=='test').sum()}")

    model = rf_model()
    model.fit(tr[NUM + CAT], tr[TARGET].astype(float))
    df["rf_target"] = model.predict(df[NUM + CAT])

    P, PR = asyncio.run(price_prob_paths(df))

    # ── baseline run ──
    print("\n" + "="*70)
    print("  BASELINE (hardcoded policy)")
    print("="*70)
    tdf = run_backtest(df, P, PR, DEFAULT_POLICY)
    out_csv = "data/liran_trades_baseline.csv" if run_rl else "data/liran_trades.csv"
    tdf.to_csv(out_csv, index=False)
    print(f"trades taken: {len(tdf)}  -> {out_csv}\n")
    print_results(tdf)

    # ── null / market baselines: is it real edge or just beta? ──
    spy = asyncio.run(load_spy())
    compare_baselines(df, P, PR, spy)
    hold_with_stop_report(df, P, PR)

    if not run_rl:
        return

    # ── RL experiments ──
    print("\n\n" + "#"*70)
    print("#  RUNNING RL / CEM EXPERIMENTS")
    print("#  The RL will search for optimal policy on TRAIN only,")
    print("#  then report Val and Test to check if it survives OOS.")
    print("#  Stop loss has a HARD minimum of 3% -- it can NEVER be disabled.")
    print("#"*70)

    # Experiment 1: Maximize Mean Return
    policy_mr = cem_search(df, P, PR, score_mean_return,
                           label="Maximize Mean Return", n_iter=10, pop_size=40)

    # Experiment 2: Maximize Per-Day Sharpe Ratio
    policy_sh = cem_search(df, P, PR, score_sharpe_per_day,
                           label="Maximize Per-Day Sharpe Ratio", n_iter=10, pop_size=40)

    # ── save both sets of trades ──
    tdf_mr = run_backtest(df, P, PR, policy_mr)
    tdf_mr.to_csv("data/liran_trades_rl_meanret.csv", index=False)
    print(f"Mean-return RL trades saved: data/liran_trades_rl_meanret.csv ({len(tdf_mr)} trades)")

    tdf_sh = run_backtest(df, P, PR, policy_sh)
    tdf_sh.to_csv("data/liran_trades_rl_sharpe.csv", index=False)
    print(f"Sharpe RL trades saved: data/liran_trades_rl_sharpe.csv ({len(tdf_sh)} trades)")


if __name__ == "__main__":
    main()

