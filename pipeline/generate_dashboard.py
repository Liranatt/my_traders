"""Generate interactive HTML dashboard for all backtest experiments."""
from __future__ import annotations

import asyncio, json, re, sys, webbrowser
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from pipeline.strategy import (
    DEFAULT_POLICY, run_backtest, simulate_one,
    score_sharpe_per_day,
)
from pipeline.data_loader import NUM_FEATURES_LEAN, TARGET
from sklearn.ensemble import RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline as SkPipeline

PROJECT = Path(__file__).resolve().parents[1]
REL_COL = "feat_connection_strength"

CEM_SHARPE = {'atr_mult': 3.205, 'lock_activate': 0.02, 'theta_out': 0.594,
              'enter_strong': 0.709, 'enter_floor': 0.709, 'hold_days': 2,
              'max_prob_surge': 0.237, 'max_price_runup': 0.03}

# ── helpers ──────────────────────────────────────────────────────────

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


def bench_ret(prices, sym, entry_date, exit_date):
    e = pd.Timestamp(entry_date, tz="UTC")
    x = pd.Timestamp(exit_date, tz="UTC")
    bars = prices.get(sym, [])
    pe = next((c for t, h, l, c in bars if t >= e), None)
    px = next((c for t, h, l, c in bars if t >= x), None)
    if pe and px and pe > 0:
        return round((px / pe - 1.0) * 100, 4)
    return 0.0


def enrich(tdf, prices):
    if tdf.empty:
        return tdf
    tdf = tdf.copy()
    tdf["spy_return"] = tdf.apply(lambda r: bench_ret(prices, "SPY", r["entry_date"], r["exit_date"]), axis=1)
    tdf["qqq_return"] = tdf.apply(lambda r: bench_ret(prices, "QQQ", r["entry_date"], r["exit_date"]), axis=1)
    tdf["excess_spy"] = tdf["return_pct"] - tdf["spy_return"]
    tdf["excess_qqq"] = tdf["return_pct"] - tdf["qqq_return"]
    tdf["holding_days"] = (pd.to_datetime(tdf["exit_date"]) - pd.to_datetime(tdf["entry_date"])).dt.days
    return tdf


def metrics(tdf):
    if tdf.empty:
        return {"n": 0}
    rets = tdf["return_pct"].values
    entry = pd.to_datetime(tdf["entry_date"])
    exit_ = pd.to_datetime(tdf["exit_date"])
    days = (exit_ - entry).dt.days.clip(lower=1).values
    daily_ret = rets / days
    wins = rets[rets > 0]
    losses = rets[rets < 0]
    downside = daily_ret[daily_ret < 0]
    downside_std = np.sqrt(np.mean(downside ** 2)) if len(downside) > 0 else 1e-9
    mu = daily_ret.mean()
    sigma = daily_ret.std()
    sharpe = float(mu / sigma * np.sqrt(252)) if sigma > 1e-9 else 0
    sortino = float(mu / downside_std * np.sqrt(252)) if downside_std > 1e-9 else 0
    cum = np.cumprod(1 + rets / 100)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / peak * 100
    max_dd = float(dd.min())
    ann_ret = (cum[-1] ** (252 / max(sum(days), 1)) - 1) * 100 if len(cum) > 0 else 0
    calmar = float(ann_ret / abs(max_dd)) if abs(max_dd) > 0.01 else 0
    pf = float(wins.sum() / abs(losses.sum())) if len(losses) > 0 and abs(losses.sum()) > 0 else 999
    var_95 = float(np.percentile(rets, 5)) if len(rets) >= 5 else 0
    below_var = rets[rets <= var_95]
    cvar_95 = float(below_var.mean()) if len(below_var) > 0 else var_95
    return {
        "n": int(len(rets)),
        "mean_ret": round(float(rets.mean()), 2),
        "median_ret": round(float(np.median(rets)), 2),
        "std_ret": round(float(rets.std()), 2),
        "win_pct": round(float((rets > 0).mean() * 100), 1),
        "sharpe": round(sharpe, 2),
        "sortino": round(sortino, 2),
        "max_dd": round(max_dd, 2),
        "calmar": round(calmar, 2),
        "profit_factor": round(pf, 2),
        "avg_win": round(float(wins.mean()), 2) if len(wins) else 0,
        "avg_loss": round(float(losses.mean()), 2) if len(losses) else 0,
        "max_win": round(float(wins.max()), 2) if len(wins) else 0,
        "max_loss": round(float(losses.min()), 2) if len(losses) else 0,
        "var_95": round(var_95, 2),
        "cvar_95": round(cvar_95, 2),
        "avg_hold": round(float(days.mean()), 1),
        "excess_spy": round(float(tdf["excess_spy"].mean()), 2) if "excess_spy" in tdf else 0,
        "excess_qqq": round(float(tdf["excess_qqq"].mean()), 2) if "excess_qqq" in tdf else 0,
    }


def split_metrics(tdf):
    out = {}
    for sp in ("train", "val", "test"):
        s = tdf[tdf["split"] == sp] if not tdf.empty and "split" in tdf.columns else pd.DataFrame()
        out[sp] = metrics(s)
    out["all"] = metrics(tdf)
    return out


def exit_counts(tdf):
    if tdf.empty:
        return {}
    raw = tdf["exit_reason"].value_counts().to_dict()
    grouped = {}
    for k, v in raw.items():
        key = k.split("_")[0] if "_" in k else (k.split("<")[0] if "<" in k else k)
        grouped[key] = grouped.get(key, 0) + v
    return grouped


def trades_to_list(tdf):
    if tdf.empty:
        return []
    cols = ["symbol", "market_id", "archetype", "split", "entry_date", "exit_date",
            "entry_price", "exit_price", "return_pct", "exit_reason",
            "holding_days", "spy_return", "qqq_return", "excess_spy", "excess_qqq",
            "relevance", "peak_pct", "trough_pct", "entry_prob"]
    out = []
    for _, r in tdf.iterrows():
        row = {}
        for c in cols:
            v = r.get(c, "")
            if isinstance(v, (np.floating, float)):
                row[c] = round(float(v), 2) if not np.isnan(v) else 0
            elif isinstance(v, (np.integer, int)):
                row[c] = int(v)
            else:
                row[c] = str(v)
        out.append(row)
    return out


# ── portfolio sim ──────────────────────────────────────────────────

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


def ib_cost(shares: int, price: float, is_sell: bool) -> float:
    """IB tiered commission + SEC fee on sells + 5 bp slippage."""
    if shares <= 0 or price <= 0:
        return 0.0
    trade_val = shares * price
    commission = max(0.35, min(shares * 0.0035, trade_val * 0.01))
    sec = trade_val * 0.0000278 if is_sell else 0.0
    slippage = trade_val * 0.0005
    return commission + sec + slippage


def _bench_close_on(prices, sym, date):
    """Return the close price of *sym* on or just before *date*."""
    bars = prices.get(sym, [])
    c = None
    for t, _h, _l, cl in bars:
        if t <= date:
            c = cl
        else:
            break
    return c


def _asset_close_on(prices, sym, date):
    """Return close of *sym* on or just before *date*."""
    return _bench_close_on(prices, sym, date)


def sim_portfolio_opp_cost(df, prices, probs, policy,
                           bench_sym="SPY", initial=100_000):
    """Portfolio sim where idle capital stays invested in *bench_sym*.

    On trade entry: sell benchmark shares → buy asset.
    On trade exit:  sell asset → buy benchmark shares back.
    All four legs incur IB costs + slippage.
    Returns (tdf, daily_eq_log, stats, policy).
    """
    ps = policy.get("position_size_pct", 0.10)
    mc = policy.get("max_concurrent", 10)

    empty_stats = {"initial": initial, "final": initial, "total_return": 0,
                    "max_dd": 0, "n_trades": 0, "total_pnl": 0,
                    "avg_pnl": 0, "win_rate": 0, "total_txn_cost": 0,
                    "bench_sym": bench_sym}
    if df.empty:
        return pd.DataFrame(), [], empty_stats, policy

    # 1. Pre-compute all candidate trades
    df_s = df.sort_values("t_theta").copy()
    all_trades = []
    for _, row in df_s.iterrows():
        trade = simulate_one(row, prices, probs, policy)
        if trade is not None:
            trade["_entry_ts"] = pd.Timestamp(trade["entry_date"], tz="UTC")
            trade["_exit_ts"] = pd.Timestamp(trade["exit_date"], tz="UTC")
            all_trades.append(trade)
    all_trades.sort(key=lambda t: t["_entry_ts"])

    # 2. Build trading-day calendar from benchmark bars
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
        return pd.DataFrame(), [], {"initial": initial, "final": initial,
            "total_return": 0, "max_dd": 0, "n_trades": 0, "total_pnl": 0,
            "avg_pnl": 0, "win_rate": 0, "total_txn_cost": 0}, policy

    # 3. Buy benchmark at start
    first_bench_close = _bench_close_on(prices, bench_sym, cal_dates[0])
    if first_bench_close is None or first_bench_close <= 0:
        first_bench_close = bench_bars[0][3]
    bench_shares = int(initial / first_bench_close)
    cash = initial - bench_shares * first_bench_close
    buy_cost_init = ib_cost(bench_shares, first_bench_close, False)
    cash -= buy_cost_init
    total_txn = buy_cost_init

    open_pos = []       # {trade dict + qty, cost, sym_shares}
    completed = []
    eq_log = []
    trade_idx = 0       # pointer into all_trades

    for day in cal_dates:
        bc = _bench_close_on(prices, bench_sym, day)
        if bc is None:
            continue

        # 4a. Close positions whose exit_date <= today
        still_open = []
        for pos in open_pos:
            if pos["_exit_ts"] <= day:
                sym = pos["symbol"]
                xp = pos["exit_price"]
                qty = pos["_qty"]
                sell_cost = ib_cost(qty, xp, True)
                net_proceeds = qty * xp - sell_cost
                total_txn += sell_cost
                # Buy benchmark back
                rebuy_shares = int(net_proceeds / bc) if bc > 0 else 0
                rebuy_cost = ib_cost(rebuy_shares, bc, False)
                total_txn += rebuy_cost
                cash += net_proceeds - rebuy_shares * bc - rebuy_cost
                bench_shares += rebuy_shares
                # Record completed trade
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

        # 4b. Open new positions if available and under capacity
        while trade_idx < len(all_trades):
            trade = all_trades[trade_idx]
            if trade["_entry_ts"] > day:
                break
            trade_idx += 1
            if trade["_entry_ts"] < day:
                continue  # missed entry date
            if len(open_pos) >= mc:
                continue

            total_eq = bench_shares * bc + sum(
                p["_qty"] * (_asset_close_on(prices, p["symbol"], day) or p["entry_price"])
                for p in open_pos) + cash
            alloc = total_eq * ps
            ep = trade["entry_price"]
            if ep <= 0 or alloc < ep:
                continue

            # Sell benchmark to fund
            sell_bench_qty = min(int(alloc / bc), bench_shares) if bc > 0 else 0
            if sell_bench_qty < 1:
                continue
            sell_bench_cost = ib_cost(sell_bench_qty, bc, True)
            total_txn += sell_bench_cost
            net_from_bench = sell_bench_qty * bc - sell_bench_cost
            bench_shares -= sell_bench_qty

            # Buy alpha asset
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

        # 4c. Log daily equity
        open_val = sum(
            p["_qty"] * (_asset_close_on(prices, p["symbol"], day) or p["entry_price"])
            for p in open_pos)
        equity = bench_shares * bc + open_val + cash
        eq_log.append({"date": str(day.date()), "equity": round(equity, 2),
                       "positions": len(open_pos),
                       "bench_shares": bench_shares,
                       "capital": round(cash, 2)})

    # 5. Force-close any remaining open positions
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


def sim_portfolio(df, prices, probs, policy, initial=100_000):
    df_s = df.sort_values("t_theta").copy()
    capital = initial
    open_pos, completed, eq_log = [], [], []
    ps = policy.get("position_size_pct", 0.10)
    mc = policy.get("max_concurrent", 10)

    for _, row in df_s.iterrows():
        t_th = pd.Timestamp(row["t_theta"]).tz_convert("UTC")
        closed = [p for p in open_pos if pd.Timestamp(p["exit_date"], tz="UTC") <= t_th]
        for p in closed:
            capital += p["exit_value"]
            completed.append(p)
            open_pos.remove(p)

        if len(open_pos) >= mc:
            continue
        trade = simulate_one(row, prices, probs, policy)
        if trade is None:
            continue
        alloc = capital * ps
        ep = trade["entry_price"]
        if ep <= 0 or alloc < ep:
            continue
        qty = int(alloc / ep)
        if qty < 1:
            continue
        cost = qty * ep
        xp = trade["exit_price"]
        xv = qty * xp
        pnl = xv - cost
        capital -= cost
        pos = {**trade, "qty": qty, "cost": round(cost, 2), "exit_value": round(xv, 2),
               "pnl": round(pnl, 2), "pnl_pct": round(pnl / cost * 100, 2)}
        open_pos.append(pos)
        ov = sum(p["cost"] * (1 + p["return_pct"] / 100) for p in open_pos)
        eq_log.append({"date": str(t_th.date()), "equity": round(capital + ov, 2),
                       "positions": len(open_pos), "capital": round(capital, 2)})

    for p in open_pos:
        capital += p["exit_value"]
        completed.append(p)

    tdf = pd.DataFrame(completed) if completed else pd.DataFrame()
    eq = eq_log
    final = capital
    tot_ret = (final / initial - 1) * 100
    if eq:
        ev = [e["equity"] for e in eq]
        pk = np.maximum.accumulate(ev)
        dd = [(v - p) / p * 100 for v, p in zip(ev, pk)]
        max_dd = min(dd)
    else:
        max_dd = 0

    stats = {
        "initial": initial, "final": round(final, 2),
        "total_return": round(tot_ret, 2), "max_dd": round(max_dd, 2),
        "n_trades": len(completed),
        "total_pnl": round(sum(t["pnl"] for t in completed), 2) if completed else 0,
        "avg_pnl": round(float(np.mean([t["pnl"] for t in completed])), 2) if completed else 0,
        "win_rate": round(float(np.mean([t["pnl"] > 0 for t in completed]) * 100), 1) if completed else 0,
    }
    return tdf, eq, stats, policy


