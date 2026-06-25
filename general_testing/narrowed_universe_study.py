"""Task 1: narrow the universe to the only pre-declared possible alpha pockets.

This is an outsider research script. It reads completed historical backtest runs from
Postgres, dedupes exact repeated candidates across runs, and does not modify the engine.

Usage:
  .venv/Scripts/python.exe -m general_testing.narrowed_universe_study
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

import numpy as np

from general_testing.diffusion_research_helpers import (
    DEFAULT_RUN_STATUSES,
    SPY,
    ci_pct,
    cluster_bootstrap_mean_ci,
    hedged_forward_bars,
    hedge_symbol,
    load_observations,
    load_price_series,
    pct,
    stats,
    summarize_dataset,
)

HORIZONS = (3, 5, 10, 15)
NARROWED_ARCHETYPES = (
    "military_escalation+energy_beneficiary",
    "oil_supply_disruption+energy_beneficiary",
    "fda_approval+direct_company",
)
DROPPED_ARCHETYPES = (
    "earnings_beat+direct_company",
    "military_escalation+defense_beneficiary",
)
ALL_ARCHETYPES = tuple(dict.fromkeys((*NARROWED_ARCHETYPES, *DROPPED_ARCHETYPES)))


def result_rows_for_archetype(
    observations: list[dict],
    prices: dict,
    archetype: str,
    horizon: int,
) -> list[dict]:
    output = []
    for row in observations:
        if row["event_archetype"] != archetype:
            continue
        outright, hedged, hedge = hedged_forward_bars(prices, row, horizon)
        if outright is None:
            continue
        output.append(
            {
                "event_id": row["event_id"],
                "market_id": row["market_id"],
                "symbol": row["symbol"],
                "horizon": horizon,
                "archetype": archetype,
                "outright": outright,
                "hedged": hedged,
                "hedge_symbol": hedge,
            }
        )
    return output


def print_archetype_table(archetype: str, rows_by_horizon: dict[int, list[dict]]) -> None:
    print("\n" + archetype)
    print("-" * len(archetype))
    print(
        " h  n   ev  outright_mean hit   hedged_mean hit   hedged_std sharpe      t   hedged_mean_95ci"
    )
    for horizon in HORIZONS:
        rows = rows_by_horizon[horizon]
        out = stats(row["outright"] for row in rows)
        hedged_rows = [row for row in rows if row["hedged"] is not None and np.isfinite(row["hedged"])]
        hed = stats(row["hedged"] for row in hedged_rows)
        ci = cluster_bootstrap_mean_ci(hedged_rows, "hedged")
        if hed.get("n", 0) == 0:
            print(f"{horizon:2}d {0:4} {0:4}  no priced hedged rows")
            continue
        print(
            f"{horizon:2}d {hed['n']:4} {len({row['event_id'] for row in hedged_rows}):4} "
            f"{pct(out.get('mean'))} {out.get('hit', float('nan')):5.0%} "
            f"{pct(hed.get('mean'))} {hed.get('hit', float('nan')):5.0%} "
            f"{pct(hed.get('std'), width=6)} {hed.get('sharpe_trade', float('nan')):+6.2f} "
            f"{hed.get('t_stat', float('nan')):+6.2f} {ci_pct(ci):>22}"
        )


def verdict_for_archetype(archetype: str, rows_by_horizon: dict[int, list[dict]]) -> tuple[str, str]:
    if archetype in DROPPED_ARCHETYPES:
        contradictory = []
        for horizon in (10, 15):
            hedged_rows = [
                row
                for row in rows_by_horizon[horizon]
                if row["hedged"] is not None and np.isfinite(row["hedged"])
            ]
            hed = stats(row["hedged"] for row in hedged_rows)
            ci = cluster_bootstrap_mean_ci(hedged_rows, "hedged")
            if hed.get("n", 0) >= 20 and ci is not None and ci[0] > 0:
                contradictory.append(f"{horizon}d CI {ci_pct(ci)}")
        if contradictory:
            return "DROP", "pre-declared dropped group; data contradiction to inspect: " + "; ".join(contradictory)
        return "DROP", "pre-declared dropped group"

    support = []
    for horizon in (10, 15):
        hedged_rows = [
            row
            for row in rows_by_horizon[horizon]
            if row["hedged"] is not None and np.isfinite(row["hedged"])
        ]
        hed = stats(row["hedged"] for row in hedged_rows)
        ci = cluster_bootstrap_mean_ci(hedged_rows, "hedged")
        support.append((horizon, hed, ci))
    usable = [(horizon, hed, ci) for horizon, hed, ci in support if hed.get("n", 0) > 0]
    if not usable:
        return "DROP", "no priced hedged sample"
    significant = [
        (horizon, hed, ci)
        for horizon, hed, ci in usable
        if hed.get("n", 0) >= 8 and ci is not None and ci[0] > 0 and hed.get("mean", 0.0) > 0
    ]
    consistent_positive = all(hed.get("mean", 0.0) > 0 for _, hed, _ in usable)
    if significant and consistent_positive:
        horizons = ", ".join(f"{horizon}d" for horizon, _, _ in significant)
        return "KEEP", f"positive hedged mean with event-cluster CI above 0 at {horizons}"
    best = max(usable, key=lambda item: item[1].get("mean", -999.0))
    return (
        "DROP",
        f"hedged alpha not statistically distinguishable from 0; best {best[0]}d mean={best[1].get('mean', 0.0) * 100:+.2f}%",
    )


async def run(args: argparse.Namespace) -> None:
    observations, meta = await load_observations(
        archetypes=ALL_ARCHETYPES,
        run_ids=args.run_id,
        statuses=args.status,
        dedupe=not args.keep_duplicates,
    )
    print("\nNARROWED UNIVERSE STUDY")
    print("=" * 80)
    summarize_dataset(observations, meta)

    symbols = {row["symbol"] for row in observations}
    symbols |= {hedge_symbol(row) for row in observations}
    symbols.add(SPY)
    prices = await load_price_series(symbols, "1d")
    print(f"\nloaded daily price series: {len(prices)} symbols")
    print("hedge policy: per-symbol sector_etf, fallback benchmark_symbol, fallback SPY")
    print("CI policy: event-cluster bootstrap of hedged mean, exact duplicate candidates deduped by default")

    all_results: dict[str, dict[int, list[dict]]] = {}
    for archetype in ALL_ARCHETYPES:
        all_results[archetype] = {
            horizon: result_rows_for_archetype(observations, prices, archetype, horizon)
            for horizon in HORIZONS
        }
        print_archetype_table(archetype, all_results[archetype])

    print("\nVERDICTS")
    print("-" * 80)
    for archetype in ALL_ARCHETYPES:
        verdict, reason = verdict_for_archetype(archetype, all_results[archetype])
        label = "narrowed candidate" if archetype in NARROWED_ARCHETYPES else "dropped contrast"
        print(f"{verdict:4}  {archetype:48}  ({label})  {reason}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Daily narrowed-universe idiosyncratic alpha study.")
    parser.add_argument("--run-id", action="append", help="Optional run_id filter; repeat for multiple runs.")
    parser.add_argument(
        "--status",
        action="append",
        default=list(DEFAULT_RUN_STATUSES),
        help="Run status to include; default includes complete and kept.",
    )
    parser.add_argument(
        "--keep-duplicates",
        action="store_true",
        help="Keep exact duplicate candidates across runs instead of deduping them.",
    )
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
