"""Study synchronized Polymarket probability paths vs asset price paths.

This is deliberately not a trade P&L study. The unit of observation is an hourly
asset bar interval inside a market's life. For every mapped candidate, the script:

  * aligns each asset bar to the latest Polymarket probability at that timestamp;
  * computes the probability change over the same bar interval;
  * computes raw, hedge, and sector-hedged asset returns for the same interval;
  * asks whether probability changes are contemporaneously related to prices and
    whether they lead future returns.

Usage:
  .venv/Scripts/python.exe -m general_testing.probability_price_relationship_study
"""
from __future__ import annotations

import argparse
import asyncio
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from database.backtesting.schema import SCHEMA
from database.db_connection import connect
from general_testing.diffusion_research_helpers import (
    DEFAULT_RUN_STATUSES,
    SPY,
    hedge_symbol,
    load_observations,
    load_price_series,
    load_probability_series,
    np_dt,
    polarity_for_group,
    summarize_dataset,
)
from main_backtesting.semantic_groups import LONG_ELIGIBLE_GROUPS

MIN_CORR_N = 40
SHOCK_THRESHOLD = 0.05
FUTURE_BARS = (1, 2, 6)
FUTURE_CLOCK_HOURS = (24,)


@dataclass(frozen=True)
class IntervalRow:
    event_id: str
    market_id: str
    symbol: str
    archetype: str
    instrument_type: str
    phase: str
    hedge: str
    self_hedged: bool
    ts: np.datetime64
    dp: float
    abs_dp: float
    probability: float
    raw_ret: float
    hedge_ret: float
    hedged_ret: float
    signed_hedged_ret: float
    signed_raw_ret: float
    signed_hret_fwd_1bar: float | None
    signed_hret_fwd_2bar: float | None
    signed_hret_fwd_6bar: float | None
    signed_hret_fwd_24h: float | None
    dp_fwd_1bar: float | None
    dp_fwd_2bar: float | None
    dp_fwd_6bar: float | None
    dp_fwd_24h: float | None
    cum_prob_move: float | None
    cum_signed_hedged: float | None
    repricing_ratio: float | None


def finite(value: Any) -> bool:
    return value is not None and np.isfinite(float(value))


def pct(value: float | None, width: int = 7) -> str:
    if value is None or not finite(value):
        return " " * (width - 2) + "na"
    return f"{float(value) * 100:+{width}.2f}%"


def corr(x: Iterable[float | None], y: Iterable[float | None]) -> float | None:
    pairs = [(float(a), float(b)) for a, b in zip(x, y) if finite(a) and finite(b)]
    if len(pairs) < MIN_CORR_N:
        return None
    xs = np.array([a for a, _ in pairs], dtype=float)
    ys = np.array([b for _, b in pairs], dtype=float)
    if xs.std() == 0 or ys.std() == 0:
        return None
    return float(np.corrcoef(xs, ys)[0, 1])


def mean(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if finite(value)]
    return float(np.mean(clean)) if clean else None


def hit(values: Iterable[float | None]) -> float | None:
    clean = [float(value) for value in values if finite(value)]
    return float(np.mean([value > 0 for value in clean])) if clean else None


def close_at_or_before(series: dict[str, tuple[np.ndarray, np.ndarray]], symbol: str, ts: np.datetime64) -> float | None:
    if symbol not in series:
        return None
    dates, closes = series[symbol]
    idx = int(np.searchsorted(dates, ts, side="right")) - 1
    if idx < 0:
        return None
    return float(closes[idx])


def fwd_hedged_bar_return(
    series: dict[str, tuple[np.ndarray, np.ndarray]],
    asset: str,
    hedge: str,
    asset_idx: int,
    bars: int,
) -> float | None:
    if asset not in series:
        return None
    dates, closes = series[asset]
    exit_idx = asset_idx + bars
    if exit_idx >= len(closes) or closes[asset_idx] <= 0:
        return None
    asset_ret = float(closes[exit_idx] / closes[asset_idx] - 1.0)
    start_h = close_at_or_before(series, hedge, dates[asset_idx])
    exit_h = close_at_or_before(series, hedge, dates[exit_idx])
    if start_h is None or exit_h is None or start_h <= 0:
        return None
    return asset_ret - (exit_h / start_h - 1.0)


