from __future__ import annotations

import json
import random
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Iterable

import numpy as np

from database.backtesting.schema import SCHEMA
from database.db_connection import connect
from main_backtesting.semantic_groups import LONG_ELIGIBLE_GROUPS, PROPOSED_GROUPS, classify_assignment

DEFAULT_RUN_STATUSES = ("complete", "kept")
SPY = "SPY"
TRADE_NOTIONAL = 1000.0

PriceSeries = tuple[np.ndarray, np.ndarray]
ProbabilitySeries = tuple[np.ndarray, np.ndarray, np.ndarray]


def json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def naive_dt(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def np_dt(value: datetime) -> np.datetime64:
    return np.datetime64(naive_dt(value), "ns")


def pct(value: float | None, width: int = 7) -> str:
    if value is None or not np.isfinite(value):
        return " " * (width - 2) + "na"
    return f"{value * 100:+{width}.2f}%"


def pct_from_pct(value: float | None, width: int = 7) -> str:
    if value is None or not np.isfinite(value):
        return " " * (width - 2) + "na"
    return f"{value:+{width}.2f}%"


def split_group(group: str | None) -> tuple[str | None, str | None]:
    if not group or "+" not in group:
        return None, None
    outcome, role = group.split("+", 1)
    return outcome, role


def polarity_for_group(group: str | None) -> str:
    if group in LONG_ELIGIBLE_GROUPS:
        return "positive"
    if group in PROPOSED_GROUPS:
        return "negative"
    return "ambiguous"


def attach_semantic_fields(row: dict[str, Any]) -> dict[str, Any]:
    stored_group = str(row.get("event_archetype") or "")
    try:
        assignment = classify_assignment(
            str(row.get("question") or ""),
            symbol=str(row.get("symbol") or ""),
            asset_name=str(row.get("asset_name") or ""),
            event_title=str(row.get("event_title") or ""),
            tags=list(row.get("tags") or []),
        )
        semantic_group = assignment.group or stored_group
        semantic_outcome = assignment.outcome
        asset_role = assignment.asset_role
        polarity = assignment.yes_outcome_polarity
        long_eligible = assignment.long_eligible
    except Exception:
        semantic_group = stored_group
        semantic_outcome, asset_role = split_group(stored_group)
        polarity = polarity_for_group(stored_group)
        long_eligible = stored_group in LONG_ELIGIBLE_GROUPS
    if not semantic_outcome or asset_role == "ambiguous":
        semantic_outcome, parsed_role = split_group(semantic_group)
        asset_role = parsed_role or asset_role
    return {
        **row,
        "semantic_group": semantic_group,
        "semantic_outcome": semantic_outcome,
        "asset_role": asset_role,
        "yes_outcome_polarity": polarity,
        "long_eligible": bool(long_eligible),
    }


def dedupe_observations(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in rows:
        key = (
            str(row["event_id"]),
            str(row["market_id"]),
            int(row["first_pass_number"]),
            str(row["symbol"]),
            naive_dt(row["first_pass_at"]),
            str(row["event_archetype"]),
        )
        if key not in deduped:
            item = dict(row)
            item["source_row_count"] = 1
            item["source_run_ids"] = [str(row["run_id"])]
            deduped[key] = item
        else:
            deduped[key]["source_row_count"] += 1
            deduped[key]["source_run_ids"].append(str(row["run_id"]))
    output = []
    for item in deduped.values():
        item["source_run_ids"] = sorted(set(item["source_run_ids"]))
        output.append(attach_semantic_fields(item))
    output.sort(key=lambda r: (r["first_pass_at"], str(r["market_id"]), str(r["symbol"])))
    return output


async def load_observations(
    *,
    archetypes: Iterable[str] | None = None,
    run_ids: Iterable[str] | None = None,
    statuses: Iterable[str] = DEFAULT_RUN_STATUSES,
    dedupe: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    args: list[Any] = [list(statuses)]
    where = ["br.status = ANY($1::text[])", "o.valid_for_training"]
    if run_ids:
        args.append([str(run_id) for run_id in run_ids])
        where.append(f"o.run_id::text = ANY(${len(args)}::text[])")
    if archetypes:
        args.append(list(archetypes))
        where.append(f"o.event_archetype = ANY(${len(args)}::text[])")
    where_sql = " AND ".join(where)
    conn = await connect()
    try:
        records = await conn.fetch(
            f"""
            SELECT o.run_id::text AS run_id, o.observation_id::text AS observation_id,
                   o.event_id, o.market_id, o.first_pass_number, o.first_pass_at,
                   o.label_available_at, o.symbol, o.event_archetype, o.features,
                   o.research_data, o.classification_target, o.regression_target,
                   m.created_at AS t0, m.end_at AS te, m.question, m.final_outcome,
                   e.title AS event_title, e.tags,
                   COALESCE(am.asset_name, o.symbol) AS asset_name,
                   am.sector_etf, am.benchmark_symbol
            FROM {SCHEMA}.historical_ml_observations o
            JOIN {SCHEMA}.historical_backtest_runs br ON br.run_id=o.run_id
            JOIN {SCHEMA}.historical_run_markets m
              ON m.run_id=o.run_id AND m.market_id=o.market_id
            JOIN {SCHEMA}.source_events e ON e.event_id=o.event_id
            LEFT JOIN {SCHEMA}.historical_asset_metadata am ON am.symbol=o.symbol
            WHERE {where_sql}
            ORDER BY o.first_pass_at, o.market_id, o.first_pass_number, o.symbol, o.run_id
            """,
            *args,
        )
    finally:
        await conn.close()

    rows: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        item["features"] = json_value(item.get("features") or {})
        item["research_data"] = json_value(item.get("research_data") or {})
        item["tags"] = json_value(item.get("tags") or [])
        rows.append(item)
    output = dedupe_observations(rows) if dedupe else [attach_semantic_fields(row) for row in rows]
    meta = {
        "source_rows": len(rows),
        "unique_candidates": len(output),
        "run_ids": sorted({str(row["run_id"]) for row in rows}),
        "statuses": list(statuses),
        "deduped": dedupe,
    }
    return output, meta


async def load_price_series(symbols: Iterable[str], resolution: str) -> dict[str, PriceSeries]:
    wanted = sorted({str(symbol) for symbol in symbols if symbol})
    if not wanted:
        return {}
    conn = await connect()
    try:
        rows = await conn.fetch(
            f"""
            SELECT symbol, ts, close
            FROM {SCHEMA}.historical_price_bars
            WHERE resolution=$1 AND symbol = ANY($2::text[])
            ORDER BY symbol, ts
            """,
            resolution,
            wanted,
        )
    finally:
        await conn.close()
    by_symbol: dict[str, list[tuple[np.datetime64, float]]] = defaultdict(list)
    for row in rows:
        by_symbol[str(row["symbol"])].append((np_dt(row["ts"]), float(row["close"])))
    return {
        symbol: (
            np.array([ts for ts, _ in values], dtype="datetime64[ns]"),
            np.array([close for _, close in values], dtype=float),
        )
        for symbol, values in by_symbol.items()
    }


async def load_probability_series(market_ids: Iterable[str]) -> dict[str, ProbabilitySeries]:
    wanted = sorted({str(market_id) for market_id in market_ids if market_id})
    if not wanted:
        return {}
    conn = await connect()
    try:
        rows = await conn.fetch(
            f"""
            SELECT market_id, hour_ts, probability, volume_usdc
            FROM {SCHEMA}.historical_probability_points
            WHERE market_id = ANY($1::text[])
            ORDER BY market_id, hour_ts
            """,
            wanted,
        )
    finally:
        await conn.close()
    by_market: dict[str, list[tuple[np.datetime64, float, float]]] = defaultdict(list)
    for row in rows:
        volume = row["volume_usdc"]
        by_market[str(row["market_id"])].append(
            (np_dt(row["hour_ts"]), float(row["probability"]), float(volume) if volume is not None else np.nan)
        )
    return {
        market_id: (
            np.array([ts for ts, _, _ in values], dtype="datetime64[ns]"),
            np.array([probability for _, probability, _ in values], dtype=float),
            np.array([volume for _, _, volume in values], dtype=float),
        )
        for market_id, values in by_market.items()
    }


def hedge_symbol(row: dict[str, Any]) -> str:
    return str(row.get("sector_etf") or row.get("benchmark_symbol") or SPY)


def _entry_idx(series: dict[str, PriceSeries], symbol: str, t: datetime) -> int | None:
    if not symbol or symbol not in series:
        return None
    dates, _ = series[symbol]
    idx = int(np.searchsorted(dates, np_dt(t), side="left"))
    return idx if idx < len(dates) else None


def _before_idx(series: dict[str, PriceSeries], symbol: str, t: datetime) -> int | None:
    if not symbol or symbol not in series:
        return None
    dates, _ = series[symbol]
    idx = int(np.searchsorted(dates, np_dt(t), side="right")) - 1
    return idx if idx >= 0 else None


def fwd_return_bars(series: dict[str, PriceSeries], symbol: str, t: datetime, bars: int) -> float | None:
    idx = _entry_idx(series, symbol, t)
    if idx is None:
        return None
    _, closes = series[symbol]
    exit_idx = idx + bars
    if exit_idx >= len(closes) or closes[idx] <= 0:
        return None
    return float(closes[exit_idx] / closes[idx] - 1.0)


def fwd_return_elapsed(
    series: dict[str, PriceSeries],
    symbol: str,
    t: datetime,
    hours: int,
) -> float | None:
    idx = _entry_idx(series, symbol, t)
    if idx is None:
        return None
    dates, closes = series[symbol]
    target = dates[idx] + np.timedelta64(int(hours), "h")
    exit_idx = int(np.searchsorted(dates, target, side="left"))
    if exit_idx >= len(closes) or closes[idx] <= 0:
        return None
    return float(closes[exit_idx] / closes[idx] - 1.0)


def return_between(series: dict[str, PriceSeries], symbol: str, start: datetime, end: datetime) -> float | None:
    if symbol not in series:
        return None
    start_idx = _entry_idx(series, symbol, start)
    end_idx = _before_idx(series, symbol, end)
    if start_idx is None or end_idx is None or end_idx <= start_idx:
        return None
    _, closes = series[symbol]
    if closes[start_idx] <= 0:
        return None
    return float(closes[end_idx] / closes[start_idx] - 1.0)


def hedged_forward_bars(
    series: dict[str, PriceSeries],
    row: dict[str, Any],
    bars: int,
) -> tuple[float | None, float | None, str]:
    asset = fwd_return_bars(series, str(row["symbol"]), row["first_pass_at"], bars)
    hedge = hedge_symbol(row)
    hedge_ret = fwd_return_bars(series, hedge, row["first_pass_at"], bars)
    if hedge_ret is None and hedge != SPY:
        hedge = SPY
        hedge_ret = fwd_return_bars(series, hedge, row["first_pass_at"], bars)
    return asset, (asset - hedge_ret if asset is not None and hedge_ret is not None else None), hedge


def hedged_forward_elapsed(
    series: dict[str, PriceSeries],
    row: dict[str, Any],
    hours: int,
) -> tuple[float | None, float | None, float | None, str]:
    asset = fwd_return_elapsed(series, str(row["symbol"]), row["first_pass_at"], hours)
    hedge = hedge_symbol(row)
    hedge_ret = fwd_return_elapsed(series, hedge, row["first_pass_at"], hours)
    if hedge_ret is None and hedge != SPY:
        hedge = SPY
        hedge_ret = fwd_return_elapsed(series, hedge, row["first_pass_at"], hours)
    hedged = asset - hedge_ret if asset is not None and hedge_ret is not None else None
    return asset, hedge_ret, hedged, hedge


def unconditional_return_elapsed(series: dict[str, PriceSeries], symbol: str, hours: int) -> float | None:
    if symbol not in series:
        return None
    dates, closes = series[symbol]
    if len(closes) < 2:
        return None
    returns = []
    delta = np.timedelta64(int(hours), "h")
    for idx in range(len(closes)):
        exit_idx = int(np.searchsorted(dates, dates[idx] + delta, side="left"))
        if exit_idx < len(closes) and closes[idx] > 0:
            returns.append(closes[exit_idx] / closes[idx] - 1.0)
    return float(np.mean(returns)) if returns else None


def probability_at_or_before(
    series: dict[str, ProbabilitySeries],
    market_id: str,
    t: datetime,
) -> float | None:
    if market_id not in series:
        return None
    dates, probabilities, _ = series[market_id]
    idx = int(np.searchsorted(dates, np_dt(t), side="right")) - 1
    if idx < 0:
        return None
    return float(probabilities[idx])


def probability_at_or_after(
    series: dict[str, ProbabilitySeries],
    market_id: str,
    t: datetime,
) -> float | None:
    if market_id not in series:
        return None
    dates, probabilities, _ = series[market_id]
    idx = int(np.searchsorted(dates, np_dt(t), side="left"))
    if idx >= len(probabilities):
        return None
    return float(probabilities[idx])


def probability_volume_sum(
    series: dict[str, ProbabilitySeries],
    market_id: str,
    start: datetime,
    end: datetime,
) -> float | None:
    if market_id not in series:
        return None
    dates, _, volumes = series[market_id]
    left = int(np.searchsorted(dates, np_dt(start), side="left"))
    right = int(np.searchsorted(dates, np_dt(end), side="right"))
    values = volumes[left:right]
    values = values[np.isfinite(values)]
    return float(values.sum()) if values.size else None


def stats(values: Iterable[float | None]) -> dict[str, Any]:
    arr = np.array([float(value) for value in values if value is not None and np.isfinite(value)], dtype=float)
    if arr.size == 0:
        return {"n": 0}
    std = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
    return {
        "n": int(arr.size),
        "mean": float(arr.mean()),
        "median": float(np.median(arr)),
        "hit": float((arr > 0).mean()),
        "std": std,
        "sharpe_trade": float(arr.mean() / std) if std > 0 else float("nan"),
        "t_stat": float(arr.mean() / (std / np.sqrt(arr.size))) if std > 0 and arr.size > 1 else float("nan"),
        "net_per_1k": float(arr.sum() * TRADE_NOTIONAL),
    }


def cluster_bootstrap_mean_ci(
    rows: Iterable[dict[str, Any]],
    value_key: str,
    *,
    cluster_key: str = "event_id",
    samples: int = 2000,
    seed: int = 20260623,
) -> tuple[float, float] | None:
    clusters: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = row.get(value_key)
        if value is not None and np.isfinite(value):
            clusters[str(row[cluster_key])].append(float(value))
    cluster_ids = sorted(clusters)
    if len(cluster_ids) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        sampled = [rng.choice(cluster_ids) for _ in cluster_ids]
        values = [value for cluster_id in sampled for value in clusters[cluster_id]]
        means.append(float(np.mean(values)))
    means.sort()
    return means[int(samples * 0.025)], means[int(samples * 0.975)]


def cluster_bootstrap_hit_ci(
    rows: Iterable[dict[str, Any]],
    *,
    cluster_key: str = "event_id",
    samples: int = 2000,
    seed: int = 20260623,
) -> tuple[float, float] | None:
    clusters: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        clusters[str(row[cluster_key])].append(1.0 if row["prediction"] == row["target_sign"] else 0.0)
    cluster_ids = sorted(clusters)
    if len(cluster_ids) < 2:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(samples):
        sampled = [rng.choice(cluster_ids) for _ in cluster_ids]
        values = [value for cluster_id in sampled for value in clusters[cluster_id]]
        means.append(float(np.mean(values)))
    means.sort()
    return means[int(samples * 0.025)], means[int(samples * 0.975)]


def ci_pct(ci: tuple[float, float] | None) -> str:
    if ci is None:
        return "[na, na]"
    return f"[{ci[0] * 100:+.2f}%, {ci[1] * 100:+.2f}%]"


def ci_rate(ci: tuple[float, float] | None) -> str:
    if ci is None:
        return "[na, na]"
    return f"[{ci[0]:.0%}, {ci[1]:.0%}]"


def summarize_dataset(rows: list[dict[str, Any]], meta: dict[str, Any]) -> None:
    counts: dict[str, int] = defaultdict(int)
    events: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        counts[str(row["event_archetype"])] += 1
        events[str(row["event_archetype"])].add(str(row["event_id"]))
    print(
        f"runs={len(meta['run_ids'])} statuses={','.join(meta['statuses'])} "
        f"source_rows={meta['source_rows']} unique_candidates={meta['unique_candidates']} "
        f"deduped={meta['deduped']}"
    )
    for archetype in sorted(counts, key=lambda key: (-counts[key], key)):
        print(f"  {archetype:48} n={counts[archetype]:5} events={len(events[archetype]):4}")


def hours_ago(t: datetime, hours: int) -> datetime:
    return t - timedelta(hours=hours)
