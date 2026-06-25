"""Task 2: test whether the diffusion lag appears at hourly horizons.

This outsider research script uses historical_price_bars where resolution='1h'.
Forward horizons are elapsed clock hours from the first tradable hourly bar at or
after T_theta, not a count of trading bars.

Usage:
  .venv/Scripts/python.exe -m general_testing.intraday_diffusion_study
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
    hedged_forward_elapsed,
    hedge_symbol,
    load_observations,
    load_price_series,
    pct,
    stats,
    summarize_dataset,
    unconditional_return_elapsed,
)

HORIZONS_HOURS = (2, 6, 24, 48, 72)
NARROWED_ARCHETYPES = (
    "military_escalation+energy_beneficiary",
    "oil_supply_disruption+energy_beneficiary",
    "fda_approval+direct_company",
)


def compute_rows(
    observations: list[dict],
    prices: dict,
    unconditional: dict[tuple[str, int], float | None],
    archetype: str,
    hours: int,
) -> list[dict]:
    output = []
    for row in observations:
        if row["event_archetype"] != archetype:
            continue
        outright, hedge_ret, hedged, hedge = hedged_forward_elapsed(prices, row, hours)
        if outright is None:
            continue
        hedge_drift = unconditional.get((hedge, hours))
        output.append(
            {
                "event_id": row["event_id"],
                "market_id": row["market_id"],
                "symbol": row["symbol"],
                "archetype": archetype,
                "hours": hours,
                "outright": outright,
                "hedge_return": hedge_ret,
                "hedged": hedged,
                "hedge_symbol": hedge,
                "asset_abnormal_vs_etf_drift": (
                    outright - hedge_drift if hedge_drift is not None else None
                ),
                "sector_abnormal": (
                    hedge_ret - hedge_drift
                    if hedge_ret is not None and hedge_drift is not None
                    else None
                ),
            }
        )
    return output


def print_archetype(archetype: str, rows_by_horizon: dict[int, list[dict]]) -> None:
    focus = "  [NARROWED FOCUS]" if archetype in NARROWED_ARCHETYPES else ""
    print("\n" + archetype + focus)
    print("-" * (len(archetype) + len(focus)))
    print(
        " h    n   ev  outright_mean  hedged_mean hit   hedged_std      t   hedged_95ci        sector_abn"
    )
    for hours in HORIZONS_HOURS:
        rows = rows_by_horizon[hours]
        hedged_rows = [row for row in rows if row["hedged"] is not None and np.isfinite(row["hedged"])]
        if not hedged_rows:
            print(f"{hours:2}h {0:5} {0:4}  no priced hedged rows")
            continue
        outright = stats(row["outright"] for row in rows)
        hedged = stats(row["hedged"] for row in hedged_rows)
        sector_abn = stats(row["sector_abnormal"] for row in hedged_rows)
        ci = cluster_bootstrap_mean_ci(hedged_rows, "hedged")
        print(
            f"{hours:2}h {hedged['n']:5} {len({row['event_id'] for row in hedged_rows}):4} "
            f"{pct(outright.get('mean'))}      {pct(hedged.get('mean'))} {hedged.get('hit', float('nan')):5.0%} "
            f"{pct(hedged.get('std'), width=6)} {hedged.get('t_stat', float('nan')):+6.2f} "
            f"{ci_pct(ci):>20} {pct(sector_abn.get('mean'))}"
        )


def intraday_verdict(all_results: dict[str, dict[int, list[dict]]]) -> tuple[str, str]:
    evidence = []
    for archetype in NARROWED_ARCHETYPES:
        for hours in HORIZONS_HOURS:
            rows = [
                row
                for row in all_results.get(archetype, {}).get(hours, [])
                if row["hedged"] is not None and np.isfinite(row["hedged"])
            ]
            hedged = stats(row["hedged"] for row in rows)
            ci = cluster_bootstrap_mean_ci(rows, "hedged")
            if hedged.get("n", 0) >= 8 and ci is not None and ci[0] > 0 and hedged.get("mean", 0.0) > 0:
                evidence.append((archetype, hours, hedged, ci))
    if evidence:
        parts = [
            f"{archetype} {hours}h mean={hedged['mean'] * 100:+.2f}% CI={ci_pct(ci)}"
            for archetype, hours, hedged, ci in evidence
        ]
        return "YES", "intraday idiosyncratic alpha survives in: " + "; ".join(parts)
    best = None
    for archetype in NARROWED_ARCHETYPES:
        for hours in HORIZONS_HOURS:
            rows = [
                row
                for row in all_results.get(archetype, {}).get(hours, [])
                if row["hedged"] is not None and np.isfinite(row["hedged"])
            ]
            hedged = stats(row["hedged"] for row in rows)
            if hedged.get("n", 0) and (best is None or hedged["mean"] > best[2]["mean"]):
                best = (archetype, hours, hedged)
    if best is None:
        return "NO", "no priced narrowed intraday sample"
    return (
        "NO",
        f"no narrowed hourly horizon has a positive event-cluster CI; best {best[0]} {best[1]}h mean={best[2]['mean'] * 100:+.2f}%",
    )


async def run(args: argparse.Namespace) -> None:
    observations, meta = await load_observations(
        run_ids=args.run_id,
        statuses=args.status,
        dedupe=not args.keep_duplicates,
    )
    print("\nINTRADAY DIFFUSION STUDY")
    print("=" * 80)
    summarize_dataset(observations, meta)

    symbols = {row["symbol"] for row in observations}
    symbols |= {hedge_symbol(row) for row in observations}
    symbols.add(SPY)
    prices = await load_price_series(symbols, "1h")
    print(f"\nloaded hourly price series: {len(prices)} symbols")
    observations = [
        row
        for row in observations
        if row["symbol"] in prices and (hedge_symbol(row) in prices or SPY in prices)
    ]
    print(f"hourly-price-eligible candidates={len(observations)}")

    hedge_symbols = sorted({hedge_symbol(row) if hedge_symbol(row) in prices else SPY for row in observations})
    unconditional = {
        (symbol, hours): unconditional_return_elapsed(prices, symbol, hours)
        for symbol in hedge_symbols
        for hours in HORIZONS_HOURS
    }

    archetypes = sorted({row["event_archetype"] for row in observations})
    all_results: dict[str, dict[int, list[dict]]] = {}
    for archetype in archetypes:
        all_results[archetype] = {
            hours: compute_rows(observations, prices, unconditional, archetype, hours)
            for hours in HORIZONS_HOURS
        }
        print_archetype(archetype, all_results[archetype])

    verdict, reason = intraday_verdict(all_results)
    print("\nVERDICT: INTRADAY WORTH PURSUING?")
    print("-" * 80)
    print(f"{verdict}: {reason}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Hourly diffusion-lag event study.")
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