def fwd_hedged_clock_return(
    series: dict[str, tuple[np.ndarray, np.ndarray]],
    asset: str,
    hedge: str,
    asset_idx: int,
    hours: int,
) -> float | None:
    if asset not in series:
        return None
    dates, closes = series[asset]
    exit_ts = dates[asset_idx] + np.timedelta64(int(hours), "h")
    exit_idx = int(np.searchsorted(dates, exit_ts, side="left"))
    if exit_idx >= len(closes) or closes[asset_idx] <= 0:
        return None
    asset_ret = float(closes[exit_idx] / closes[asset_idx] - 1.0)
    start_h = close_at_or_before(series, hedge, dates[asset_idx])
    exit_h = close_at_or_before(series, hedge, dates[exit_idx])
    if start_h is None or exit_h is None or start_h <= 0:
        return None
    return asset_ret - (exit_h / start_h - 1.0)


async def load_is_etf(symbols: Iterable[str]) -> dict[str, bool]:
    wanted = sorted({str(symbol) for symbol in symbols if symbol})
    if not wanted:
        return {}
    conn = await connect()
    try:
        rows = await conn.fetch(
            f"""
            SELECT yfinance_symbol AS symbol, bool_or(is_etf) AS is_etf
            FROM {SCHEMA}.historical_us_security_master
            WHERE yfinance_symbol = ANY($1::text[])
            GROUP BY yfinance_symbol
            """,
            wanted,
        )
    finally:
        await conn.close()
    return {str(row["symbol"]): bool(row["is_etf"]) for row in rows}


def classify_instrument(row: dict[str, Any], hedge: str, is_etf: dict[str, bool]) -> str:
    symbol = str(row["symbol"])
    archetype = str(row["event_archetype"])
    if symbol == hedge:
        return "self_hedged_beta"
    if is_etf.get(symbol):
        return "etf_or_fund"
    if archetype == "fda_approval+direct_company":
        return "binary_direct_company"
    if archetype.endswith("+direct_company"):
        return "direct_company"
    return "single_name_exposure"


def polarity_sign(row: dict[str, Any]) -> int:
    group = str(row.get("semantic_group") or row.get("event_archetype") or "")
    if group in LONG_ELIGIBLE_GROUPS or polarity_for_group(group) == "positive":
        return 1
    if polarity_for_group(group) == "negative":
        return -1
    return 1


def probability_at(prob_dates: np.ndarray, probabilities: np.ndarray, ts: np.datetime64) -> float | None:
    idx = int(np.searchsorted(prob_dates, ts, side="right")) - 1
    if idx < 0:
        return None
    return float(probabilities[idx])


def probability_at_or_after(prob_dates: np.ndarray, probabilities: np.ndarray, ts: np.datetime64) -> float | None:
    idx = int(np.searchsorted(prob_dates, ts, side="left"))
    if idx >= len(probabilities):
        return None
    return float(probabilities[idx])