def port_cem(df, prices, probs, dd_weight=0.3, n_iter=8, pop=30, seed=42):
    rng = np.random.default_rng(seed)
    names = list(PORTFOLIO_BOUNDS.keys())
    dim = len(names)
    ek = max(2, int(pop * 0.25))
    mean = np.array([PORT_DEFAULT[n] for n in names], float)
    std = np.array([(PORTFOLIO_BOUNDS[n][1] - PORTFOLIO_BOUNDS[n][0]) / 4 for n in names], float)
    df_tr = df[df["split"] == "train"]
    best_s, best_p = -999, None
    for it in range(n_iter):
        samps = rng.normal(mean, std, size=(pop, dim))
        pols = [port_policy_from_vec(s) for s in samps]
        scores = []
        for p in pols:
            t, _, st, _ = sim_portfolio(df_tr, prices, probs, p)
            if t.empty or st["n_trades"] < 5:
                scores.append(-999)
                continue
            sh = score_sharpe_per_day(t)
            scores.append(sh - dd_weight * abs(st["max_dd"]))
        scores = np.array(scores)
        ei = np.argsort(scores)[-ek:]
        elite = samps[ei]
        mean = elite.mean(0)
        std = elite.std(0) + 1e-4
        ib = scores.max()
        if ib > best_s:
            best_s = ib
            best_p = pols[ei[-1]]
        print(f"  CEM(dd_w={dd_weight}) iter {it}/{n_iter} best={ib:+.3f}")
    return best_p


def port_cem_opp_cost(df, prices, probs, bench_sym="SPY",
                      reward_mode="sharpe", n_iter=8, pop=30, seed=42):
    """CEM search over portfolio knobs using the opportunity-cost sim."""
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
            else:  # sharpe
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
        print(f"  CEM-OC({bench_sym},{reward_mode}) iter {it}/{n_iter} best={ib:+.3f}")
    return best_p


# ── new helpers ────────────────────────────────────────────────────

def parse_experiment_log(filepath: Path) -> list[dict]:
    """Parse EXPERIMENTS_LOG.md into structured phases/experiments."""
    text = filepath.read_text(encoding="utf-8")
    phases = []
    current_phase = None
    current_exp = None

    for line in text.split("\n"):
        line = line.rstrip()
        # Phase heading
        m = re.match(r"^## (Phase \d+.*)$", line)
        if m:
            if current_exp and current_phase:
                current_phase["experiments"].append(current_exp)
                current_exp = None
            if current_phase:
                phases.append(current_phase)
            current_phase = {"name": m.group(1).strip(), "experiments": []}
            continue
        # Experiment heading
        m = re.match(r"^### (E[\d.]+)\s+(.*)$", line)
        if m:
            if current_exp and current_phase:
                current_phase["experiments"].append(current_exp)
            current_exp = {"id": m.group(1), "title": m.group(2).strip(), "body": []}
            continue
        # Body lines
        if current_exp is not None:
            current_exp["body"].append(line)

    if current_exp and current_phase:
        current_phase["experiments"].append(current_exp)
    if current_phase:
        phases.append(current_phase)

    # Clean up body: join and extract key fields
    for phase in phases:
        for exp in phase["experiments"]:
            body = "\n".join(exp["body"]).strip()
            exp["body_text"] = body
            # Extract labeled fields
            for field in ("What", "Why", "Result", "Learned", "Why it failed",
                          "Why it worked", "Data", "Changed from", "Converged Policy"):
                m = re.search(rf"- \*\*{field}[:\*]*\*\*\s*(.*?)(?=\n- \*\*|\Z)",
                              body, re.DOTALL)
                if m:
                    exp[field.lower().replace(" ", "_")] = m.group(1).strip()
            del exp["body"]  # remove raw lines
    return phases


def extract_trade_paths(trades_list, prices, probs_dict, limit=200):
    """For up to `limit` trades, extract daily price path + SPY/QQQ + probability."""
    paths = {}
    for i, t in enumerate(trades_list[:limit]):
        sym = t["symbol"]
        mkt = t["market_id"]
        ed = pd.Timestamp(t["entry_date"], tz="UTC")
        xd = pd.Timestamp(t["exit_date"], tz="UTC")
        ep = t["entry_price"]
        if ep <= 0:
            continue

        sym_bars = prices.get(sym, [])
        spy_bars = prices.get("SPY", [])
        qqq_bars = prices.get("QQQ", [])
        prob_pts = probs_dict.get(mkt, [])

        path = []
        spy_e = next((c for tt, h, l, c in spy_bars if tt >= ed), None)
        qqq_e = next((c for tt, h, l, c in qqq_bars if tt >= ed), None)

        for tt, h, l, c in sym_bars:
            if tt < ed or tt > xd + pd.Timedelta(days=1):
                continue
            spy_c = next((cc for ttt, _, _, cc in spy_bars if ttt >= tt), None)
            qqq_c = next((cc for ttt, _, _, cc in qqq_bars if ttt >= tt), None)
            prob_v = next((v for pt, v in prob_pts if pt >= tt.normalize()), None)

            point = {"d": str(tt.date()), "c": round(c, 2)}
            if spy_e and spy_c:
                point["spy"] = round((spy_c / spy_e - 1) * 100, 2)
            if qqq_e and qqq_c:
                point["qqq"] = round((qqq_c / qqq_e - 1) * 100, 2)
            point["ret"] = round((c / ep - 1) * 100, 2)
            if prob_v is not None:
                point["prob"] = round(prob_v, 3)
            path.append(point)

        if path:
            key = f"{sym}_{mkt}_{t['entry_date']}"
            paths[key] = path
    return paths


def compute_benchmark(prices, eq_log):
    """Compute SPY/QQQ buy-and-hold returns over the same date range as portfolio."""
    if not eq_log:
        return []
    dates = [e["date"] for e in eq_log]
    start = pd.Timestamp(dates[0], tz="UTC")
    benchmarks = []
    for d_str in dates:
        d = pd.Timestamp(d_str, tz="UTC")
        spy_ret = bench_ret(prices, "SPY", dates[0], d_str)
        qqq_ret = bench_ret(prices, "QQQ", dates[0], d_str)
        benchmarks.append({"date": d_str, "spy_ret": spy_ret, "qqq_ret": qqq_ret})
    return benchmarks


def load_worlds_dump():
    """Load worlds_dump.csv for archetype deep-dive."""
    fp = PROJECT / "data" / "worlds_dump.csv"
    if not fp.exists():
        return {}
    df = pd.read_csv(fp)
    worlds = {}
    for _, r in df.iterrows():
        mid = str(r.get("market_id", ""))
        if mid not in worlds:
            worlds[mid] = {
                "question": str(r.get("question", "")),
                "archetype": str(r.get("archetype", "")),
                "assets": [],
            }
        worlds[mid]["assets"].append({
            "symbol": str(r.get("symbol", "")),
            "relevance": round(float(r.get("relevance_grade", 0)), 2) if pd.notna(r.get("relevance_grade")) else 0,
            "reason": str(r.get("reason", ""))[:200],
        })
    return worlds


# ── main ──────────────────────────────────────────────────────────

def main():
    print("Loading data...")
    df = pd.read_parquet(PROJECT / "data" / "candidates.parquet")
    df = df[df[REL_COL].astype(float) > 0.5].copy()
    df["t_theta"] = pd.to_datetime(df["t_theta"], utc=True)
    df["t_e"] = pd.to_datetime(df["t_e"], utc=True)

    is_earn = df["feat_archetype"].str.lower().str.contains("earning", na=False)
    df_earn = df[is_earn].copy()
    df_other = df[~is_earn].copy()

    print(f"Candidates: {len(df)} (earn={len(df_earn)} other={len(df_other)})")
    print("Loading prices/probs from DB...")
    P, PR = asyncio.run(load_paths(df))

    # ── Run experiments ──
    experiments = []

    def run_exp(name, subset, policy, policy_name, color):
        print(f"  Running: {name}...")
        tdf = run_backtest(subset, P, PR, policy)
        tdf = enrich(tdf, P)
        experiments.append({
            "name": name, "policy_name": policy_name, "color": color,
            "policy": {k: round(v, 4) if isinstance(v, float) else v for k, v in policy.items()},
            "splits": split_metrics(tdf),
            "exits": exit_counts(tdf),
            "trades": trades_to_list(tdf),
            "n_candidates": len(subset),
        })

    run_exp("All — Default", df, DEFAULT_POLICY, "Default", "#58a6ff")
    run_exp("All — CEM Sharpe", df, CEM_SHARPE, "CEM Sharpe", "#3fb950")
    run_exp("Earnings — Default", df_earn, DEFAULT_POLICY, "Default", "#f85149")
    run_exp("Earnings — CEM Sharpe", df_earn, CEM_SHARPE, "CEM Sharpe", "#ff7b72")
    run_exp("Non-Earnings — Default", df_other, DEFAULT_POLICY, "Default", "#bc8cff")
    run_exp("Non-Earnings — CEM Sharpe", df_other, CEM_SHARPE, "CEM Sharpe", "#d2a8ff")

    # RF experiments
    print("  Training RF lean model...")
    pipe = SkPipeline([
        ("imp", SimpleImputer(strategy="median")),
        ("rf", RandomForestRegressor(n_estimators=100, max_depth=6,
                                     min_samples_leaf=15, random_state=42, n_jobs=-1)),
    ])
    train_df = df[df["split"] == "train"]
    pipe.fit(train_df[NUM_FEATURES_LEAN], train_df[TARGET])
    preds = pd.Series(pipe.predict(df[NUM_FEATURES_LEAN]), index=df.index)
    df_rf = df[preds > 0].copy()

    rf_metrics = {}
    for sp in ("train", "val", "test"):
        sp_df = df[df["split"] == sp]
        if sp_df.empty:
            continue
        p = pipe.predict(sp_df[NUM_FEATURES_LEAN])
        actual = sp_df[TARGET].values
        corr = float(np.corrcoef(actual, p)[0, 1]) if len(sp_df) > 2 else 0
        rf_metrics[sp] = {"corr": round(corr, 3), "n": len(sp_df),
                          "dir_acc": round(float(((p > 0) == (actual > 0)).mean()), 3)}

    fi = pipe.named_steps["rf"].feature_importances_
    feature_imp = {f: round(float(fi[i]), 4) for i, f in enumerate(NUM_FEATURES_LEAN)}

    run_exp("All RF>0 — Default", df_rf, DEFAULT_POLICY, "Default+RF", "#79c0ff")
    run_exp("All RF>0 — CEM Sharpe", df_rf, CEM_SHARPE, "CEM Sharpe+RF", "#56d364")

    # ── Portfolio simulations ──
    print("  Portfolio CEM Sharpe...")
    pol_sharpe = port_cem(df, P, PR, dd_weight=0.3, n_iter=8, pop=30, seed=42)
    print("  Portfolio CEM Min-DD...")
    pol_mindd = port_cem(df, P, PR, dd_weight=1.5, n_iter=8, pop=30, seed=99)

    port_results = []

    def run_port(name, subset, policy, color):
        print(f"  Portfolio: {name}...")
        tdf, eq, stats, pol = sim_portfolio(subset, P, PR, policy)
        tdf_e = enrich(tdf, P) if not tdf.empty else tdf
        port_results.append({
            "name": name, "color": color,
            "policy": {k: round(v, 4) if isinstance(v, float) else v for k, v in pol.items()},
            "stats": stats, "equity": eq,
            "splits": split_metrics(tdf_e),
            "trades": trades_to_list(tdf_e),
        })

    run_port("Default (10%/trade, max 10)", df, PORT_DEFAULT, "#58a6ff")
    run_port("CEM Sharpe", df, {**pol_sharpe}, "#3fb950")
    run_port("CEM Min-DD", df, {**pol_mindd}, "#d29922")
    run_port("Non-Earnings CEM Sharpe", df_other, {**pol_sharpe}, "#bc8cff")

    # ── Opportunity-Cost Portfolio Simulations ──
    opp_cost_results = []

    def run_oc(name, subset, policy, bench_sym, color):
        print(f"  Opp-Cost: {name}...")
        tdf, eq, stats, pol = sim_portfolio_opp_cost(
            subset, P, PR, policy, bench_sym=bench_sym)
        tdf_e = enrich(tdf, P) if not tdf.empty else tdf
        opp_cost_results.append({
            "name": name, "color": color, "benchmark": bench_sym,
            "policy": {k: round(v, 4) if isinstance(v, float) else v
                       for k, v in pol.items()},
            "stats": stats, "equity": eq,
            "splits": split_metrics(tdf_e),
            "trades": trades_to_list(tdf_e),
        })

    for bsym, bcolor_base in [("SPY", 0), ("QQQ", 1)]:
        print(f"\n  === Opportunity-Cost: {bsym} ===")
        print(f"  CEM-OC {bsym} Sharpe...")
        oc_sharpe = port_cem_opp_cost(df, P, PR, bsym, "sharpe",
                                       n_iter=8, pop=30, seed=42)
        print(f"  CEM-OC {bsym} Max P&L...")
        oc_maxpnl = port_cem_opp_cost(df, P, PR, bsym, "max_pnl",
                                       n_iter=8, pop=30, seed=55)
        print(f"  CEM-OC {bsym} Min-DD...")
        oc_mindd = port_cem_opp_cost(df, P, PR, bsym, "min_dd",
                                      n_iter=8, pop=30, seed=99)

        colors = (["#58a6ff", "#3fb950", "#f97316", "#a855f7"] if bsym == "SPY"
                  else ["#38bdf8", "#22d3ee", "#fb923c", "#c084fc"])
        run_oc(f"{bsym} — Default", df, PORT_DEFAULT, bsym, colors[0])
        run_oc(f"{bsym} — CEM Sharpe", df, oc_sharpe, bsym, colors[1])
        run_oc(f"{bsym} — CEM Max P&L", df, oc_maxpnl, bsym, colors[2])
        run_oc(f"{bsym} — CEM Min-DD", df, oc_mindd, bsym, colors[3])

    # Print opportunity-cost summary
    print("\n  === Opportunity-Cost Results ===")
    for r in opp_cost_results:
        s = r["stats"]
        print(f"  {r['name']:30s}  return={s['total_return']:+.2f}%  "
              f"dd={s['max_dd']:.2f}%  trades={s['n_trades']}  "
              f"txn_cost=${s['total_txn_cost']:.2f}  "
              f"final=${s['final']:,.0f}")

    # ── Archetype breakdown ──
    tdf_all = run_backtest(df, P, PR, DEFAULT_POLICY)
    tdf_all = enrich(tdf_all, P)
    ne_trades = tdf_all[~tdf_all["archetype"].str.lower().str.contains("earning", na=False)]
    arch_stats = []
    if not ne_trades.empty:
        for arch, grp in ne_trades.groupby("archetype"):
            arch_stats.append({
                "archetype": str(arch)[:60], "n": len(grp),
                "mean_ret": round(float(grp.return_pct.mean()), 2),
                "win_pct": round(float((grp.return_pct > 0).mean() * 100), 1),
                "excess_spy": round(float(grp.excess_spy.mean()), 2),
                "avg_hold": round(float(grp.holding_days.mean()), 1),
            })
        arch_stats.sort(key=lambda x: x["n"], reverse=True)

    # ── Holding days distribution ──
    hold_dist = []
    for label, lo, hi in [("0-3d", 0, 3), ("4-7d", 4, 7), ("8-14d", 8, 14),
                           ("15-21d", 15, 21), ("22-30d", 22, 30), ("30d+", 31, 999)]:
        sub = tdf_all[(tdf_all.holding_days >= lo) & (tdf_all.holding_days <= hi)]
        if sub.empty:
            continue
        hold_dist.append({
            "bucket": label, "n": len(sub),
            "mean_ret": round(float(sub.return_pct.mean()), 2),
            "win_pct": round(float((sub.return_pct > 0).mean() * 100), 1),
            "excess_spy": round(float(sub.excess_spy.mean()), 2),
        })

    # ── NEW: Extra data for enhanced dashboard ──
    print("  Loading worlds dump...")
    worlds = load_worlds_dump()

    print("  Extracting trade price paths (top 200)...")
    default_trades = experiments[0]["trades"] if experiments else []
    price_paths = extract_trade_paths(default_trades, P, PR, limit=200)

    print("  Computing portfolio benchmarks...")
    port_benchmark = compute_benchmark(P, port_results[0]["equity"]) if port_results else []

    print("  Parsing experiment log...")
    exp_log_path = PROJECT / "EXPERIMENTS_LOG.md"
    exp_log = parse_experiment_log(exp_log_path) if exp_log_path.exists() else []

    # ── Build dashboard data ──
    data = {
        "generated": str(pd.Timestamp.now()),
        "n_candidates": len(df),
        "n_earnings": len(df_earn),
        "n_other": len(df_other),
        "experiments": experiments,
        "portfolio": port_results,
        "rf_metrics": rf_metrics,
        "feature_importance": feature_imp,
        "archetype_stats": arch_stats[:20],
        "holding_dist": hold_dist,
        "worlds": {k: v for k, v in worlds.items()
                   if k in {t["market_id"] for e in experiments for t in e["trades"]}},
        "price_paths": price_paths,
        "port_benchmark": port_benchmark,
        "experiment_log": exp_log,
        "opp_cost": opp_cost_results,
    }

    print("Generating HTML...")
    html = build_html(data)
    out = PROJECT / "data" / "dashboard.html"
    out.write_text(html, encoding="utf-8")
    print(f"Dashboard saved to {out}")
    webbrowser.open(str(out))


