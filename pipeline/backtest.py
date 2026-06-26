"""Backtesting harness: load data, run strategy, report results.

Usage:
    python -m pipeline.backtest                    # default parquet
    python -m pipeline.backtest --from-db          # build from database
    python -m pipeline.backtest --rl               # with CEM optimisation
    python -m pipeline.backtest data/custom.parquet
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from pipeline.data_loader import NUM_FEATURES, CAT_FEATURES, TARGET
from pipeline.strategy import (
    DEFAULT_POLICY,
    run_backtest,
    score_sharpe_per_day,
    score_mean_return,
)
from pipeline.portfolio_manager import (
    cem_search,
    evaluate_splits,
    reward_sharpe_dd,
)

DEFAULT_PARQUET = Path(__file__).resolve().parents[1] / "data" / "candidates.parquet"
RELEVANCE_COL = "feat_connection_strength"


async def load_price_prob_paths(df: pd.DataFrame):
    """Load daily bars and probability paths from DB (same as liran_strategy)."""
    c = await connect()
    try:
        syms = sorted(df["symbol"].unique())
        mkts = sorted(df["market_id"].unique())
        bars = await c.fetch(
            f"SELECT symbol, ts, high, low, close FROM {SCHEMA}.historical_price_bars "
            f"WHERE resolution='1d' AND symbol=ANY($1::text[]) ORDER BY symbol, ts",
            syms,
        )
        prob_rows = await c.fetch(
            f"""SELECT DISTINCT ON (market_id, (hour_ts AT TIME ZONE 'UTC')::date)
                market_id, (hour_ts AT TIME ZONE 'UTC')::date AS d, probability
                FROM {SCHEMA}.historical_probability_points
                WHERE market_id=ANY($1::text[])
                  AND EXTRACT(HOUR FROM hour_ts AT TIME ZONE 'UTC') <= 20
                ORDER BY market_id, (hour_ts AT TIME ZONE 'UTC')::date, hour_ts DESC""",
            mkts,
        )
    finally:
        await c.close()

    prices: dict[str, list[tuple]] = {}
    for b in bars:
        prices.setdefault(b["symbol"], []).append((
            pd.Timestamp(b["ts"]).tz_convert("UTC").normalize(),
            float(b["high"]), float(b["low"]), float(b["close"]),
        ))

    probs: dict[str, list[tuple]] = {}
    for p in prob_rows:
        probs.setdefault(p["market_id"], []).append((
            pd.Timestamp(p["d"]).tz_localize("UTC"),
            float(p["probability"]),
        ))

    for d in (prices, probs):
        for k in d:
            d[k].sort()
    return prices, probs


def print_split_results(tdf: pd.DataFrame):
    """Print per-split summary table."""
    print(f"\n  {'split':5} {'n':>5} {'mean_ret':>10} {'win%':>6} {'median':>8} {'sharpe':>8}")
    print(f"  {'-'*50}")
    for sp in ("train", "val", "test"):
        s = tdf[tdf["split"] == sp] if "split" in tdf.columns else pd.DataFrame()
        if s.empty:
            print(f"  {sp:5}     0  (no trades)")
            continue
        n = len(s)
        mr = s["return_pct"].mean()
        wr = (s["return_pct"] > 0).mean() * 100
        med = s["return_pct"].median()
        sh = score_sharpe_per_day(s)
        print(f"  {sp:5} {n:5d} {mr:+10.2f}% {wr:5.0f}% {med:+8.2f}% {sh:+8.3f}")

    if not tdf.empty:
        print(f"\n  exit reasons: {tdf['exit_reason'].value_counts().to_dict()}")


def print_rf_comparison(tdf_no_rf: pd.DataFrame, tdf_rf: pd.DataFrame):
    """Side-by-side comparison with and without RF filter."""
    print(f"\n  {'':15} {'NO RF':>30} {'WITH RF':>30}")
    print(f"  {'split':5} {'n':>5} {'mean':>8} {'win%':>6}  |  {'n':>5} {'mean':>8} {'win%':>6}")
    print(f"  {'-'*70}")
    for sp in ("train", "val", "test"):
        s1 = tdf_no_rf[tdf_no_rf["split"] == sp] if not tdf_no_rf.empty else pd.DataFrame()
        s2 = tdf_rf[tdf_rf["split"] == sp] if not tdf_rf.empty else pd.DataFrame()
        n1 = len(s1); n2 = len(s2)
        m1 = s1["return_pct"].mean() if n1 else 0
        m2 = s2["return_pct"].mean() if n2 else 0
        w1 = (s1["return_pct"] > 0).mean() * 100 if n1 else 0
        w2 = (s2["return_pct"] > 0).mean() * 100 if n2 else 0
        print(f"  {sp:5} {n1:5d} {m1:+8.2f}% {w1:5.0f}%  |  {n2:5d} {m2:+8.2f}% {w2:5.0f}%")


async def run(
    parquet_path: str | None = None,
    from_db: bool = False,
    use_rl: bool = False,
    use_rf: bool = False,
):
    """Main backtest entry point."""
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    # Load candidates
    if from_db:
        from pipeline.data_loader import build_dataset_from_db
        df = await build_dataset_from_db(output_path=str(DEFAULT_PARQUET))
    else:
        path = parquet_path or str(DEFAULT_PARQUET)
        df = pd.read_parquet(path)

    df = df[df[RELEVANCE_COL].astype(float) > 0.5].copy()
    print(f"[backtest] loaded {len(df)} candidates (relevance > 0.5)")

    # Load price/prob paths from DB
    prices, probs = await load_price_prob_paths(df)

    # Baseline run
    print("\n" + "=" * 60)
    print("  BASELINE (DEFAULT POLICY, no RF)")
    print("=" * 60)
    tdf_baseline = run_backtest(df, prices, probs, DEFAULT_POLICY)
    print_split_results(tdf_baseline)

    # RF comparison
    if use_rf:
        from pipeline import model as rf_model
        train_df = df[df["split"] == "train"]
        if len(train_df) >= 10:
            print("\n" + "=" * 60)
            print("  TRAINING RF MODEL")
            print("=" * 60)
            pipe = rf_model.train(train_df)
            preds = rf_model.predict(pipe, df)

            for sp in ("train", "val", "test"):
                sp_df = df[df["split"] == sp]
                if not sp_df.empty:
                    metrics = rf_model.evaluate(pipe, sp_df)
                    print(f"  {sp}: {metrics}")

            imp = rf_model.feature_importance(pipe)
            print(f"\n  Top features: {dict(imp.head(5))}")

            df_rf = df[preds > 0].copy()
            tdf_rf = run_backtest(df_rf, prices, probs, DEFAULT_POLICY)

            print("\n" + "=" * 60)
            print("  RF-FILTERED vs UNFILTERED")
            print("=" * 60)
            print_rf_comparison(tdf_baseline, tdf_rf)

            rf_model.save(pipe)
            print(f"\n  Model saved to data/models/rf_alpha.joblib")

    # CEM RL optimisation
    if use_rl:
        print("\n" + "=" * 60)
        print("  CEM POLICY SEARCH (Sharpe + DD penalty)")
        print("=" * 60)
        best_policy = cem_search(
            df, prices, probs,
            reward_fn=reward_sharpe_dd,
            n_iter=10,
            pop_size=40,
            seed=42,
        )
        results = evaluate_splits(df, prices, probs, best_policy)
        for sp, stats in results.items():
            print(f"  {sp}: {stats}")

    # Save trades
    if not tdf_baseline.empty:
        out_path = Path("data/backtest_trades.csv")
        tdf_baseline.to_csv(out_path, index=False)
        print(f"\n  Trades saved to {out_path}")


def main():
    args = sys.argv[1:]
    from_db = "--from-db" in args
    use_rl = "--rl" in args
    use_rf = "--rf" in args
    parquet = next((a for a in args if not a.startswith("--")), None)
    asyncio.run(run(parquet_path=parquet, from_db=from_db, use_rl=use_rl, use_rf=use_rf))


if __name__ == "__main__":
    main()