def build_interval_rows(
    observations: list[dict[str, Any]],
    prices: dict[str, tuple[np.ndarray, np.ndarray]],
    probabilities: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    is_etf: dict[str, bool],
    *,
    phase_filter: str,
) -> list[IntervalRow]:
    output: list[IntervalRow] = []
    for row in observations:
        symbol = str(row["symbol"])
        if symbol not in prices or str(row["market_id"]) not in probabilities:
            continue
        hedge = hedge_symbol(row)
        if hedge not in prices and hedge != SPY:
            hedge = SPY
        if hedge not in prices:
            continue
        sign = polarity_sign(row)
        dates, closes = prices[symbol]
        prob_dates, prob_values, _ = probabilities[str(row["market_id"])]
        t0 = np_dt(row["t0"])
        t_theta = np_dt(row["first_pass_at"])
        te = np_dt(row["te"])
        start = t0
        end = te
        if phase_filter == "pre_trigger":
            end = min(te, t_theta)
        elif phase_filter == "post_trigger":
            start = max(t0, t_theta)
        instrument_type = classify_instrument(row, hedge, is_etf)
        p0 = probability_at(prob_dates, prob_values, t0)
        if p0 is None:
            p0 = probability_at_or_after(prob_dates, prob_values, t0)
        start_idx = int(np.searchsorted(dates, start, side="left"))
        end_idx = int(np.searchsorted(dates, end, side="right"))
        if end_idx - start_idx < 2:
            continue
        entry_close = closes[start_idx] if closes[start_idx] > 0 else None
        entry_hedge = close_at_or_before(prices, hedge, dates[start_idx])
        for idx in range(start_idx + 1, end_idx):
            p_prev = probability_at(prob_dates, prob_values, dates[idx - 1])
            p_now = probability_at(prob_dates, prob_values, dates[idx])
            if p_prev is None or p_now is None or closes[idx - 1] <= 0:
                continue
            hedge_prev = close_at_or_before(prices, hedge, dates[idx - 1])
            hedge_now = close_at_or_before(prices, hedge, dates[idx])
            if hedge_prev is None or hedge_now is None or hedge_prev <= 0:
                continue
            raw_ret = float(closes[idx] / closes[idx - 1] - 1.0)
            hedge_ret = float(hedge_now / hedge_prev - 1.0)
            hedged_ret = raw_ret - hedge_ret
            fwd = {
                bars: fwd_hedged_bar_return(prices, symbol, hedge, idx, bars)
                for bars in FUTURE_BARS
            }
            fwd_clock = {
                hours: fwd_hedged_clock_return(prices, symbol, hedge, idx, hours)
                for hours in FUTURE_CLOCK_HOURS
            }
            dp_fwd: dict[int, float | None] = {}
            for bars in FUTURE_BARS:
                future_idx = idx + bars
                if future_idx < len(dates):
                    p_future = probability_at(prob_dates, prob_values, dates[future_idx])
                    dp_fwd[bars] = p_future - p_now if p_future is not None else None
                else:
                    dp_fwd[bars] = None
            dp_fwd_clock: dict[int, float | None] = {}
            for hours in FUTURE_CLOCK_HOURS:
                future_ts = dates[idx] + np.timedelta64(int(hours), "h")
                future_idx = int(np.searchsorted(dates, future_ts, side="left"))
                if future_idx < len(dates):
                    p_future = probability_at(prob_dates, prob_values, dates[future_idx])
                    dp_fwd_clock[hours] = p_future - p_now if p_future is not None else None
                else:
                    dp_fwd_clock[hours] = None
            cum_prob = p_now - p0 if p0 is not None else None
            cum_signed_hedged = None
            ratio = None
            if entry_close is not None and entry_hedge is not None and entry_close > 0 and entry_hedge > 0:
                current_hedge = close_at_or_before(prices, hedge, dates[idx])
                if current_hedge is not None:
                    cum_raw = float(closes[idx] / entry_close - 1.0)
                    cum_hedge = float(current_hedge / entry_hedge - 1.0)
                    cum_signed_hedged = sign * (cum_raw - cum_hedge)
                    if cum_prob is not None and abs(cum_prob) >= 0.01:
                        ratio = cum_signed_hedged / abs(cum_prob)
            phase = "pre_trigger" if dates[idx] < t_theta else "post_trigger"
            output.append(
                IntervalRow(
                    event_id=str(row["event_id"]),
                    market_id=str(row["market_id"]),
                    symbol=symbol,
                    archetype=str(row["event_archetype"]),
                    instrument_type=instrument_type,
                    phase=phase,
                    hedge=hedge,
                    self_hedged=symbol == hedge,
                    ts=dates[idx],
                    dp=float(p_now - p_prev),
                    abs_dp=abs(float(p_now - p_prev)),
                    probability=float(p_now),
                    raw_ret=raw_ret,
                    hedge_ret=hedge_ret,
                    hedged_ret=hedged_ret,
                    signed_hedged_ret=sign * hedged_ret,
                    signed_raw_ret=sign * raw_ret,
                    signed_hret_fwd_1bar=sign * fwd[1] if fwd[1] is not None else None,
                    signed_hret_fwd_2bar=sign * fwd[2] if fwd[2] is not None else None,
                    signed_hret_fwd_6bar=sign * fwd[6] if fwd[6] is not None else None,
                    signed_hret_fwd_24h=sign * fwd_clock[24] if fwd_clock[24] is not None else None,
                    dp_fwd_1bar=dp_fwd[1],
                    dp_fwd_2bar=dp_fwd[2],
                    dp_fwd_6bar=dp_fwd[6],
                    dp_fwd_24h=dp_fwd_clock[24],
                    cum_prob_move=cum_prob,
                    cum_signed_hedged=cum_signed_hedged,
                    repricing_ratio=ratio,
                )
            )
    return output