# ── HTML generation ──────────────────────────────────────────────

def build_html(data):
    d = json.dumps(data, default=str)
    gen_ts = data['generated'][:19]
    n_cand = data['n_candidates']
    n_earn = data['n_earnings']
    n_other = data['n_other']

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Alpha — Strategy Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.plot.ly/plotly-2.32.0.min.js"></script>
<style>
:root {{
  --bg: #0a0e14; --surface: #131921; --surface2: #1a2332;
  --border: #243044; --border2: #2d3f58;
  --text: #e2e8f0; --text2: #8899ac; --text3: #5a6b7f;
  --green: #22c55e; --green-dim: rgba(34,197,94,.12);
  --red: #ef4444; --red-dim: rgba(239,68,68,.12);
  --blue: #3b82f6; --blue-dim: rgba(59,130,246,.12);
  --yellow: #eab308; --yellow-dim: rgba(234,179,8,.12);
  --purple: #a855f7; --purple-dim: rgba(168,85,247,.12);
  --cyan: #06b6d4; --cyan-dim: rgba(6,182,212,.12);
  --orange: #f97316;
  --grad1: linear-gradient(135deg, #1e3a5f 0%, #0a0e14 100%);
  --grad-card: linear-gradient(135deg, rgba(26,35,50,.8) 0%, rgba(19,25,33,.9) 100%);
  --glass: rgba(19,25,33,.7);
  --glass-border: rgba(59,130,246,.15);
  --radius: 12px; --radius-sm: 8px;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:var(--bg); color:var(--text); font-family:'Inter',system-ui,-apple-system,sans-serif; line-height:1.5; }}

/* ── Hero ── */
.hero {{
  background: var(--grad1);
  text-align:center; padding:48px 24px 24px;
  border-bottom: 1px solid var(--border);
  position:relative; overflow:hidden;
}}
.hero::before {{
  content:''; position:absolute; top:-50%; left:-50%; width:200%; height:200%;
  background: radial-gradient(circle at 30% 20%, rgba(59,130,246,.06) 0%, transparent 50%),
              radial-gradient(circle at 70% 80%, rgba(168,85,247,.04) 0%, transparent 50%);
}}
.hero h1 {{ font-size:28px; font-weight:800; letter-spacing:-.5px; position:relative;
  background: linear-gradient(135deg, #e2e8f0, #3b82f6);
  -webkit-background-clip:text; -webkit-text-fill-color:transparent; }}
.hero p {{ color:var(--text2); margin-top:8px; font-size:14px; position:relative; }}

/* ── Nav ── */
.nav {{
  background: var(--surface); border-bottom:1px solid var(--border);
  padding:0 16px; display:flex; gap:0; position:sticky; top:0; z-index:100;
  overflow-x:auto; backdrop-filter:blur(12px);
}}
.nav button {{
  background:none; border:none; color:var(--text2); padding:12px 16px;
  cursor:pointer; font-size:13px; font-weight:500; font-family:inherit;
  border-bottom:2px solid transparent; transition:all .2s; white-space:nowrap;
}}
.nav button:hover {{ color:var(--text); background:rgba(59,130,246,.06); }}
.nav button.active {{ color:var(--blue); border-bottom-color:var(--blue); }}

/* ── Layout ── */
.page {{ display:none; padding:24px; max-width:1440px; margin:0 auto; }}
.page.active {{ display:block; animation: fadeIn .3s ease; }}
@keyframes fadeIn {{ from {{ opacity:0; transform:translateY(8px); }} to {{ opacity:1; transform:translateY(0); }} }}
.grid {{ display:grid; gap:16px; }}
.g2 {{ grid-template-columns:1fr 1fr; }}
.g3 {{ grid-template-columns:1fr 1fr 1fr; }}
.g4 {{ grid-template-columns:repeat(4,1fr); }}
.g5 {{ grid-template-columns:repeat(5,1fr); }}
@media(max-width:1000px) {{ .g2,.g3,.g4,.g5 {{ grid-template-columns:1fr; }} }}

/* ── Cards ── */
.card {{
  background:var(--grad-card); border:1px solid var(--border); border-radius:var(--radius);
  padding:20px; backdrop-filter:blur(8px); transition: border-color .2s, transform .15s;
}}
.card:hover {{ border-color:var(--border2); }}
.card h3 {{ color:var(--text2); font-size:11px; text-transform:uppercase; letter-spacing:1.2px; margin-bottom:8px; font-weight:600; }}
.card .val {{ font-size:28px; font-weight:700; letter-spacing:-.5px; }}
.card .sub {{ color:var(--text2); font-size:12px; margin-top:6px; }}
.pos {{ color:var(--green); }} .neg {{ color:var(--red); }}

/* ── Section headers ── */
h2 {{
  font-size:18px; font-weight:700; margin:32px 0 16px; color:var(--text);
  display:flex; align-items:center; gap:10px;
}}
h2:first-child {{ margin-top:0; }}
h2 .badge {{ background:var(--blue-dim); color:var(--blue); font-size:11px; padding:2px 8px;
  border-radius:20px; font-weight:600; }}

/* ── Tables ── */
table {{ width:100%; border-collapse:collapse; font-size:12px; }}
th {{
  text-align:left; padding:10px 12px; background:var(--surface2); border-bottom:1px solid var(--border);
  color:var(--text2); font-weight:600; position:sticky; top:46px; z-index:5;
  cursor:pointer; user-select:none; font-size:11px; text-transform:uppercase; letter-spacing:.5px;
}}
th:hover {{ color:var(--text); }}
td {{ padding:8px 12px; border-bottom:1px solid rgba(36,48,68,.5); }}
tr:hover td {{ background:rgba(59,130,246,.03); }}
tr.clickable {{ cursor:pointer; }}
tr.clickable:hover td {{ background:rgba(59,130,246,.08); }}

/* ── Chart containers ── */
.chart-box {{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  padding:16px; margin-bottom:16px;
}}

/* ── Tags & pills ── */
.tag {{ display:inline-block; padding:2px 8px; border-radius:6px; font-size:10px; font-weight:600; letter-spacing:.3px; }}
.tg {{ background:var(--green-dim); color:var(--green); }}
.tr {{ background:var(--red-dim); color:var(--red); }}
.tb {{ background:var(--blue-dim); color:var(--blue); }}
.ty {{ background:var(--yellow-dim); color:var(--yellow); }}
.tp {{ background:var(--purple-dim); color:var(--purple); }}
.pill {{
  display:inline-flex; align-items:center; gap:6px; padding:5px 12px;
  background:var(--surface2); border:1px solid var(--border); border-radius:20px;
  font-size:11px; font-weight:500;
}}

/* ── Filter bar ── */
.filter-bar {{ display:flex; gap:10px; margin-bottom:16px; flex-wrap:wrap; align-items:center; }}
.filter-bar input, .filter-bar select {{
  background:var(--surface2); border:1px solid var(--border); color:var(--text);
  padding:8px 12px; border-radius:var(--radius-sm); font-size:12px; font-family:inherit;
  transition: border-color .2s;
}}
.filter-bar input:focus, .filter-bar select:focus {{ outline:none; border-color:var(--blue); }}
.filter-bar input {{ min-width:220px; }}

/* ── Pagination ── */
.pagination {{
  display:flex; align-items:center; justify-content:center; gap:8px; margin-top:16px; padding:12px;
}}
.pagination button {{
  background:var(--surface2); border:1px solid var(--border); color:var(--text2);
  padding:6px 14px; border-radius:var(--radius-sm); cursor:pointer; font-size:12px; font-family:inherit;
  transition: all .2s;
}}
.pagination button:hover {{ border-color:var(--blue); color:var(--text); }}
.pagination button.active {{ background:var(--blue); color:#fff; border-color:var(--blue); }}
.pagination button:disabled {{ opacity:.3; cursor:not-allowed; }}
.pagination span {{ color:var(--text2); font-size:12px; }}

/* ── Info box ── */
.info-box {{
  background: linear-gradient(135deg, rgba(59,130,246,.08), rgba(168,85,247,.04));
  border:1px solid rgba(59,130,246,.2); border-radius:var(--radius);
  padding:16px 20px; margin-bottom:20px; font-size:13px; line-height:1.7;
}}
.info-box h4 {{ color:var(--blue); font-size:13px; margin-bottom:8px; font-weight:700; }}
.info-box .metric-explain {{
  display:grid; grid-template-columns:140px 1fr; gap:4px 16px; margin-top:8px;
}}
.info-box .metric-explain dt {{ color:var(--cyan); font-weight:600; font-size:12px; }}
.info-box .metric-explain dd {{ color:var(--text2); font-size:12px; margin:0; }}

/* ── Strategy cards ── */
.strat-card {{
  background:var(--grad-card); border:1px solid var(--border); border-radius:var(--radius);
  padding:20px; transition: all .2s;
}}
.strat-card:hover {{ border-color:var(--blue); }}
.strat-card h4 {{ font-size:14px; font-weight:700; margin-bottom:8px; }}
.strat-card p {{ font-size:12px; color:var(--text2); line-height:1.6; }}
.strat-card .strat-tag {{ display:inline-block; margin-top:8px; }}

/* ── Experiment timeline ── */
.timeline {{ position:relative; padding-left:24px; }}
.timeline::before {{
  content:''; position:absolute; left:8px; top:0; bottom:0; width:2px;
  background: linear-gradient(180deg, var(--blue), var(--purple));
}}
.timeline-phase {{ margin-bottom:24px; }}
.timeline-phase h3 {{
  font-size:15px; font-weight:700; margin-bottom:12px; color:var(--blue);
  position:relative;
}}
.timeline-phase h3::before {{
  content:''; position:absolute; left:-20px; top:6px; width:10px; height:10px;
  border-radius:50%; background:var(--blue); border:2px solid var(--bg);
}}
.timeline-exp {{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:14px 16px; margin-bottom:10px; cursor:pointer; transition:all .2s;
}}
.timeline-exp:hover {{ border-color:var(--blue); transform:translateX(4px); }}
.timeline-exp h4 {{ font-size:13px; font-weight:600; margin-bottom:4px; display:flex; align-items:center; gap:8px; }}
.timeline-exp h4 .eid {{ color:var(--cyan); font-size:11px; font-weight:700; }}
.timeline-exp .exp-body {{ display:none; margin-top:10px; font-size:12px; color:var(--text2); line-height:1.6; }}
.timeline-exp.open .exp-body {{ display:block; }}
.timeline-exp .exp-field {{ margin:4px 0; }}
.timeline-exp .exp-field strong {{ color:var(--text); }}

/* ── Modal ── */
.modal-overlay {{
  display:none; position:fixed; top:0; left:0; width:100%; height:100%; z-index:1000;
  background:rgba(0,0,0,.7); backdrop-filter:blur(4px);
  justify-content:center; align-items:flex-start; padding:40px 20px; overflow-y:auto;
}}
.modal-overlay.active {{ display:flex; }}
.modal {{
  background:var(--surface); border:1px solid var(--border); border-radius:var(--radius);
  max-width:1000px; width:100%; padding:24px; position:relative;
  animation: modalIn .25s ease;
}}
@keyframes modalIn {{ from {{ opacity:0; transform:scale(.95); }} to {{ opacity:1; transform:scale(1); }} }}
.modal-close {{
  position:absolute; top:12px; right:16px; background:none; border:none;
  color:var(--text2); font-size:22px; cursor:pointer; padding:4px 8px;
}}
.modal-close:hover {{ color:var(--text); }}
.modal h3 {{ font-size:18px; font-weight:700; margin-bottom:16px; }}

/* ── Metric coloring ── */
.metric-good {{ color:var(--green); }}
.metric-warn {{ color:var(--yellow); }}
.metric-bad {{ color:var(--red); }}
.metric-neutral {{ color:var(--text2); }}

/* ── Scrollbar ── */
::-webkit-scrollbar {{ width:6px; height:6px; }}
::-webkit-scrollbar-track {{ background:var(--bg); }}
::-webkit-scrollbar-thumb {{ background:var(--border); border-radius:3px; }}
::-webkit-scrollbar-thumb:hover {{ background:var(--border2); }}
</style>
</head>
<body>
<div class="hero">
  <h1>Polymarket Alpha — Strategy Dashboard</h1>
  <p>Generated {gen_ts} &nbsp;|&nbsp; {n_cand} candidates ({n_earn} earnings, {n_other} non-earnings)</p>
</div>
<nav class="nav">
  <button class="active" onclick="showTab('overview')">Overview</button>
  <button onclick="showTab('strategies')">Strategies</button>
  <button onclick="showTab('earnings')">Earnings & Archetypes</button>
  <button onclick="showTab('portfolio')">Portfolio $100K</button>
  <button onclick="showTab('risk')">Risk Metrics</button>
  <button onclick="showTab('rf')">RF Model</button>
  <button onclick="showTab('trades')">Trade Explorer</button>
</nav>

<div id="overview" class="page active"></div>
<div id="strategies" class="page"></div>
<div id="earnings" class="page"></div>
<div id="portfolio" class="page"></div>
<div id="risk" class="page"></div>
<div id="rf" class="page"></div>
<div id="trades" class="page"></div>
<div id="explog" class="page"></div>

<!-- Trade Detail Modal -->
<div class="modal-overlay" id="trade-modal">
  <div class="modal">
    <button class="modal-close" onclick="closeModal()">&times;</button>
    <div id="modal-content"></div>
  </div>
</div>

<script>
const D = {d};

const LAYOUT = {{
  paper_bgcolor:'transparent', plot_bgcolor:'rgba(10,14,20,.6)',
  font:{{color:'#8899ac', family:'Inter, system-ui, sans-serif', size:11}},
  xaxis:{{gridcolor:'rgba(36,48,68,.6)', zerolinecolor:'rgba(36,48,68,.8)', tickfont:{{size:10}}}},
  yaxis:{{gridcolor:'rgba(36,48,68,.6)', zerolinecolor:'rgba(36,48,68,.8)', tickfont:{{size:10}}}},
  margin:{{t:44,b:44,l:56,r:24}},
  legend:{{bgcolor:'transparent', font:{{size:10}}}},
  hoverlabel:{{bgcolor:'#1a2332', bordercolor:'#243044', font:{{family:'Inter', size:12}}}},
}};
const CFG = {{responsive:true, displayModeBar:false}};

// ── Strategy Descriptions ──
const STRAT_DESC = {{
  'Default': {{
    title: 'Default (Hand-Tuned Baseline)',
    desc: 'The original strategy with hand-tuned parameters. Enters when Polymarket probability crosses 75% (instant) or holds above 70% for 1 day. Uses a 2.5× ATR trailing stop, profit lock starting at 3%, and exits when probability drops below 55%. Designed as a conservative baseline.',
    opt: 'Manual tuning based on domain knowledge',
    color: '#58a6ff',
  }},
  'CEM Sharpe': {{
    title: 'CEM Sharpe (Evolutionary RL — Risk-Adjusted)',
    desc: 'Cross-Entropy Method (CEM) evolutionary search over 6 policy knobs, optimizing the annualized Sharpe ratio (return÷risk). Takes more trades at a lower entry bar (70.9%) but cuts Polymarket-exit losses faster (θ=0.594). Produces lower variance and higher risk-adjusted returns. Test Sharpe: 3.25.',
    opt: 'Maximizes Sharpe ratio on train set',
    color: '#3fb950',
  }},
  'Default+RF': {{
    title: 'Default + Random Forest Filter',
    desc: 'The Default strategy with an additional ML filter: a Random Forest model trained on 7 lean features predicts trade return, and only trades with predicted return > 0 are taken. This filters out unpromising setups before entry. The RF\\'s job is selection + magnitude, NOT direction (direction is a coin flip OOS).',
    opt: 'RF filters to predicted-positive trades',
    color: '#79c0ff',
  }},
  'CEM Sharpe+RF': {{
    title: 'CEM Sharpe + Random Forest Filter',
    desc: 'Combines the CEM Sharpe policy (optimized entry/exit parameters) with the RF selection filter. This layers ML-based candidate filtering on top of the risk-adjusted entry/exit rules, giving the most selective trade set.',
    opt: 'CEM-optimized exits + RF-filtered entries',
    color: '#56d364',
  }},
}};

// ── Risk Metric Explanations ──
const RISK_EXPLAIN = {{
  sharpe: {{ name:'Sharpe Ratio', desc:'Risk-adjusted return: mean daily return ÷ std deviation, annualized. >1 decent, >2 strong, >3 exceptional.', good:2, warn:1 }},
  sortino: {{ name:'Sortino Ratio', desc:'Like Sharpe but only penalizes downside volatility. Better for asymmetric returns. >2 good, >3 great.', good:2, warn:1 }},
  max_dd: {{ name:'Max Drawdown', desc:'Largest peak-to-trough cumulative loss. Measures worst-case scenario. <-10% OK, <-30% concerning, <-50% severe.', good:-10, warn:-30, invert:true }},
  calmar: {{ name:'Calmar Ratio', desc:'Annualized return ÷ |max drawdown|. Measures return per unit of drawdown risk. >1 good, >2 strong.', good:1, warn:0.5 }},
  var_95: {{ name:'VaR 95%', desc:'Value at Risk — the loss level that only 5% of trades exceed. E.g. VaR=-8% means 95% of trades lose less than 8%.', good:-5, warn:-10, invert:true }},
  cvar_95: {{ name:'CVaR 95%', desc:'Conditional VaR — the average loss in the worst 5% of trades. Measures tail risk severity.', good:-8, warn:-15, invert:true }},
  profit_factor: {{ name:'Profit Factor', desc:'Sum of all gains ÷ sum of all losses. >1 = profitable system. >1.5 = strong. >2 = excellent.', good:1.5, warn:1 }},
  win_pct: {{ name:'Win Rate', desc:'Percentage of trades that are profitable. >55% strong for this strategy type.', good:55, warn:50 }},
}};

function riskColor(key, val) {{
  const r = RISK_EXPLAIN[key];
  if(!r) return '';
  if(r.invert) {{
    if(val >= r.good) return 'metric-good';
    if(val >= r.warn) return 'metric-warn';
    return 'metric-bad';
  }}
  if(val >= r.good) return 'metric-good';
  if(val >= r.warn) return 'metric-warn';
  return 'metric-bad';
}}

// ── Utilities ──
function showTab(id) {{
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  const btn = document.querySelector(`.nav button[onclick="showTab('${{id}}')"]`);
  if(btn) btn.classList.add('active');
  if(!document.getElementById(id).dataset.rendered) {{
    render[id]();
    document.getElementById(id).dataset.rendered = '1';
  }}
}}

function fmtPct(v) {{
  if(v == null || isNaN(v)) return '<span class="metric-neutral">—</span>';
  return v > 0 ? `<span class="pos">+${{v.toFixed(2)}}%</span>` : v < 0 ? `<span class="neg">${{v.toFixed(2)}}%</span>` : `${{v.toFixed(2)}}%`;
}}
function fmtVal(v) {{
  if(v == null || isNaN(v)) return '—';
  return v > 0 ? `<span class="pos">+${{v.toFixed(2)}}</span>` : v < 0 ? `<span class="neg">${{v.toFixed(2)}}</span>` : v.toFixed(2);
}}
function fmtMoney(v) {{ return '$' + v.toLocaleString('en-US', {{minimumFractionDigits:0, maximumFractionDigits:0}}); }}
function closeModal() {{ document.getElementById('trade-modal').classList.remove('active'); }}
document.getElementById('trade-modal').addEventListener('click', e => {{ if(e.target === e.currentTarget) closeModal(); }});

const render = {{}};

// ═══════════════════════════════════════════
// OVERVIEW
// ═══════════════════════════════════════════
render.overview = function() {{
  const el = document.getElementById('overview');
  const exps = D.experiments;
  const FRONT = ['All — Default','All — CEM Sharpe','All RF>0 — Default','All RF>0 — CEM Sharpe'];
  const front = exps.filter(e => FRONT.includes(e.name));
  const test0 = exps[0]?.splits?.test || {{}};
  const bestTest = front.reduce((a,e) => {{
    const s = e.splits?.test?.sharpe||0;
    return s > a.v ? {{v:s,n:e.name}} : a;
  }}, {{v:-999,n:''}});
  const bestWin = front.reduce((a,e) => {{
    const w = e.splits?.test?.win_pct||0;
    return w > a.v ? {{v:w,n:e.name}} : a;
  }}, {{v:0,n:''}});

  let html = `<div class="grid g5" style="margin-bottom:24px">
    <div class="card"><h3>Experiments</h3><div class="val" style="color:var(--blue)">${{front.length}}</div>
      <div class="sub">${{D.n_candidates}} candidates</div></div>
    <div class="card"><h3>Total Trades</h3><div class="val">${{front[0]?.splits?.all?.n || 0}}</div>
      <div class="sub">Default strategy</div></div>
    <div class="card"><h3>Best Test Sharpe</h3><div class="val pos">+${{bestTest.v.toFixed(2)}}</div>
      <div class="sub">${{bestTest.n}}</div></div>
    <div class="card"><h3>Best Test Win%</h3><div class="val pos">${{bestWin.v.toFixed(1)}}%</div>
      <div class="sub">${{bestWin.n}}</div></div>
    <div class="card"><h3>Avg Excess vs SPY</h3><div class="val">${{fmtPct(test0.excess_spy||0)}}</div>
      <div class="sub">Default — Test set</div></div>
  </div>`;

  // Research summary
  html += `<div class="info-box" style="margin-bottom:24px">
    <h4 style="cursor:pointer" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'block':'none'">
      Research: Arbitraging the Information Diffusion Lag
      <span style="float:right;font-size:10px;color:var(--text3)">click to expand</span>
    </h4>
    <div style="display:none;margin-top:10px;font-size:12px;line-height:1.7;color:var(--text2)">
      <p><strong style="color:var(--text)">Thesis:</strong> Prediction-market probability crossings (Polymarket) flag cross-sectional equity mispricings during the information-diffusion lag. When a macro/geopolitical event resolves, ETF arbitrage forces uniform index moves while individual stocks take hours-to-days to reprice to their true fundamental sensitivity.</p>
      <p style="margin-top:8px"><strong style="color:var(--text)">Implemented:</strong> Polymarket signal intake → LLM semantic router (asset mapping) → ATR trailing stops with stepped profit locks → CEM/RL policy optimization → Portfolio risk layer. RF model tested for trade filtering.</p>
      <p style="margin-top:8px"><strong style="color:var(--text)">Key findings:</strong></p>
      <ul style="margin:4px 0 0 16px">
        <li>Direction prediction is a coin flip OOS — go long the beneficiary instead</li>
        <li>Per-archetype modeling required (earnings ≠ macro ≠ geopolitical)</li>
        <li>ATR-based stops eliminate the Wide Stop Trap overfitting</li>
        <li>CEM converges on ~3x ATR, 70% entry bar, tight Poly exit</li>
        <li>RF magnitude filtering has limited OOS value</li>
        <li>The edge is small but consistent — test 63-68% win rate, 1.4-1.9% mean return</li>
      </ul>
    </div>
  </div>`;

  // Opportunity-Cost hero chart
  const oc = D.opp_cost || [];
  if(oc.length > 0) {{
    html += `<h2>Strategy Alpha vs Index Buy-and-Hold <span class="badge">Opportunity Cost</span></h2>
      <div style="margin-bottom:8px;display:flex;gap:8px">
        <button class="oc-toggle" onclick="renderOC('SPY')" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:6px 14px;border-radius:var(--radius-sm);cursor:pointer;font-size:12px;font-family:inherit">SPY Base</button>
        <button class="oc-toggle" onclick="renderOC('QQQ')" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);padding:6px 14px;border-radius:var(--radius-sm);cursor:pointer;font-size:12px;font-family:inherit">QQQ Base</button>
      </div>
      <div class="chart-box" id="ov-opp-cost"></div>
      <div class="grid g4" id="ov-oc-cards" style="margin-top:12px"></div>`;
  }}

  // Comparison tables — front page experiments only
  for(const [label, spKey] of [['Validation Set', 'val'], ['Test Set (OOS)', 'test']]) {{
    html += `<h2>${{label}}</h2>
    <div class="card"><table><thead><tr>
      <th>Experiment</th><th>N</th><th>Mean Return</th><th>Win%</th><th>Sharpe</th>
      <th>Sortino</th><th>Max DD</th><th>vs SPY</th><th>vs QQQ</th><th>Hold</th><th>PF</th>
    </tr></thead><tbody>`;
    for(const e of front) {{
      const v = e.splits[spKey];
      if(!v || !v.n) continue;
      html += `<tr>
        <td><span class="pill" style="border-color:${{e.color}};color:${{e.color}}">${{e.name}}</span></td>
        <td>${{v.n}}</td><td>${{fmtPct(v.mean_ret)}}</td><td>${{v.win_pct}}%</td>
        <td class="${{riskColor('sharpe',v.sharpe)}}">${{fmtVal(v.sharpe)}}</td>
        <td class="${{riskColor('sortino',v.sortino)}}">${{fmtVal(v.sortino)}}</td>
        <td>${{fmtPct(v.max_dd)}}</td><td>${{fmtPct(v.excess_spy)}}</td><td>${{fmtPct(v.excess_qqq)}}</td>
        <td>${{v.avg_hold}}d</td><td>${{v.profit_factor}}</td></tr>`;
    }}
    html += `</tbody></table></div>`;
  }}

  html += `<div class="grid g2" style="margin-top:20px">
    <div class="chart-box" id="ov-bar-ret"></div>
    <div class="chart-box" id="ov-bar-sharpe"></div>
  </div>`;

  // Experiment timeline (moved from separate tab)
  const lg = D.experiment_log || [];
  if(lg.length > 0) {{
    html += `<h2 style="cursor:pointer" onclick="document.getElementById('exp-timeline').style.display=document.getElementById('exp-timeline').style.display==='none'?'block':'none'">Experiment History <span class="badge">${{lg.reduce((a,p)=>a+p.experiments.length,0)}} experiments</span> <span style="font-size:11px;color:var(--text3);margin-left:8px">click to toggle</span></h2>`;
    html += `<div id="exp-timeline" style="display:none"><div class="timeline">`;
    for(const phase of lg) {{
      html += `<div class="timeline-phase"><h3>${{phase.name}}</h3>`;
      for(const exp of phase.experiments) {{
        html += `<div class="timeline-exp" onclick="this.classList.toggle('open')">
          <h4><span class="eid">${{exp.id}}</span> ${{exp.title}}</h4>
          <div class="exp-body">`;
        for(const f of ['what','why','data','changed_from','result','converged_policy','why_it_failed','why_it_worked','learned']) {{
          if(exp[f]) html += `<div class="exp-field"><strong style="text-transform:capitalize">${{f.replace(/_/g,' ')}}:</strong> ${{exp[f]}}</div>`;
        }}
        if(!exp.what && exp.body_text) html += `<div style="white-space:pre-wrap;font-size:11px;color:var(--text3)">${{exp.body_text}}</div>`;
        html += `</div></div>`;
      }}
      html += `</div>`;
    }}
    html += `</div></div>`;
  }}

  el.innerHTML = html;

  // Render opp-cost chart
  window.renderOC = function(bench) {{
    const filtered = oc.filter(r => r.benchmark === bench);
    if(!filtered.length) return;
    const traces = filtered.map(r => ({{
      x:r.equity.map(e=>e.date), y:r.equity.map(e=>e.equity),
      name:r.name, mode:'lines', line:{{color:r.color, width:2}},
    }}));
    // Pure benchmark buy-and-hold
    const benchEq = filtered[0].equity;
    if(benchEq.length > 0) {{
      const startDate = benchEq[0].date;
      const endDate = benchEq[benchEq.length-1].date;
      traces.push({{
        x:benchEq.map(e=>e.date), y:benchEq.map((_,i)=>100000),
        name:'Starting Capital', mode:'lines',
        line:{{color:'#5a6b7f', width:1, dash:'dash'}},
      }});
    }}
    Plotly.newPlot('ov-opp-cost', traces, {{...LAYOUT,
      title:`Strategy Over ${{bench}} vs Pure ${{bench}} Hold`,
      height:450, yaxis:{{...LAYOUT.yaxis, title:'Equity ($)', tickformat:'$,.0f'}},
    }}, CFG);
    // Summary cards
    let cards = '';
    for(const r of filtered) {{
      const s = r.stats;
      const cls = s.total_return > 0 ? 'pos' : 'neg';
      cards += `<div class="card">
        <h3>${{r.name.replace(bench+' — ','')}}</h3>
        <div class="val ${{cls}}">${{s.total_return > 0 ? '+' : ''}}${{s.total_return.toFixed(2)}}%</div>
        <div class="sub">Final: $$${{s.final.toLocaleString()}} · DD: ${{s.max_dd.toFixed(1)}}% · Trades: ${{s.n_trades}} · Txn: $$${{s.total_txn_cost.toFixed(0)}}</div>
      </div>`;
    }}
    document.getElementById('ov-oc-cards').innerHTML = cards;
  }};
  if(oc.length > 0) renderOC('SPY');

  // Overview bar charts — front page experiments only
  const names = front.map(e=>e.name);
  const colors = front.map(e=>e.color);
  Plotly.newPlot('ov-bar-ret', [
    {{x:names, y:front.map(e=>e.splits.val?.mean_ret||0), type:'bar', name:'Val', marker:{{color:colors, opacity:.85}}}},
    {{x:names, y:front.map(e=>e.splits.test?.mean_ret||0), type:'bar', name:'Test', marker:{{color:colors, opacity:.4}}}},
  ], {{...LAYOUT, title:'Mean Return by Experiment', barmode:'group', height:380,
    xaxis:{{...LAYOUT.xaxis, type:'category', tickangle:-25, tickfont:{{size:9}}}}}}, CFG);

  Plotly.newPlot('ov-bar-sharpe', [
    {{x:names, y:front.map(e=>e.splits.val?.sharpe||0), type:'bar', name:'Val', marker:{{color:colors, opacity:.85}}}},
    {{x:names, y:front.map(e=>e.splits.test?.sharpe||0), type:'bar', name:'Test', marker:{{color:colors, opacity:.4}}}},
  ], {{...LAYOUT, title:'Sharpe Ratio by Experiment', barmode:'group', height:380,
    xaxis:{{...LAYOUT.xaxis, type:'category', tickangle:-25, tickfont:{{size:9}}}}}}, CFG);
}};

// ═══════════════════════════════════════════
// STRATEGIES
// ═══════════════════════════════════════════
render.strategies = function() {{
  const el = document.getElementById('strategies');
  const exps = D.experiments;

  // Strategy explanations
  let html = `<h2>Strategy Explanations</h2><div class="grid g2" style="margin-bottom:24px">`;
  const seen = new Set();
  for(const e of exps) {{
    const key = e.policy_name;
    if(seen.has(key)) continue; seen.add(key);
    const sd = STRAT_DESC[key] || {{title:key, desc:'No description available.', opt:'-', color:e.color}};
    html += `<div class="strat-card" style="border-left:3px solid ${{sd.color}}">
      <h4 style="color:${{sd.color}}">${{sd.title}}</h4>
      <p>${{sd.desc}}</p>
      <div class="strat-tag"><span class="tag tb">Optimizes: ${{sd.opt}}</span></div>
    </div>`;
  }}
  html += `</div>`;

  // Policy parameters table
  html += `<h2>Policy Parameters</h2><div class="card"><table><thead><tr>
    <th>Strategy</th><th>ATR Mult</th><th>Lock Activate</th><th>θ Out</th>
    <th>Enter Strong</th><th>Enter Floor</th><th>Hold Days</th><th>Max Prob Surge</th><th>Max Price Runup</th>
  </tr></thead><tbody>`;
  const seen2 = new Set();
  for(const e of exps) {{
    if(seen2.has(e.policy_name)) continue; seen2.add(e.policy_name);
    const p = e.policy;
    html += `<tr><td style="color:${{e.color}};font-weight:600">${{e.policy_name}}</td>
      <td>${{p.atr_mult}}</td><td>${{(p.lock_activate*100).toFixed(1)}}%</td>
      <td>${{p.theta_out}}</td><td>${{(p.enter_strong*100).toFixed(1)}}%</td><td>${{(p.enter_floor*100).toFixed(1)}}%</td>
      <td>${{p.hold_days}}</td><td>${{(p.max_prob_surge*100).toFixed(0)}}%</td><td>${{(p.max_price_runup*100).toFixed(0)}}%</td></tr>`;
  }}
  html += `</tbody></table></div>`;

  // Exit reason pies
  html += `<h2>Exit Reason Breakdown</h2><div class="grid g3">`;
  for(const e of exps.slice(0,6)) {{
    html += `<div class="chart-box" id="exit-${{e.name.replace(/[^a-z0-9]/gi,'')}}"></div>`;
  }}
  html += `</div>`;

  // Risk-return scatter + holding dist
  html += `<h2>Risk-Return Scatter (Test Set)</h2><div class="chart-box" id="risk-scatter"></div>`;
  html += `<h2>Holding Days Distribution</h2><div class="chart-box" id="hold-dist"></div>`;
  el.innerHTML = html;

  // Render exit pies
  const pieColors = ['#3b82f6','#22c55e','#eab308','#ef4444','#a855f7','#06b6d4','#f97316'];
  for(const e of exps.slice(0,6)) {{
    const id = 'exit-' + e.name.replace(/[^a-z0-9]/gi,'');
    Plotly.newPlot(id, [{{labels:Object.keys(e.exits), values:Object.values(e.exits),
      type:'pie', hole:.45, marker:{{colors:pieColors}},
      textinfo:'label+percent', textfont:{{size:10}}}}],
      {{...LAYOUT, title:e.name, showlegend:false, height:260, margin:{{t:40,b:16,l:16,r:16}}}}, CFG);
  }}

  // Risk-return scatter
  Plotly.newPlot('risk-scatter', exps.map(e => ({{
    x:[Math.abs(e.splits.test?.max_dd||0)], y:[e.splits.test?.sharpe||0],
    text:[e.name], mode:'markers+text', textposition:'top center',
    marker:{{size:14,color:e.color}}, name:e.name, textfont:{{size:9,color:e.color}}
  }})), {{...LAYOUT, title:'Sharpe vs Max Drawdown (Test)', showlegend:false, height:380,
    xaxis:{{...LAYOUT.xaxis, title:'|Max Drawdown| %'}},
    yaxis:{{...LAYOUT.yaxis, title:'Sharpe Ratio'}}}}, CFG);

  // Holding dist
  const hd = D.holding_dist;
  if(hd && hd.length) {{
    Plotly.newPlot('hold-dist', [
      {{x:hd.map(h=>h.bucket), y:hd.map(h=>h.n), type:'bar', name:'Count', marker:{{color:'#3b82f6', opacity:.7}}}},
      {{x:hd.map(h=>h.bucket), y:hd.map(h=>h.mean_ret), type:'scatter', mode:'lines+markers',
        name:'Mean Return %', marker:{{color:'#22c55e',size:8}}, line:{{color:'#22c55e',width:2}}, yaxis:'y2'}},
    ], {{...LAYOUT, title:'Holding Period Analysis', height:340,
      xaxis:{{...LAYOUT.xaxis, type:'category'}},
      yaxis:{{...LAYOUT.yaxis, title:'# Trades'}},
      yaxis2:{{...LAYOUT.yaxis, title:'Mean Return %', overlaying:'y', side:'right', gridcolor:'transparent'}},
      legend:{{x:0, y:1.12, orientation:'h'}}}}, CFG);
  }}
}};

// ═══════════════════════════════════════════
// EARNINGS & ARCHETYPES
// ═══════════════════════════════════════════
render.earnings = function() {{
  const el = document.getElementById('earnings');
  const earn = D.experiments.find(e=>e.name.includes('Earnings') && e.name.includes('Default') && !e.name.includes('Non'));
  const other = D.experiments.find(e=>e.name.includes('Non-Earnings') && e.name.includes('Default'));
  const earnCEM = D.experiments.find(e=>e.name.includes('Earnings') && e.name.includes('CEM') && !e.name.includes('Non'));
  const otherCEM = D.experiments.find(e=>e.name.includes('Non-Earnings') && e.name.includes('CEM'));

  function compRow(label, fn) {{
    return `<tr><td style="font-weight:600">${{label}}</td>
      <td>${{fn(earn?.splits.val)}}</td><td>${{fn(other?.splits.val)}}</td>
      <td>${{fn(earnCEM?.splits.val)}}</td><td>${{fn(otherCEM?.splits.val)}}</td></tr>`;
  }}

  let html = `<h2>Earnings vs Non-Earnings Comparison <span class="badge">Validation</span></h2>
  <div class="card"><table><thead><tr>
    <th>Metric</th>
    <th><span class="tag tr">Earnings Default</span></th>
    <th><span class="tag tp">Non-Earn Default</span></th>
    <th><span class="tag tr">Earnings CEM</span></th>
    <th><span class="tag tp">Non-Earn CEM</span></th>
  </tr></thead><tbody>`;
  html += compRow('Trades', m => m?.n || 0);
  html += compRow('Mean Return', m => fmtPct(m?.mean_ret||0));
  html += compRow('Win Rate', m => (m?.win_pct||0)+'%');
  html += compRow('Sharpe', m => fmtVal(m?.sharpe||0));
  html += compRow('Sortino', m => fmtVal(m?.sortino||0));
  html += compRow('Max DD', m => fmtPct(m?.max_dd||0));
  html += compRow('Excess SPY', m => fmtPct(m?.excess_spy||0));
  html += compRow('Excess QQQ', m => fmtPct(m?.excess_qqq||0));
  html += compRow('Avg Hold', m => (m?.avg_hold||0)+'d');
  html += compRow('Profit Factor', m => (m?.profit_factor||0));
  html += `</tbody></table></div>`;

  html += `<div class="grid g2">
    <div class="chart-box" id="earn-ret-dist"></div>
    <div class="chart-box" id="earn-compare-bar"></div>
  </div>`;

  // Archetype deep-dive with worlds
  html += `<h2>Archetype Deep-Dive <span class="badge">Polymarket → LLM World → Trade</span></h2>`;
  html += `<div class="info-box"><h4>How archetypes work</h4>
    Each Polymarket question is analyzed by an LLM that builds a "world" — mapping the event to affected US equities.
    The <strong>relevance score</strong> = question_relevance × connection_strength. Only trades with relevance ≥ 0.50 are taken.
    Click any archetype to see the full Polymarket question, LLM reasoning, and world mapping.</div>`;

  const archs = D.archetype_stats || [];
  const worlds = D.worlds || {{}};
  html += `<div class="card"><table><thead><tr>
    <th>Archetype</th><th>N</th><th>Mean Ret</th><th>Win%</th><th>vs SPY</th><th>Hold</th><th>Polymarket Question</th>
  </tr></thead><tbody>`;
  for(const a of archs) {{
    // Find a matching world entry
    const matchingTrades = (other?.trades||[]).filter(t => t.archetype === a.archetype);
    const sampleTrade = matchingTrades[0];
    const world = sampleTrade ? worlds[sampleTrade.market_id] : null;
    const question = world?.question || 'N/A';
    const qShort = question.length > 80 ? question.slice(0,77)+'...' : question;
    html += `<tr class="clickable" onclick="this.nextElementSibling?.classList.toggle('open')">
      <td style="font-weight:600">${{a.archetype}}</td><td>${{a.n}}</td>
      <td>${{fmtPct(a.mean_ret)}}</td><td>${{a.win_pct}}%</td>
      <td>${{fmtPct(a.excess_spy)}}</td><td>${{a.avg_hold}}d</td>
      <td style="color:var(--text2);font-size:11px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{qShort}}</td>
    </tr>`;
    // Expandable detail row
    if(world) {{
      html += `<tr class="timeline-exp" style="display:none"><td colspan="7" style="padding:12px 16px">
        <div style="margin-bottom:8px"><strong style="color:var(--cyan)">Polymarket Question:</strong> <span style="color:var(--text)">${{question}}</span></div>
        <div style="margin-bottom:8px"><strong style="color:var(--cyan)">LLM World Mapping:</strong></div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px">`;
      for(const asset of (world.assets||[]).slice(0,8)) {{
        html += `<div style="background:var(--surface2);padding:8px 12px;border-radius:6px;font-size:11px">
          <strong style="color:var(--text)">${{asset.symbol}}</strong>
          <span class="tag ${{asset.relevance >= 0.7 ? 'tg' : asset.relevance >= 0.4 ? 'ty' : 'tr'}}" style="margin-left:6px">
            ${{(asset.relevance*100).toFixed(0)}}% relevant
          </span>
          <div style="color:var(--text2);margin-top:4px">${{asset.reason}}</div>
        </div>`;
      }}
      html += `</div></td></tr>`;
    }} else {{
      html += `<tr style="display:none"><td colspan="7"></td></tr>`;
    }}
  }}
  html += `</tbody></table></div>`;
  el.innerHTML = html;

  // Charts
  const earnTrades = (earn?.trades||[]).map(t=>t.return_pct);
  const otherTrades = (other?.trades||[]).map(t=>t.return_pct);
  Plotly.newPlot('earn-ret-dist', [
    {{x:earnTrades, type:'histogram', name:'Earnings', opacity:.6, marker:{{color:'#ef4444'}}, nbinsx:30}},
    {{x:otherTrades, type:'histogram', name:'Non-Earnings', opacity:.6, marker:{{color:'#a855f7'}}, nbinsx:30}},
  ], {{...LAYOUT, title:'Return Distribution: Earnings vs Non-Earnings', barmode:'overlay', height:350,
    xaxis:{{...LAYOUT.xaxis, type:'linear', title:'Return %'}}, yaxis:{{...LAYOUT.yaxis, title:'Count'}}}}, CFG);

  const labels = ['Mean Ret', 'Win%/10', 'Sharpe', 'Excess SPY'];
  Plotly.newPlot('earn-compare-bar', [
    {{x:labels, y:[earn?.splits.val?.mean_ret||0, (earn?.splits.val?.win_pct||0)/10, earn?.splits.val?.sharpe||0, earn?.splits.val?.excess_spy||0],
      type:'bar', name:'Earnings', marker:{{color:'#ef4444'}}}},
    {{x:labels, y:[other?.splits.val?.mean_ret||0, (other?.splits.val?.win_pct||0)/10, other?.splits.val?.sharpe||0, other?.splits.val?.excess_spy||0],
      type:'bar', name:'Non-Earnings', marker:{{color:'#a855f7'}}}},
  ], {{...LAYOUT, title:'Key Metrics Comparison (Val)', barmode:'group', height:350,
    xaxis:{{...LAYOUT.xaxis, type:'category'}}}}, CFG);
}};

// ═══════════════════════════════════════════
// PORTFOLIO $100K
// ═══════════════════════════════════════════
render.portfolio = function() {{
  const el = document.getElementById('portfolio');
  const ports = D.portfolio;
  const bench = D.port_benchmark || [];

  let html = `<h2>Portfolio Simulation — $100,000 Starting Capital</h2>
  <div class="grid g4" style="margin-bottom:20px">`;
  for(const p of ports) {{
    const s = p.stats;
    const rc = s.total_return > 0 ? 'pos' : 'neg';
    html += `<div class="card">
      <h3>${{p.name}}</h3>
      <div class="val ${{rc}}">${{s.total_return > 0 ? '+' : ''}}${{s.total_return.toFixed(2)}}%</div>
      <div class="sub">${{fmtMoney(s.final)}} final &nbsp;|&nbsp; MaxDD: ${{s.max_dd.toFixed(1)}}%</div>
      <div class="sub">${{s.n_trades}} trades &nbsp;|&nbsp; Win: ${{s.win_rate}}% &nbsp;|&nbsp; Avg P&L: ${{fmtMoney(s.avg_pnl)}}</div>
    </div>`;
  }}
  html += `</div>`;

  html += `<div class="chart-box" id="port-equity"></div>`;
  html += `<div class="grid g2">
    <div class="chart-box" id="port-drawdown"></div>
    <div class="chart-box" id="port-pnl-dist"></div>
  </div>`;

  // Policy table
  html += `<h2>Portfolio Policy Parameters</h2>
  <div class="card"><table><thead><tr>
    <th>Portfolio</th><th>Pos Size</th><th>Max Conc</th><th>ATR Mult</th>
    <th>θ Out</th><th>Enter Strong</th><th>Hold Days</th>
  </tr></thead><tbody>`;
  for(const p of ports) {{
    const pol = p.policy;
    html += `<tr><td style="color:${{p.color}};font-weight:600">${{p.name}}</td>
      <td>${{(pol.position_size_pct*100).toFixed(1)}}%</td><td>${{pol.max_concurrent}}</td>
      <td>${{pol.atr_mult?.toFixed(2)||'-'}}</td><td>${{pol.theta_out?.toFixed(3)||'-'}}</td>
      <td>${{pol.enter_strong?.toFixed(3)||'-'}}</td><td>${{pol.hold_days||'-'}}</td></tr>`;
  }}
  html += `</tbody></table></div>`;

  // Split breakdown
  html += `<h2>Per-Split Performance</h2>
  <div class="card"><table><thead><tr>
    <th>Portfolio</th><th>Split</th><th>N</th><th>Mean Ret</th><th>Win%</th><th>Sharpe</th>
  </tr></thead><tbody>`;
  for(const p of ports) {{
    for(const sp of ['train','val','test']) {{
      const m = p.splits[sp];
      if(!m || !m.n) continue;
      html += `<tr><td>${{p.name}}</td><td><span class="tag ty">${{sp}}</span></td><td>${{m.n}}</td>
        <td>${{fmtPct(m.mean_ret)}}</td><td>${{m.win_pct}}%</td><td class="${{riskColor('sharpe',m.sharpe)}}">${{fmtVal(m.sharpe)}}</td></tr>`;
    }}
  }}
  html += `</tbody></table></div>`;
  el.innerHTML = html;

  // Equity curve with benchmarks
  const eqTraces = ports.filter(p=>p.equity.length>0).map(p => ({{
    x:p.equity.map(e=>e.date), y:p.equity.map(e=>e.equity),
    name:p.name, mode:'lines', line:{{color:p.color, width:2}},
  }}));
  // Add SPY/QQQ benchmarks
  if(bench.length > 0) {{
    eqTraces.push({{
      x:bench.map(b=>b.date), y:bench.map(b=>100000*(1+b.spy_ret/100)),
      name:'SPY Buy & Hold', mode:'lines', line:{{color:'#8899ac', width:1.5, dash:'dash'}},
    }});
    eqTraces.push({{
      x:bench.map(b=>b.date), y:bench.map(b=>100000*(1+b.qqq_ret/100)),
      name:'QQQ Buy & Hold', mode:'lines', line:{{color:'#f97316', width:1.5, dash:'dot'}},
    }});
  }}
  eqTraces.push({{
    x:eqTraces[0]?.x||[], y:(eqTraces[0]?.x||[]).map(()=>100000),
    name:'Starting Capital', mode:'lines', line:{{color:'#5a6b7f', dash:'dash', width:1}},
  }});
  Plotly.newPlot('port-equity', eqTraces, {{...LAYOUT, title:'Portfolio Equity Curves vs Benchmarks',
    height:420, yaxis:{{...LAYOUT.yaxis, title:'Equity ($)', tickformat:'$,.0f'}},
    legend:{{x:0, y:1.15, orientation:'h', font:{{size:10}}}}}}, CFG);

  // Drawdown
  const ddTraces = ports.filter(p=>p.equity.length>0).map(p => {{
    const eq = p.equity.map(e=>e.equity);
    let peak = eq[0] || 100000;
    const dd = eq.map(v => {{ peak = Math.max(peak, v); return (v - peak) / peak * 100; }});
    return {{x:p.equity.map(e=>e.date), y:dd, name:p.name,
      mode:'lines', line:{{color:p.color, width:2}}, fill:'tozeroy',
      fillcolor:p.color + '10'}};
  }});
  Plotly.newPlot('port-drawdown', ddTraces, {{...LAYOUT, title:'Drawdown from Peak',
    height:320, yaxis:{{...LAYOUT.yaxis, title:'Drawdown %'}}}}, CFG);

  // P&L distribution
  const allPnl = ports[0]?.trades?.map(t=>t.return_pct) || [];
  Plotly.newPlot('port-pnl-dist', [
    {{x:allPnl, type:'histogram', nbinsx:40, marker:{{color:'#3b82f6', opacity:.7}}, name:'Trade Returns'}},
  ], {{...LAYOUT, title:'Trade Return Distribution (Default Portfolio)', height:320,
    xaxis:{{...LAYOUT.xaxis, type:'linear', title:'Return %'}}, yaxis:{{...LAYOUT.yaxis, title:'Count'}},
    shapes:[{{type:'line',x0:0,x1:0,y0:0,y1:1,yref:'paper',line:{{color:'#ef4444',width:1.5,dash:'dash'}}}}]}}, CFG);
}};

// ═══════════════════════════════════════════
// RISK METRICS
// ═══════════════════════════════════════════
render.risk = function() {{
  const el = document.getElementById('risk');
  const exps = D.experiments;

  // Explanations panel
  let html = `<h2>Risk Metrics Explained</h2>
  <div class="info-box">
    <h4>Understanding the colors: <span class="metric-good">■ Good</span> &nbsp; <span class="metric-warn">■ Caution</span> &nbsp; <span class="metric-bad">■ Concerning</span></h4>
    <dl class="metric-explain">`;
  for(const [key, r] of Object.entries(RISK_EXPLAIN)) {{
    html += `<dt>${{r.name}}</dt><dd>${{r.desc}}</dd>`;
  }}
  html += `</dl></div>`;

  // Risk table with strategy selector
  html += `<h2>Full Risk Breakdown <span class="badge">Color-coded</span></h2>
  <div class="filter-bar">
    <select id="risk-split" onchange="renderRiskTable()">
      <option value="val">Validation Set</option>
      <option value="test">Test Set</option>
      <option value="train">Train Set</option>
    </select>
  </div>
  <div class="card" id="risk-table-wrap" style="overflow-x:auto"></div>`;

  // Charts
  html += `<div class="grid g2">
    <div class="chart-box" id="risk-box"></div>
    <div class="chart-box" id="risk-var"></div>
  </div>`;

  // Best/worst trades with strategy selector
  html += `<h2>Best & Worst Trades</h2>
  <div class="filter-bar">
    <select id="bw-exp" onchange="renderBestWorst()">`;
  for(let i=0; i<exps.length; i++) {{
    html += `<option value="${{i}}">${{exps[i].name}}</option>`;
  }}
  html += `</select></div>
  <div class="grid g2" id="bw-container"></div>`;
  el.innerHTML = html;

  window.renderRiskTable = function() {{
    const sp = document.getElementById('risk-split').value;
    let t = `<table><thead><tr>
      <th>Experiment</th><th>N</th><th>Mean</th><th>Median</th><th>Std</th>
      <th title="${{RISK_EXPLAIN.sharpe.desc}}">Sharpe ℹ</th>
      <th title="${{RISK_EXPLAIN.sortino.desc}}">Sortino ℹ</th>
      <th title="${{RISK_EXPLAIN.max_dd.desc}}">Max DD ℹ</th>
      <th title="${{RISK_EXPLAIN.calmar.desc}}">Calmar ℹ</th>
      <th title="${{RISK_EXPLAIN.var_95.desc}}">VaR 95% ℹ</th>
      <th title="${{RISK_EXPLAIN.cvar_95.desc}}">CVaR 95% ℹ</th>
      <th title="${{RISK_EXPLAIN.profit_factor.desc}}">PF ℹ</th>
      <th>Avg Win</th><th>Avg Loss</th><th>Max Win</th><th>Max Loss</th>
    </tr></thead><tbody>`;
    for(const e of exps) {{
      const v = e.splits[sp];
      if(!v || !v.n) continue;
      t += `<tr><td style="white-space:nowrap"><span style="color:${{e.color}}">●</span> ${{e.name}}</td>
        <td>${{v.n}}</td><td>${{fmtPct(v.mean_ret)}}</td><td>${{fmtPct(v.median_ret)}}</td><td>${{v.std_ret}}</td>
        <td class="${{riskColor('sharpe',v.sharpe)}}" style="font-weight:600">${{fmtVal(v.sharpe)}}</td>
        <td class="${{riskColor('sortino',v.sortino)}}" style="font-weight:600">${{fmtVal(v.sortino)}}</td>
        <td class="${{riskColor('max_dd',v.max_dd)}}">${{fmtPct(v.max_dd)}}</td>
        <td class="${{riskColor('calmar',v.calmar)}}">${{fmtVal(v.calmar)}}</td>
        <td class="${{riskColor('var_95',v.var_95)}}">${{fmtPct(v.var_95)}}</td>
        <td class="${{riskColor('cvar_95',v.cvar_95)}}">${{fmtPct(v.cvar_95)}}</td>
        <td class="${{riskColor('profit_factor',v.profit_factor)}}">${{v.profit_factor}}</td>
        <td>${{fmtPct(v.avg_win)}}</td><td>${{fmtPct(v.avg_loss)}}</td>
        <td>${{fmtPct(v.max_win)}}</td><td>${{fmtPct(v.max_loss)}}</td></tr>`;
    }}
    t += `</tbody></table>`;
    document.getElementById('risk-table-wrap').innerHTML = t;
  }};
  renderRiskTable();

  // Best/Worst trades
  window.renderBestWorst = function() {{
    const idx = parseInt(document.getElementById('bw-exp').value);
    const trades = exps[idx]?.trades || [];
    const sorted = [...trades].sort((a,b) => a.return_pct - b.return_pct);

    function tradeRow(t) {{
      return `<tr class="clickable" onclick="showTradeDetail('${{t.symbol}}','${{t.market_id}}','${{t.entry_date}}')">
        <td><strong>${{t.symbol}}</strong></td><td>${{fmtPct(t.return_pct)}}</td><td>${{t.holding_days}}d</td>
        <td>${{fmtPct(t.excess_spy)}}</td><td>${{fmtPct(t.excess_qqq)}}</td>
        <td><span class="tag tb">${{(t.exit_reason||'').slice(0,18)}}</span></td>
        <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px">${{t.archetype}}</td></tr>`;
    }}

    let bw = `<div class="card"><h3>Worst 15 Trades — ${{exps[idx].name}} <span style="font-weight:400;color:var(--text2)">(click for details)</span></h3>
      <table><thead><tr><th>Symbol</th><th>Return</th><th>Hold</th><th>vs SPY</th><th>vs QQQ</th><th>Exit</th><th>Archetype</th></tr></thead><tbody>`;
    for(const t of sorted.slice(0,15)) bw += tradeRow(t);
    bw += `</tbody></table></div>`;

    bw += `<div class="card"><h3>Best 15 Trades — ${{exps[idx].name}}</h3>
      <table><thead><tr><th>Symbol</th><th>Return</th><th>Hold</th><th>vs SPY</th><th>vs QQQ</th><th>Exit</th><th>Archetype</th></tr></thead><tbody>`;
    for(const t of sorted.slice(-15).reverse()) bw += tradeRow(t);
    bw += `</tbody></table></div>`;
    document.getElementById('bw-container').innerHTML = bw;
  }};
  renderBestWorst();

  // Box plots
  Plotly.newPlot('risk-box', exps.map(e => ({{
    y:e.trades.map(t=>t.return_pct), type:'box', name:e.name.split(' — ')[0],
    marker:{{color:e.color}}, boxpoints:'outliers',
  }})), {{...LAYOUT, title:'Return Distribution by Experiment', height:380,
    showlegend:false, xaxis:{{...LAYOUT.xaxis, type:'category', tickangle:-20, tickfont:{{size:9}}}}}}, CFG);

  // VaR comparison
  Plotly.newPlot('risk-var', [
    {{x:exps.map(e=>e.name), y:exps.map(e=>e.splits.val?.var_95||0), type:'bar', name:'VaR 95%', marker:{{color:'#eab308'}}}},
    {{x:exps.map(e=>e.name), y:exps.map(e=>e.splits.val?.cvar_95||0), type:'bar', name:'CVaR 95%', marker:{{color:'#ef4444'}}}},
  ], {{...LAYOUT, title:'Value at Risk (Validation)', barmode:'group', height:380,
    xaxis:{{...LAYOUT.xaxis, type:'category', tickangle:-25, tickfont:{{size:9}}}},
    yaxis:{{...LAYOUT.yaxis, title:'Return %'}}}}, CFG);
}};

// ═══════════════════════════════════════════
// RF MODEL
// ═══════════════════════════════════════════
render.rf = function() {{
  const el = document.getElementById('rf');
  const rfm = D.rf_metrics;
  const fi = D.feature_importance;

  let html = `<h2>Random Forest Model</h2>
  <div class="info-box">
    <h4>What is the RF Model?</h4>
    <p>A <strong>Random Forest</strong> is a machine learning ensemble of 100 decision trees, each trained on 7 lean features
    (debt-to-equity, 2-week trend, time-to-resolution, market cap, SPY trend, profit margin, probability at trigger).</p>
    <p style="margin-top:8px">Its job is <strong>trade selection</strong> — predicting which candidates will have positive returns.
    When RF prediction > 0, the trade is taken; otherwise it's filtered out. <strong>The RF does NOT pick direction</strong>
    (that's harmful, −14pp OOS vs long-the-beneficiary). Its value is filtering unpromising setups.</p>
    <p style="margin-top:8px"><strong>Key insight:</strong> A correlation near 0 and direction accuracy near 50% on OOS
    means the model has <em>no predictive power</em> — it's essentially random. This is expected; the experiment log
    documents that per-trade prediction is a coin flip for this dataset.</p>
  </div>`;

  // Score cards
  html += `<div class="grid g3" style="margin-bottom:20px">`;
  for(const sp of ['train','val','test']) {{
    const m = rfm[sp];
    if(!m) continue;
    const corrClass = Math.abs(m.corr) > 0.3 ? (m.corr > 0 ? 'metric-good' : 'metric-bad') : 'metric-warn';
    const dirClass = m.dir_acc > 0.55 ? 'metric-good' : m.dir_acc > 0.50 ? 'metric-warn' : 'metric-bad';
    html += `<div class="card">
      <h3>RF ${{sp.toUpperCase()}}</h3>
      <div class="val ${{corrClass}}">${{m.corr > 0 ? '+' : ''}}${{m.corr.toFixed(3)}}</div>
      <div class="sub">Correlation (actual vs predicted)</div>
      <div style="margin-top:8px">
        <span class="tag ${{dirClass === 'metric-good' ? 'tg' : dirClass === 'metric-warn' ? 'ty' : 'tr'}}">
          Dir Accuracy: ${{(m.dir_acc*100).toFixed(1)}}%</span>
        <span style="color:var(--text3);font-size:11px;margin-left:6px">(50% = random)</span>
      </div>
      <div class="sub">n = ${{m.n}} candidates</div>
    </div>`;
  }}
  html += `</div>`;

  html += `<div class="grid g2">
    <div class="chart-box" id="rf-fi"></div>
    <div class="chart-box" id="rf-compare"></div>
  </div>`;

  // RF impact table
  const noRF = D.experiments.find(e=>e.name==='All — Default');
  const withRF = D.experiments.find(e=>e.name==='All RF>0 — Default');
  html += `<h2>RF Filter Impact <span class="badge">Does filtering help?</span></h2>
  <div class="card"><table><thead><tr>
    <th>Split</th><th colspan="4" style="text-align:center;color:var(--text2)">Without RF</th>
    <th colspan="4" style="text-align:center;color:var(--green)">With RF (pred > 0)</th>
    <th>Δ Mean Ret</th>
  </tr><tr><th></th><th>N</th><th>Mean Ret</th><th>Win%</th><th>Sharpe</th>
    <th>N</th><th>Mean Ret</th><th>Win%</th><th>Sharpe</th><th></th></tr></thead><tbody>`;
  for(const sp of ['train','val','test']) {{
    const a = noRF?.splits[sp] || {{}};
    const b = withRF?.splits[sp] || {{}};
    const delta = (b.mean_ret||0) - (a.mean_ret||0);
    html += `<tr><td><span class="tag ty">${{sp}}</span></td>
      <td>${{a.n||0}}</td><td>${{fmtPct(a.mean_ret||0)}}</td><td>${{(a.win_pct||0)}}%</td><td>${{fmtVal(a.sharpe||0)}}</td>
      <td>${{b.n||0}}</td><td>${{fmtPct(b.mean_ret||0)}}</td><td>${{(b.win_pct||0)}}%</td><td>${{fmtVal(b.sharpe||0)}}</td>
      <td style="font-weight:600">${{fmtPct(delta)}}</td></tr>`;
  }}
  html += `</tbody></table></div>`;

  // Feature list
  html += `<div class="card" style="margin-top:16px"><h3>Features Used (7 Lean Features)</h3>
    <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:10px">`;
  const featureNames = {{
    'feat_debt_to_equity': 'Debt-to-Equity Ratio',
    'feat_asset_2w_trend': 'Asset 2-Week Price Trend',
    'feat_time_to_resolution_days': 'Days Until Market Resolution',
    'feat_log_market_cap': 'Log Market Cap',
    'feat_spy_2w_trend': 'SPY 2-Week Trend',
    'feat_profit_margin': 'Company Profit Margin',
    'feat_prob_at_trigger': 'Probability at Trigger',
  }};
  for(const f of Object.keys(fi)) {{
    const label = featureNames[f] || f.replace('feat_','');
    html += `<span class="pill">${{label}} <span class="tag tb">${{(fi[f]*100).toFixed(1)}}%</span></span>`;
  }}
  html += `</div></div>`;
  el.innerHTML = html;

  // Feature importance chart
  const fNames = Object.keys(fi).map(f => featureNames[f] || f.replace('feat_',''));
  const fVals = Object.values(fi);
  const sorted = fNames.map((n,i) => [n, fVals[i]]).sort((a,b) => a[1]-b[1]);
  Plotly.newPlot('rf-fi', [
    {{y:sorted.map(s=>s[0]), x:sorted.map(s=>s[1]), type:'bar', orientation:'h',
      marker:{{color:sorted.map(s=>s[1]), colorscale:[['0','#243044'],['1','#3b82f6']]}}}},
  ], {{...LAYOUT, title:'Feature Importance (Gini)', height:300,
    xaxis:{{...LAYOUT.xaxis, type:'linear', title:'Importance', tickformat:'.1%'}},
    yaxis:{{...LAYOUT.yaxis, type:'category'}},
    margin:{{...LAYOUT.margin, l:200}}}}, CFG);

  // RF comparison bar
  const splits = ['train','val','test'];
  Plotly.newPlot('rf-compare', [
    {{x:splits.map(s=>s.toUpperCase()), y:splits.map(s=>noRF?.splits[s]?.mean_ret||0), type:'bar',
      name:'No RF', marker:{{color:'#3b82f6', opacity:.7}}}},
    {{x:splits.map(s=>s.toUpperCase()), y:splits.map(s=>withRF?.splits[s]?.mean_ret||0), type:'bar',
      name:'With RF>0', marker:{{color:'#22c55e', opacity:.7}}}},
  ], {{...LAYOUT, title:'Mean Return: RF Filter Impact by Split', barmode:'group', height:300,
    xaxis:{{...LAYOUT.xaxis, type:'category'}}}}, CFG);
}};

// ═══════════════════════════════════════════
// TRADE EXPLORER (with pagination)
// ═══════════════════════════════════════════
render.trades = function() {{
  const el = document.getElementById('trades');
  const allExp = D.experiments;

  let html = `<div class="filter-bar">
    <select id="t-exp" onchange="filterTrades()">
      <option value="all">All Experiments</option>`;
  for(let i=0; i<allExp.length; i++) {{
    html += `<option value="${{i}}">${{allExp[i].name}}</option>`;
  }}
  html += `</select>
    <select id="t-split" onchange="filterTrades()">
      <option value="all">All Splits</option>
      <option value="train">Train</option><option value="val">Val</option><option value="test">Test</option>
    </select>
    <input type="text" id="t-search" placeholder="Search symbol or archetype..." oninput="filterTrades()">
    <span id="t-count" style="color:var(--text2);font-size:12px"></span>
  </div>`;

  html += `<div class="card"><table id="trades-table"><thead><tr>
    <th onclick="sortTrades('symbol')">Symbol ↕</th>
    <th onclick="sortTrades('return_pct')">Return % ↕</th>
    <th onclick="sortTrades('holding_days')">Hold ↕</th>
    <th onclick="sortTrades('excess_spy')">vs SPY ↕</th>
    <th onclick="sortTrades('excess_qqq')">vs QQQ ↕</th>
    <th onclick="sortTrades('entry_date')">Entry ↕</th>
    <th onclick="sortTrades('exit_date')">Exit ↕</th>
    <th>Exit Reason</th>
    <th onclick="sortTrades('split')">Split ↕</th>
    <th>Archetype</th>
    <th onclick="sortTrades('relevance')">Relevance ↕</th>
    <th onclick="sortTrades('entry_price')">Entry $</th>
    <th onclick="sortTrades('exit_price')">Exit $</th>
  </tr></thead><tbody id="trades-body"></tbody></table></div>`;

  html += `<div class="pagination" id="trades-pagination"></div>`;
  html += `<div class="chart-box" id="trades-scatter" style="margin-top:16px"></div>`;
  el.innerHTML = html;

  window._tPage = 1;
  window._tPageSize = 50;
  window._sortCol = 'return_pct';
  window._sortDir = -1;
  filterTrades();

  const t0 = allExp[0]?.trades || [];
  Plotly.newPlot('trades-scatter', [{{
    x:t0.map(t=>t.holding_days), y:t0.map(t=>t.return_pct),
    text:t0.map(t=>`${{t.symbol}} (${{t.archetype?.slice(0,25)||''}}) ${{t.return_pct > 0 ? '+' : ''}}${{t.return_pct.toFixed(1)}}%`),
    mode:'markers', marker:{{
      size:7, color:t0.map(t=>t.return_pct),
      colorscale:[['0','#ef4444'],['0.5','#5a6b7f'],['1','#22c55e']],
      cmin:-20, cmax:20, colorbar:{{title:'Return %', tickfont:{{color:'#8899ac',size:10}}, len:.8}},
      line:{{width:.5, color:'rgba(255,255,255,.15)'}},
    }},
    hovertemplate:'<b>%{{text}}</b><br>Hold: %{{x}}d<br>Return: %{{y:.2f}}%<extra></extra>',
  }}], {{...LAYOUT, title:'Return vs Holding Days (All Default)', height:400,
    xaxis:{{...LAYOUT.xaxis, title:'Holding Days'}},
    yaxis:{{...LAYOUT.yaxis, title:'Return %'}}}}, CFG);
}};

function filterTrades() {{
  const expIdx = document.getElementById('t-exp').value;
  const split = document.getElementById('t-split').value;
  const search = document.getElementById('t-search').value.toLowerCase();
  const allExp = D.experiments;
  let trades = expIdx === 'all' ? (allExp[0]?.trades||[]) : (allExp[parseInt(expIdx)]?.trades||[]);
  if(split !== 'all') trades = trades.filter(t => t.split === split);
  if(search) trades = trades.filter(t => t.symbol.toLowerCase().includes(search) || (t.archetype||'').toLowerCase().includes(search));
  trades.sort((a,b) => {{
    const av = a[window._sortCol], bv = b[window._sortCol];
    if(typeof av === 'string') return window._sortDir * av.localeCompare(bv);
    return window._sortDir * ((av||0) - (bv||0));
  }});
  window._filteredTrades = trades;
  window._tPage = 1;
  renderTradesPage();
}}

function renderTradesPage() {{
  const trades = window._filteredTrades || [];
  const page = window._tPage;
  const ps = window._tPageSize;
  const totalPages = Math.max(1, Math.ceil(trades.length / ps));
  const start = (page - 1) * ps;
  const end = Math.min(start + ps, trades.length);
  const slice = trades.slice(start, end);

  const body = document.getElementById('trades-body');
  body.innerHTML = slice.map(t => `<tr class="clickable" onclick="showTradeDetail('${{t.symbol}}','${{t.market_id}}','${{t.entry_date}}')">
    <td><strong>${{t.symbol}}</strong></td>
    <td>${{fmtPct(t.return_pct)}}</td><td>${{t.holding_days}}d</td>
    <td>${{fmtPct(t.excess_spy)}}</td><td>${{fmtPct(t.excess_qqq)}}</td>
    <td style="font-size:11px">${{t.entry_date}}</td><td style="font-size:11px">${{t.exit_date}}</td>
    <td><span class="tag tb">${{(t.exit_reason||'').slice(0,20)}}</span></td>
    <td><span class="tag ty">${{t.split}}</span></td>
    <td style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px">${{t.archetype||''}}</td>
    <td>${{t.relevance||''}}</td>
    <td>${{t.entry_price}}</td><td>${{t.exit_price}}</td>
  </tr>`).join('');

  document.getElementById('t-count').textContent = `${{trades.length}} trades — page ${{page}} of ${{totalPages}}`;

  // Pagination controls
  let pag = '';
  pag += `<button onclick="goPage(${{page-1}})" ${{page<=1?'disabled':''}}>← Prev</button>`;
  const maxBtns = 7;
  let startP = Math.max(1, page - 3);
  let endP = Math.min(totalPages, startP + maxBtns - 1);
  if(endP - startP < maxBtns - 1) startP = Math.max(1, endP - maxBtns + 1);
  if(startP > 1) pag += `<button onclick="goPage(1)">1</button><span>...</span>`;
  for(let i=startP; i<=endP; i++) {{
    pag += `<button onclick="goPage(${{i}})" class="${{i===page?'active':''}}">${{i}}</button>`;
  }}
  if(endP < totalPages) pag += `<span>...</span><button onclick="goPage(${{totalPages}})">${{totalPages}}</button>`;
  pag += `<button onclick="goPage(${{page+1}})" ${{page>=totalPages?'disabled':''}}>Next →</button>`;
  document.getElementById('trades-pagination').innerHTML = pag;
}}

function goPage(p) {{
  const totalPages = Math.max(1, Math.ceil((window._filteredTrades||[]).length / window._tPageSize));
  window._tPage = Math.max(1, Math.min(p, totalPages));
  renderTradesPage();
}}

function sortTrades(col) {{
  if(window._sortCol === col) window._sortDir *= -1;
  else {{ window._sortCol = col; window._sortDir = -1; }}
  filterTrades();
}}

// ═══════════════════════════════════════════
// TRADE DETAIL MODAL
// ═══════════════════════════════════════════
function showTradeDetail(sym, mkt, entryDate) {{
  const key = `${{sym}}_${{mkt}}_${{entryDate}}`;
  const path = D.price_paths?.[key];
  const world = D.worlds?.[mkt];

  // Find the trade object
  const allTrades = D.experiments[0]?.trades || [];
  const trade = allTrades.find(t => t.symbol === sym && t.market_id === mkt && t.entry_date === entryDate);
  if(!trade) return;

  const mc = document.getElementById('modal-content');
  let html = `<h3 style="display:flex;align-items:center;gap:10px">
    <span style="font-size:22px">${{sym}}</span>
    <span class="tag ${{trade.return_pct > 0 ? 'tg' : 'tr'}}" style="font-size:13px">${{trade.return_pct > 0 ? '+' : ''}}${{trade.return_pct.toFixed(2)}}%</span>
    <span style="color:var(--text2);font-size:13px;font-weight:400">${{trade.archetype}}</span>
  </h3>`;

  // Trade metadata
  html += `<div class="grid g5" style="margin:16px 0">
    <div class="card"><h3>Entry</h3><div style="font-size:14px;font-weight:600">$${{trade.entry_price}}</div><div class="sub">${{trade.entry_date}}</div></div>
    <div class="card"><h3>Exit</h3><div style="font-size:14px;font-weight:600">$${{trade.exit_price}}</div><div class="sub">${{trade.exit_date}} — ${{trade.exit_reason}}</div></div>
    <div class="card"><h3>vs Benchmarks</h3><div style="font-size:13px">SPY: ${{fmtPct(trade.excess_spy)}} &nbsp; QQQ: ${{fmtPct(trade.excess_qqq)}}</div></div>
    <div class="card"><h3>Details</h3><div style="font-size:12px">Hold: ${{trade.holding_days}}d &nbsp; Peak: ${{trade.peak_pct}}% &nbsp; Trough: ${{trade.trough_pct}}%</div></div>
    <div class="card"><h3>Confidence</h3><div class="val" style="font-size:22px">${{(trade.entry_prob*100).toFixed(1)}}%</div><div class="sub">Polymarket Prob</div></div>
  </div>`;

  // World info
  if(world) {{
    html += `<div class="info-box" style="margin-bottom:16px">
      <h4>Polymarket Question</h4><p style="color:var(--text)">${{world.question}}</p>
      <div style="margin-top:8px"><strong style="color:var(--cyan)">LLM World Mapping:</strong></div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">`;
    for(const a of (world.assets||[]).slice(0,6)) {{
      html += `<span class="pill">${{a.symbol}} <span class="tag ${{a.relevance >= 0.7 ? 'tg' : 'ty'}}">${{(a.relevance*100).toFixed(0)}}%</span></span>`;
    }}
    html += `</div></div>`;
  }}

  // Price chart
  html += `<div id="modal-chart" style="height:350px"></div>`;
  mc.innerHTML = html;
  document.getElementById('trade-modal').classList.add('active');

  // Render chart if path exists
  if(path && path.length > 1) {{
    const traces = [{{
      x:path.map(p=>p.d), y:path.map(p=>p.ret),
      name:sym + ' Return %', mode:'lines+markers',
      line:{{color:'#3b82f6', width:2.5}}, marker:{{size:4}},
      fill:'tozeroy', fillcolor:'rgba(59,130,246,.08)',
    }}];
    if(path[0].spy != null) {{
      traces.push({{
        x:path.map(p=>p.d), y:path.map(p=>p.spy),
        name:'SPY Return %', mode:'lines', line:{{color:'#8899ac', width:1.5, dash:'dash'}},
      }});
    }}
    if(path[0].qqq != null) {{
      traces.push({{
        x:path.map(p=>p.d), y:path.map(p=>p.qqq),
        name:'QQQ Return %', mode:'lines', line:{{color:'#f97316', width:1.5, dash:'dot'}},
      }});
    }}
    if(path[0].prob != null) {{
      traces.push({{
        x:path.map(p=>p.d), y:path.map(p=>(p.prob-0.5)*100),
        name:'Poly Prob (shifted)', mode:'lines', line:{{color:'#a855f7', width:1, dash:'dashdot'}},
        yaxis:'y2',
      }});
    }}
    Plotly.newPlot('modal-chart', traces, {{
      ...LAYOUT, title:`${{sym}} Trade Performance vs Benchmarks`,
      height:340, margin:{{t:40,b:40,l:50,r:50}},
      xaxis:{{...LAYOUT.xaxis, title:'Date'}},
      yaxis:{{...LAYOUT.yaxis, title:'Return %'}},
      yaxis2:{{...LAYOUT.yaxis, title:'Prob Deviation', overlaying:'y', side:'right', showgrid:false}},
      legend:{{x:0, y:1.12, orientation:'h', font:{{size:10}}}},
      shapes:[{{type:'line',x0:path[0].d,x1:path[path.length-1].d,y0:0,y1:0,
        line:{{color:'#5a6b7f',width:1,dash:'dash'}}}}],
    }}, CFG);
  }} else {{
    document.getElementById('modal-chart').innerHTML = '<p style="color:var(--text2);text-align:center;padding:40px">Price path data not available for this trade. Run the dashboard generator to include it.</p>';
  }}
}}

// ═══════════════════════════════════════════
// EXPERIMENT HISTORY
// ═══════════════════════════════════════════
render.explog = function() {{
  const el = document.getElementById('explog');
  const log = D.experiment_log || [];

  let html = `<h2>Experiment History <span class="badge">${{log.reduce((a,p)=>a+p.experiments.length,0)}} experiments across ${{log.length}} phases</span></h2>
  <div class="info-box" style="margin-bottom:20px">
    <h4>Research Timeline</h4>
    <p>Complete record of every experiment run for the information-diffusion-lag strategy.
    Each experiment documents: what was tested, why, the result, and what was learned.
    Click any experiment to expand its details.</p>
  </div>`;

  html += `<div class="timeline">`;
  for(const phase of log) {{
    html += `<div class="timeline-phase"><h3>${{phase.name}}</h3>`;
    for(const exp of phase.experiments) {{
      html += `<div class="timeline-exp" onclick="this.classList.toggle('open')">
        <h4><span class="eid">${{exp.id}}</span> ${{exp.title}}</h4>
        <div class="exp-body">`;

      // Show extracted fields
      const fields = [
        ['what', 'What', 'var(--blue)'],
        ['why', 'Why', 'var(--cyan)'],
        ['data', 'Data', 'var(--text2)'],
        ['changed_from', 'Changed From', 'var(--yellow)'],
        ['result', 'Result', 'var(--green)'],
        ['converged_policy', 'Converged Policy', 'var(--purple)'],
        ['why_it_failed', 'Why It Failed', 'var(--red)'],
        ['why_it_worked', 'Why It Worked', 'var(--green)'],
        ['learned', 'Learned', 'var(--orange)'],
      ];
      for(const [key, label, color] of fields) {{
        if(exp[key]) {{
          // Escape HTML
          const val = exp[key].replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>');
          html += `<div class="exp-field"><strong style="color:${{color}}">${{label}}:</strong> ${{val}}</div>`;
        }}
      }}

      // Fallback: show raw body if no fields extracted
      if(!exp.what && !exp.result && exp.body_text) {{
        const bt = exp.body_text.replace(/</g,'&lt;').replace(/>/g,'&gt;')
          .replace(/\\*\\*(.*?)\\*\\*/g, '<strong>$1</strong>')
          .replace(/\\n/g, '<br>');
        html += `<div style="margin-top:6px">${{bt}}</div>`;
      }}
      html += `</div></div>`;
    }}
    html += `</div>`;
  }}
  html += `</div>`;
  el.innerHTML = html;
}};

// Render overview on load
render.overview();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