def group_key(row: IntervalRow, scope: str) -> str:
    if scope == "archetype":
        return row.archetype
    if scope == "instrument_type":
        return row.instrument_type
    if scope == "phase":
        return row.phase
    return "all"


def print_corr_table(rows: list[IntervalRow], scope: str, *, sample: str = "all") -> None:
    if sample == "nonzero":
        rows = [row for row in rows if row.abs_dp > 0]
    elif sample == "shock":
        rows = [row for row in rows if row.dp >= SHOCK_THRESHOLD]
    grouped: dict[str, list[IntervalRow]] = defaultdict(list)
    for row in rows:
        grouped[group_key(row, scope)].append(row)
    print(f"\nCORRELATION ({sample}): delta probability vs signed returns by {scope}")
    print("-" * 100)
    print("group                                      n  nonzero  shocks  same_raw same_hedge next1 next2 next6 next24h")
    for key in sorted(grouped, key=lambda k: (-len(grouped[k]), k)):
        values = grouped[key]
        nonzero = [row for row in values if row.abs_dp > 0]
        shocks = [row for row in values if row.dp >= SHOCK_THRESHOLD]
        print(
            f"{key[:42]:42} {len(values):6} {len(nonzero):7} {len(shocks):6} "
            f"{corr([r.dp for r in values], [r.signed_raw_ret for r in values]) or float('nan'):+8.3f} "
            f"{corr([r.dp for r in values], [r.signed_hedged_ret for r in values]) or float('nan'):+10.3f} "
            f"{corr([r.dp for r in values], [r.signed_hret_fwd_1bar for r in values]) or float('nan'):+5.3f} "
            f"{corr([r.dp for r in values], [r.signed_hret_fwd_2bar for r in values]) or float('nan'):+5.3f} "
            f"{corr([r.dp for r in values], [r.signed_hret_fwd_6bar for r in values]) or float('nan'):+5.3f} "
            f"{corr([r.dp for r in values], [r.signed_hret_fwd_24h for r in values]) or float('nan'):+7.3f}"
        )


def print_shock_table(rows: list[IntervalRow], scope: str) -> None:
    grouped: dict[str, list[IntervalRow]] = defaultdict(list)
    for row in rows:
        grouped[group_key(row, scope)].append(row)
    print(f"\nIMPULSE RESPONSE: probability up-shocks dp >= {SHOCK_THRESHOLD:.0%} by {scope}")
    print("-" * 100)
    print("group                                      n_shock same_hedge next1 hit1 next6 hit6 next24h hit24")
    for key in sorted(grouped, key=lambda k: (-len(grouped[k]), k)):
        shocks = [row for row in grouped[key] if row.dp >= SHOCK_THRESHOLD]
        if len(shocks) < 5:
            continue
        next1 = [row.signed_hret_fwd_1bar for row in shocks]
        next6 = [row.signed_hret_fwd_6bar for row in shocks]
        next24 = [row.signed_hret_fwd_24h for row in shocks]
        print(
            f"{key[:42]:42} {len(shocks):7} {pct(mean(row.signed_hedged_ret for row in shocks))} "
            f"{pct(mean(next1))} {hit(next1) or float('nan'):4.0%} "
            f"{pct(mean(next6))} {hit(next6) or float('nan'):4.0%} "
            f"{pct(mean(next24))} {hit(next24) or float('nan'):5.0%}"
        )


def print_lead_lag_table(rows: list[IntervalRow], scope: str) -> None:
    grouped: dict[str, list[IntervalRow]] = defaultdict(list)
    for row in rows:
        grouped[group_key(row, scope)].append(row)
    print(f"\nLEAD-LAG: Polymarket leads price vs price leads Polymarket by {scope}")
    print("-" * 100)
    print("group                                      n  pm->r1 pm->r6 pm->r24  r->pm1 r->pm6 r->pm24")
    for key in sorted(grouped, key=lambda k: (-len(grouped[k]), k)):
        values = grouped[key]
        print(
            f"{key[:42]:42} {len(values):6} "
            f"{corr([r.dp for r in values], [r.signed_hret_fwd_1bar for r in values]) or float('nan'):+7.3f} "
            f"{corr([r.dp for r in values], [r.signed_hret_fwd_6bar for r in values]) or float('nan'):+7.3f} "
            f"{corr([r.dp for r in values], [r.signed_hret_fwd_24h for r in values]) or float('nan'):+7.3f} "
            f"{corr([r.signed_hedged_ret for r in values], [r.dp_fwd_1bar for r in values]) or float('nan'):+7.3f} "
            f"{corr([r.signed_hedged_ret for r in values], [r.dp_fwd_6bar for r in values]) or float('nan'):+7.3f} "
            f"{corr([r.signed_hedged_ret for r in values], [r.dp_fwd_24h for r in values]) or float('nan'):+8.3f}"
        )


def print_repricing_table(rows: list[IntervalRow]) -> None:
    candidates = [
        row
        for row in rows
        if row.dp >= SHOCK_THRESHOLD
        and row.repricing_ratio is not None
        and finite(row.signed_hret_fwd_24h)
        and not row.self_hedged
    ]
    print("\nREPRICING CHECK: after probability up-shocks, split by prior signed hedged repricing")
    print("-" * 100)
    if len(candidates) < 30:
        print(f"not enough non-self-hedged shock rows with repricing ratios: n={len(candidates)}")
        return
    ratios = np.array([row.repricing_ratio for row in candidates], dtype=float)
    cuts = np.quantile(ratios, [0.0, 1 / 3, 2 / 3, 1.0])
    print("bucket              n  ratio_mean same_hedge next1 next6 next24h hit24")
    for label, low, high in zip(("under-repriced", "middle", "already-repriced"), cuts[:-1], cuts[1:]):
        bucket = [
            row
            for row in candidates
            if (row.repricing_ratio >= low and row.repricing_ratio <= high)
        ]
        print(
            f"{label:18} {len(bucket):4} {mean(row.repricing_ratio for row in bucket):+10.3f} "
            f"{pct(mean(row.signed_hedged_ret for row in bucket))} "
            f"{pct(mean(row.signed_hret_fwd_1bar for row in bucket))} "
            f"{pct(mean(row.signed_hret_fwd_6bar for row in bucket))} "
            f"{pct(mean(row.signed_hret_fwd_24h for row in bucket))} "
            f"{hit(row.signed_hret_fwd_24h for row in bucket) or float('nan'):5.0%}"
        )


async def run(args: argparse.Namespace) -> None:
    observations, meta = await load_observations(
        run_ids=args.run_id,
        statuses=args.status,
        dedupe=not args.keep_duplicates,
    )
    print("\nPROBABILITY / PRICE RELATIONSHIP STUDY")
    print("=" * 100)
    summarize_dataset(observations, meta)
    symbols = {row["symbol"] for row in observations} | {hedge_symbol(row) for row in observations} | {SPY}
    prices, probabilities, is_etf = await asyncio.gather(
        load_price_series(symbols, "1h"),
        load_probability_series({row["market_id"] for row in observations}),
        load_is_etf(symbols),
    )
    rows = build_interval_rows(
        observations,
        prices,
        probabilities,
        is_etf,
        phase_filter=args.phase,
    )
    print(
        f"\ninterval_rows={len(rows)} candidates={len({(r.market_id, r.symbol) for r in rows})} "
        f"markets={len({r.market_id for r in rows})} events={len({r.event_id for r in rows})} "
        f"symbols={len({r.symbol for r in rows})}"
    )
    print(
        "unit: consecutive hourly asset bars inside the market window; probability is latest Polymarket "
        "value at each bar timestamp"
    )
    print("returns are signed by semantic Yes-outcome exposure; positive means Yes-probability-up should help")
    print_corr_table(rows, "instrument_type")
    print_corr_table(rows, "instrument_type", sample="nonzero")
    print_corr_table(rows, "archetype")
    print_corr_table(rows, "archetype", sample="nonzero")
    print_corr_table(rows, "phase")
    print_lead_lag_table(rows, "instrument_type")
    print_lead_lag_table(rows, "archetype")
    print_shock_table(rows, "instrument_type")
    print_shock_table(rows, "archetype")
    print_repricing_table(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Study probability/price lead-lag relationships.")
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
    parser.add_argument(
        "--phase",
        choices=("all", "pre_trigger", "post_trigger"),
        default="all",
        help="Market window phase to analyze.",
    )
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
