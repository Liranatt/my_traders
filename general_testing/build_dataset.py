"""Build the shared alpha candidate dataset and Model-1 scores.

Outputs:
  data/candidates.parquet
  ALPHA_RESULTS.md

The label is the 10-trading-day sector-hedged return from the first daily close
at/after the 55% crossing to the earlier of 10 trading days or one trading day
before resolution.

Usage:
  .venv/Scripts/python.exe -m general_testing.build_dataset
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.impute import SimpleImputer
from sklearn.linear_model import HuberRegressor
from sklearn.metrics import mean_absolute_error
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from database.backtesting.schema import SCHEMA
from database.db_connection import connect
from general_testing.diffusion_research_helpers import (
    DEFAULT_RUN_STATUSES,
    SPY,
    load_observations,
    load_price_series,
    load_probability_series,
    probability_at_or_after,
    probability_at_or_before,
)

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional at import time
    yf = None

try:
    from xgboost import XGBRegressor
except Exception:  # pragma: no cover - optional dependency
    XGBRegressor = None


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = REPO_ROOT / "data" / "candidates.parquet"
DEFAULT_RESULTS = REPO_ROOT / "ALPHA_RESULTS.md"
DAILY_CLOSE_OFFSET = np.timedelta64(390, "m")
TODAY_UTC = datetime.now(timezone.utc).date()

TRAIN_END = datetime(2025, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
VAL_START = datetime(2026, 1, 10, tzinfo=timezone.utc)
VAL_END = datetime(2026, 3, 31, 23, 59, 59, tzinfo=timezone.utc)
TEST_START = datetime(2026, 4, 10, tzinfo=timezone.utc)

MOMENTUM_RANGE_PERIOD = 14
MOMENTUM_RANGE_MULTIPLIER = 3.0

CONTRACT_COLUMNS = [
    "run_id",
    "event_id",
    "market_id",
    "symbol",
    "pass_number",
    "t0",
    "t_theta",
    "t_e",
    "entry_price",
    "sector_etf",
    "y_hedged",
    "realized_dir",
    "realized_abs_move",
    "alpha_score",
    "alpha_dir",
    "split",
]

NUMERIC_FEATURES = [
    "feat_prob_at_trigger",
    "feat_prob_slope_24h",
    "feat_prob_volatility",
    "feat_prob_surge_since_t0",
    "feat_time_to_resolution_days",
    "feat_crossing_latency_days",
    "feat_pre_entry_volume_log",
    "feat_connection_strength",
    "feat_world_size",
    "feat_runup_since_t0",
    "feat_asset_2w_trend",
    "feat_sector_1m_trend",
    "feat_spy_2w_trend",
    "feat_ytd_change",
    "feat_debt_to_equity",
    "feat_cash_to_marketcap",
    "feat_beta",
    "feat_profit_margin",
    "feat_log_market_cap",
    "feat_runup_rank",
    "feat_size_rank",
    "feat_ml_class_prob",
    "feat_ml_pred_peak",
    "feat_momentum_roc",
]

CATEGORICAL_FEATURES = [
    "feat_archetype",
    "feat_asset_role",
    "feat_sector",
    "feat_ml_dir",
]

FEATURE_GROUPS = {
    "oracle": [
        "feat_prob_at_trigger",
        "feat_prob_slope_24h",
        "feat_prob_volatility",
        "feat_prob_surge_since_t0",
        "feat_time_to_resolution_days",
        "feat_crossing_latency_days",
        "feat_pre_entry_volume_log",
    ],
    "world": ["feat_connection_strength", "feat_archetype", "feat_world_size", "feat_asset_role"],
    "price": [
        "feat_runup_since_t0",
        "feat_asset_2w_trend",
        "feat_sector_1m_trend",
        "feat_spy_2w_trend",
        "feat_ytd_change",
    ],
    "fundamentals": [
        "feat_debt_to_equity",
        "feat_cash_to_marketcap",
        "feat_beta",
        "feat_profit_margin",
        "feat_log_market_cap",
        "feat_sector",
    ],
    "cross_sectional": ["feat_runup_rank", "feat_size_rank"],
    "old_ml_momentum": ["feat_ml_class_prob", "feat_ml_pred_peak", "feat_ml_dir", "feat_momentum_roc"],
}


@dataclass(frozen=True)
class PricePoint:
    idx: int
    close_ts: np.datetime64
    close: float


def json_value(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def np_dt(value: datetime) -> np.datetime64:
    value = as_utc(value).replace(tzinfo=None)
    return np.datetime64(value, "ns")


def finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        output = float(value)
    except (TypeError, ValueError):
        return None
    return output if np.isfinite(output) else None


def safe_div(num: Any, den: Any) -> float | None:
    num_f = finite_float(num)
    den_f = finite_float(den)
    if num_f is None or den_f is None or den_f == 0:
        return None
    return num_f / den_f


def sign(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


def split_for_ttheta(value: datetime) -> str:
    t = as_utc(value)
    if t <= TRAIN_END:
        return "train"
    if VAL_START <= t <= VAL_END:
        return "val"
    if t >= TEST_START:
        return "test"
    return "embargo"


def one_hot_encoder() -> OneHotEncoder:
    try:
        return OneHotEncoder(handle_unknown="ignore", sparse_output=False)
    except TypeError:
        return OneHotEncoder(handle_unknown="ignore", sparse=False)


def close_times(series: tuple[np.ndarray, np.ndarray]) -> np.ndarray:
    return series[0] + DAILY_CLOSE_OFFSET


def first_close_on_or_after(
    prices: dict[str, tuple[np.ndarray, np.ndarray]],
    symbol: str,
    timestamp: datetime | np.datetime64,
) -> PricePoint | None:
    series = prices.get(symbol)
    if series is None:
        return None
    ts = close_times(series)
    target = timestamp if isinstance(timestamp, np.datetime64) else np_dt(timestamp)
    idx = int(np.searchsorted(ts, target, side="left"))
    if idx >= len(ts):
        return None
    return PricePoint(idx=idx, close_ts=ts[idx], close=float(series[1][idx]))


def last_close_on_or_before(
    prices: dict[str, tuple[np.ndarray, np.ndarray]],
    symbol: str,
    timestamp: datetime,
) -> PricePoint | None:
    series = prices.get(symbol)
    if series is None:
        return None
    ts = close_times(series)
    idx = int(np.searchsorted(ts, np_dt(timestamp), side="right")) - 1
    if idx < 0:
        return None
    return PricePoint(idx=idx, close_ts=ts[idx], close=float(series[1][idx]))


def return_between_closes(
    prices: dict[str, tuple[np.ndarray, np.ndarray]],
    symbol: str,
    start: datetime,
    end: datetime,
) -> float | None:
    start_point = first_close_on_or_after(prices, symbol, start)
    end_point = last_close_on_or_before(prices, symbol, end)
    if start_point is None or end_point is None or end_point.idx <= start_point.idx:
        return None
    if start_point.close <= 0:
        return None
    return end_point.close / start_point.close - 1.0


def return_at_close_times(
    prices: dict[str, tuple[np.ndarray, np.ndarray]],
    symbol: str,
    entry_ts: np.datetime64,
    exit_ts: np.datetime64,
) -> float | None:
    entry = first_close_on_or_after(prices, symbol, entry_ts)
    exit_ = first_close_on_or_after(prices, symbol, exit_ts)
    if entry is None or exit_ is None or exit_.idx < entry.idx or entry.close <= 0:
        return None
    if exit_.idx == entry.idx:
        return 0.0
    return exit_.close / entry.close - 1.0


def label_for_candidate(
    prices: dict[str, tuple[np.ndarray, np.ndarray]],
    *,
    symbol: str,
    sector_etf: str,
    t_theta: datetime,
    t_e: datetime,
    max_holding_days: int,
) -> dict[str, Any] | None:
    series = prices.get(symbol)
    if series is None:
        return None
    entry = first_close_on_or_after(prices, symbol, t_theta)
    if entry is None or entry.close <= 0:
        return None
    asset_close_ts = close_times(series)
    event_close_idx = int(np.searchsorted(asset_close_ts, np_dt(t_e), side="left"))
    if event_close_idx >= len(asset_close_ts):
        event_minus_one_idx = len(asset_close_ts) - 1
    else:
        event_minus_one_idx = event_close_idx - 1
    event_minus_one_idx = max(entry.idx, event_minus_one_idx)
    exit_idx = min(entry.idx + max_holding_days, event_minus_one_idx)
    if exit_idx < entry.idx or exit_idx >= len(asset_close_ts):
        return None
    exit_close = float(series[1][exit_idx])
    if exit_close <= 0:
        return None
    asset_ret = 0.0 if exit_idx == entry.idx else exit_close / entry.close - 1.0
    exit_ts = asset_close_ts[exit_idx]
    hedge_symbol = sector_etf or SPY
    hedge_ret = return_at_close_times(prices, hedge_symbol, entry.close_ts, exit_ts)
    if hedge_ret is None and hedge_symbol != SPY:
        hedge_symbol = SPY
        hedge_ret = return_at_close_times(prices, hedge_symbol, entry.close_ts, exit_ts)
    if hedge_ret is None:
        return None
    return {
        "entry_price": entry.close,
        "entry_close_ts": entry.close_ts,
        "exit_close_ts": exit_ts,
        "exit_price": exit_close,
        "sector_etf": hedge_symbol,
        "asset_return": asset_ret,
        "sector_return": hedge_ret,
        "y_hedged": asset_ret - hedge_ret,
    }


async def ensure_alpha_schema(conn: Any) -> None:
    await conn.execute(
        f"""
        ALTER TABLE {SCHEMA}.historical_asset_world_assets
            ADD COLUMN IF NOT EXISTS connection_strength DOUBLE PRECISION;

        CREATE TABLE IF NOT EXISTS {SCHEMA}.historical_asset_fundamentals (
            symbol                  TEXT NOT NULL,
            as_of                   DATE NOT NULL,
            debt_to_equity          DOUBLE PRECISION,
            total_debt              DOUBLE PRECISION,
            total_cash              DOUBLE PRECISION,
            market_cap              DOUBLE PRECISION,
            beta                    DOUBLE PRECISION,
            profit_margin           DOUBLE PRECISION,
            free_cash_flow          DOUBLE PRECISION,
            updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (symbol, as_of)
        );

        CREATE INDEX IF NOT EXISTS idx_historical_asset_fundamentals_symbol_updated
            ON {SCHEMA}.historical_asset_fundamentals(symbol, updated_at DESC);
        """
    )


def extract_fundamentals(symbol: str) -> dict[str, Any]:
    if yf is None:
        raise RuntimeError("yfinance is not installed")
    info = yf.Ticker(symbol).info or {}
    return {
        "symbol": symbol,
        "debt_to_equity": finite_float(info.get("debtToEquity")),
        "total_debt": finite_float(info.get("totalDebt")),
        "total_cash": finite_float(info.get("totalCash")),
        "market_cap": finite_float(info.get("marketCap")),
        "beta": finite_float(info.get("beta")),
        "profit_margin": finite_float(info.get("profitMargins")),
        "free_cash_flow": finite_float(info.get("freeCashflow", info.get("freeCashFlow"))),
    }


async def existing_fundamental_symbols(conn: Any, symbols: Iterable[str], as_of: date) -> set[str]:
    wanted = sorted(set(symbols))
    if not wanted:
        return set()
    rows = await conn.fetch(
        f"""
        SELECT symbol
        FROM {SCHEMA}.historical_asset_fundamentals
        WHERE symbol = ANY($1::text[]) AND as_of = $2
        """,
        wanted,
        as_of,
    )
    return {str(row["symbol"]) for row in rows}


async def save_fundamentals(conn: Any, rows: list[dict[str, Any]], as_of: date) -> None:
    if not rows:
        return
    await conn.executemany(
        f"""
        INSERT INTO {SCHEMA}.historical_asset_fundamentals (
            symbol, as_of, debt_to_equity, total_debt, total_cash, market_cap,
            beta, profit_margin, free_cash_flow, updated_at
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW())
        ON CONFLICT (symbol, as_of) DO UPDATE SET
            debt_to_equity = EXCLUDED.debt_to_equity,
            total_debt = EXCLUDED.total_debt,
            total_cash = EXCLUDED.total_cash,
            market_cap = EXCLUDED.market_cap,
            beta = EXCLUDED.beta,
            profit_margin = EXCLUDED.profit_margin,
            free_cash_flow = EXCLUDED.free_cash_flow,
            updated_at = NOW()
        """,
        [
            (
                row["symbol"],
                as_of,
                row.get("debt_to_equity"),
                row.get("total_debt"),
                row.get("total_cash"),
                row.get("market_cap"),
                row.get("beta"),
                row.get("profit_margin"),
                row.get("free_cash_flow"),
            )
            for row in rows
        ],
    )


async def refresh_fundamentals(
    conn: Any,
    symbols: list[str],
    *,
    as_of: date,
    workers: int,
    limit: int | None,
) -> dict[str, Any]:
    if yf is None:
        return {
            "attempted": 0,
            "stored": 0,
            "failed": len(symbols),
            "failures": [{"symbol": symbol, "error": "yfinance is not installed"} for symbol in symbols[:20]],
        }
    existing = await existing_fundamental_symbols(conn, symbols, as_of)
    pending = [symbol for symbol in symbols if symbol not in existing]
    if limit is not None:
        pending = pending[:limit]
    rows: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {pool.submit(extract_fundamentals, symbol): symbol for symbol in pending}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    rows.append(future.result())
                except Exception as error:
                    failures.append({"symbol": symbol, "error": str(error)[:240]})
    await save_fundamentals(conn, rows, as_of)
    return {
        "attempted": len(pending),
        "stored": len(rows),
        "failed": len(failures),
        "already_current": len(existing),
        "failures": failures[:30],
        "as_of": str(as_of),
        "point_in_time_caveat": (
            "yfinance .info is point-in-time-now, not as-of-event; fundamentals may "
            "contain mild look-ahead versus historical event dates."
        ),
    }


async def load_fundamentals(conn: Any, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    wanted = sorted(set(symbols))
    if not wanted:
        return {}
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT ON (symbol)
               symbol, as_of, debt_to_equity, total_debt, total_cash, market_cap,
               beta, profit_margin, free_cash_flow, updated_at
        FROM {SCHEMA}.historical_asset_fundamentals
        WHERE symbol = ANY($1::text[])
        ORDER BY symbol, as_of DESC, updated_at DESC
        """,
        wanted,
    )
    return {str(row["symbol"]): dict(row) for row in rows}


async def load_asset_metadata(conn: Any, symbols: Iterable[str]) -> dict[str, dict[str, Any]]:
    wanted = sorted(set(symbols))
    if not wanted:
        return {}
    rows = await conn.fetch(
        f"""
        SELECT symbol, sector, sector_etf, benchmark_symbol
        FROM {SCHEMA}.historical_asset_metadata
        WHERE symbol = ANY($1::text[])
        """,
        wanted,
    )
    return {str(row["symbol"]): dict(row) for row in rows}


async def load_world_features(conn: Any, run_ids: list[str]) -> dict[tuple[str, str, int, str], dict[str, Any]]:
    rows = await conn.fetch(
        f"""
        WITH resolved AS (
            SELECT DISTINCT ON (
                       rw.run_id, rw.market_id, rw.pass_number,
                       COALESCE(r.resolved_symbol, a.symbol)
                   )
                   rw.run_id::text AS run_id,
                   rw.market_id,
                   rw.pass_number,
                   COALESCE(r.resolved_symbol, a.symbol) AS symbol,
                   a.asset_class,
                   a.reason,
                   a.connection_strength
            FROM {SCHEMA}.historical_run_worlds rw
            JOIN {SCHEMA}.historical_asset_worlds w ON w.world_id = rw.world_id
            JOIN {SCHEMA}.historical_asset_world_assets a ON a.world_id = rw.world_id
            LEFT JOIN {SCHEMA}.historical_run_asset_resolutions r
              ON r.run_id = rw.run_id AND r.original_symbol = a.symbol
            WHERE rw.run_id::text = ANY($1::text[])
              AND COALESCE(r.resolved_symbol, a.symbol) IS NOT NULL
            ORDER BY rw.run_id, rw.market_id, rw.pass_number,
                     COALESCE(r.resolved_symbol, a.symbol),
                     CASE WHEN a.symbol = COALESCE(r.resolved_symbol, a.symbol) THEN 0 ELSE 1 END,
                     a.symbol
        )
        SELECT *, count(*) OVER (PARTITION BY run_id, market_id, pass_number) AS world_size
        FROM resolved
        """,
        run_ids,
    )
    out: dict[tuple[str, str, int, str], dict[str, Any]] = {}
    for row in rows:
        key = (str(row["run_id"]), str(row["market_id"]), int(row["pass_number"]), str(row["symbol"]))
        out[key] = {
            "asset_class": row["asset_class"],
            "reason": row["reason"],
            "connection_strength": finite_float(row["connection_strength"]),
            "world_size": int(row["world_size"]),
        }
    return out


async def load_old_predictions(conn: Any, run_ids: list[str]) -> dict[tuple[str, str, int, str], dict[str, Any]]:
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT ON (event_id, market_id, pass_number, symbol)
               event_id, market_id, pass_number, symbol, direction,
               classification_probability, predicted_peak_percent, created_at
        FROM {SCHEMA}.historical_ml_predictions
        WHERE run_id::text = ANY($1::text[])
        ORDER BY event_id, market_id, pass_number, symbol, created_at DESC
        """,
        run_ids,
    )
    return {
        (str(row["event_id"]), str(row["market_id"]), int(row["pass_number"]), str(row["symbol"])): dict(row)
        for row in rows
    }


async def load_momentum(conn: Any, run_ids: list[str]) -> dict[tuple[str, str, int, str], float]:
    rows = await conn.fetch(
        f"""
        SELECT DISTINCT ON (event_id, market_id, pass_number, symbol)
               event_id, market_id, pass_number, symbol, momentum_value, created_at
        FROM {SCHEMA}.historical_momentum_parameter_results
        WHERE run_id::text = ANY($1::text[])
          AND range_period = $2
          AND range_multiplier = $3
        ORDER BY event_id, market_id, pass_number, symbol, created_at DESC
        """,
        run_ids,
        MOMENTUM_RANGE_PERIOD,
        MOMENTUM_RANGE_MULTIPLIER,
    )
    return {
        (str(row["event_id"]), str(row["market_id"]), int(row["pass_number"]), str(row["symbol"])): float(row["momentum_value"])
        for row in rows
        if row["momentum_value"] is not None
    }


def world_lookup(
    world_features: dict[tuple[str, str, int, str], dict[str, Any]],
    row: dict[str, Any],
) -> dict[str, Any]:
    pass_number = int(row["first_pass_number"])
    for run_id in [str(row["run_id"]), *[str(x) for x in row.get("source_run_ids", [])]]:
        item = world_features.get((run_id, str(row["market_id"]), pass_number, str(row["symbol"])))
        if item:
            return item
    return {}


def add_rank_features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    cohort_cols = ["event_id", "market_id", "pass_number"]
    out["feat_runup_rank"] = out.groupby(cohort_cols)["feat_runup_since_t0"].rank(pct=True, method="average")
    out["feat_size_rank"] = out.groupby(cohort_cols)["feat_log_market_cap"].rank(pct=True, method="average")
    return out


def pre_drop_earnings_defense(archetype: str) -> bool:
    text = (archetype or "").lower()
    return not ("earnings" in text or "defense" in text)


def build_rows(
    observations: list[dict[str, Any]],
    *,
    prices: dict[str, tuple[np.ndarray, np.ndarray]],
    probabilities: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]],
    fundamentals: dict[str, dict[str, Any]],
    metadata: dict[str, dict[str, Any]],
    world_features: dict[tuple[str, str, int, str], dict[str, Any]],
    predictions: dict[tuple[str, str, int, str], dict[str, Any]],
    momentum: dict[tuple[str, str, int, str], float],
    max_holding_days: int,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    records: list[dict[str, Any]] = []
    dropped: list[dict[str, str]] = []
    for row in observations:
        symbol = str(row["symbol"])
        features = row.get("features") or {}
        research = row.get("research_data") or {}
        sector_etf = str(row.get("sector_etf") or SPY)
        label = label_for_candidate(
            prices,
            symbol=symbol,
            sector_etf=sector_etf,
            t_theta=row["first_pass_at"],
            t_e=row["label_available_at"],
            max_holding_days=max_holding_days,
        )
        if label is None:
            dropped.append(
                {
                    "event_id": str(row["event_id"]),
                    "market_id": str(row["market_id"]),
                    "symbol": symbol,
                    "reason": "missing_entry_exit_or_hedge_price",
                }
            )
            continue

        market_id = str(row["market_id"])
        t_theta = row["first_pass_at"]
        p_now = probability_at_or_before(probabilities, market_id, t_theta)
        if p_now is None:
            p_now = finite_float(features.get("polymarket_probability_at_trigger"))
        p_t0 = probability_at_or_before(probabilities, market_id, row["t0"])
        if p_t0 is None:
            p_t0 = probability_at_or_after(probabilities, market_id, row["t0"])
        runup = finite_float(features.get("query_runup_since_creation"))
        if runup is None:
            runup = return_between_closes(prices, symbol, row["t0"], t_theta)

        fund = fundamentals.get(symbol, {})
        meta = metadata.get(symbol, {})
        market_cap = finite_float(fund.get("market_cap"))
        total_cash = finite_float(fund.get("total_cash"))
        world = world_lookup(world_features, row)
        pred_key = (str(row["event_id"]), market_id, int(row["first_pass_number"]), symbol)
        pred = predictions.get(pred_key, {})
        ml_dir = pred.get("direction") or "missing"
        mom = momentum.get(pred_key)
        if mom is None:
            mom = finite_float(features.get("asset_two_week_trend"))
        max_change = research.get("maximum_change", research.get("max_change"))
        min_change = research.get("minimum_change", research.get("min_change"))
        realized_abs_move = max(abs(finite_float(max_change) or 0.0), abs(finite_float(min_change) or 0.0))
        archetype = str(row.get("event_archetype") or row.get("semantic_group") or "unknown")

        record = {
            "run_id": str(row["run_id"]),
            "event_id": str(row["event_id"]),
            "market_id": market_id,
            "symbol": symbol,
            "pass_number": int(row["first_pass_number"]),
            "t0": row["t0"],
            "t_theta": t_theta,
            "t_e": row["label_available_at"],
            "entry_price": label["entry_price"],
            "entry_close_ts": str(label["entry_close_ts"]),
            "exit_close_ts": str(label["exit_close_ts"]),
            "exit_price": label["exit_price"],
            "sector_etf": label["sector_etf"],
            "asset_return": label["asset_return"],
            "sector_return": label["sector_return"],
            "y_hedged": label["y_hedged"],
            "realized_dir": sign(float(label["y_hedged"])),
            "realized_abs_move": realized_abs_move,
            "feat_prob_at_trigger": p_now,
            "feat_prob_slope_24h": finite_float(features.get("polymarket_probability_slope_24h")),
            "feat_prob_volatility": finite_float(features.get("polymarket_probability_volatility")),
            "feat_prob_surge_since_t0": (p_now - p_t0) if p_now is not None and p_t0 is not None else None,
            "feat_time_to_resolution_days": finite_float(features.get("polymarket_time_to_resolution_days")),
            "feat_crossing_latency_days": finite_float(features.get("polymarket_crossing_latency_days")),
            "feat_pre_entry_volume_log": finite_float(features.get("polymarket_pre_entry_volume_log")),
            "feat_connection_strength": world.get("connection_strength") if world.get("connection_strength") is not None else 1.0,
            "feat_archetype": archetype,
            "feat_world_size": world.get("world_size") or 1,
            "feat_asset_role": row.get("asset_role") or "ambiguous",
            "feat_runup_since_t0": runup,
            "feat_asset_2w_trend": finite_float(features.get("asset_two_week_trend")),
            "feat_sector_1m_trend": finite_float(features.get("sector_one_month_trend")),
            "feat_spy_2w_trend": finite_float(features.get("spy_two_week_trend")),
            "feat_ytd_change": finite_float(features.get("asset_ytd_change")),
            "feat_debt_to_equity": finite_float(fund.get("debt_to_equity")),
            "feat_cash_to_marketcap": safe_div(total_cash, market_cap),
            "feat_beta": finite_float(fund.get("beta")),
            "feat_profit_margin": finite_float(fund.get("profit_margin")),
            "feat_log_market_cap": math.log(market_cap) if market_cap and market_cap > 0 else None,
            "feat_sector": row.get("sector") or meta.get("sector") or "Unknown",
            "feat_ml_class_prob": finite_float(pred.get("classification_probability")) if pred else 0.5,
            "feat_ml_pred_peak": finite_float(pred.get("predicted_peak_percent")) if pred else 0.0,
            "feat_ml_dir": str(ml_dir),
            "feat_momentum_roc": mom,
            "pre_drop_earnings_defense": pre_drop_earnings_defense(archetype),
            "split": split_for_ttheta(t_theta),
        }
        records.append(record)
    df = pd.DataFrame.from_records(records)
    if not df.empty:
        df = add_rank_features(df)
    diagnostics = {
        "dropped_label_rows": len(dropped),
        "dropped_examples": dropped[:20],
    }
    return df, diagnostics


def impute_required_features(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    out = df.copy()
    missing_before = {col: int(out[col].isna().sum()) for col in NUMERIC_FEATURES if col in out.columns}
    fill_values: dict[str, float] = {}
    for col in NUMERIC_FEATURES:
        values = pd.to_numeric(out[col], errors="coerce")
        fill = values.median(skipna=True)
        if pd.isna(fill):
            fill = 0.0
        out[col] = values.fillna(float(fill))
        fill_values[col] = float(fill)
    for col in CATEGORICAL_FEATURES:
        out[col] = out[col].fillna("Unknown").astype(str)
        out.loc[out[col].str.len() == 0, col] = "Unknown"
    missing_after = {col: int(out[col].isna().sum()) for col in NUMERIC_FEATURES if col in out.columns}
    return out, {
        "missing_before": missing_before,
        "missing_after": missing_after,
        "fill_values": fill_values,
    }


def rank_corr(y_true: Iterable[float], y_pred: Iterable[float]) -> float:
    left = pd.Series(list(y_true), dtype=float)
    right = pd.Series(list(y_pred), dtype=float)
    if len(left) < 2 or left.nunique() < 2 or right.nunique() < 2:
        return float("nan")
    return float(left.corr(right, method="spearman"))


def top_quintile_mean(y_true: Iterable[float], y_pred: Iterable[float]) -> float:
    frame = pd.DataFrame({"y": list(y_true), "score": list(y_pred)}).dropna()
    if frame.empty:
        return float("nan")
    n = max(1, int(math.ceil(len(frame) * 0.2)))
    return float(frame.nlargest(n, "score")["y"].mean())


def long_short_spread(y_true: Iterable[float], y_pred: Iterable[float]) -> float:
    frame = pd.DataFrame({"y": list(y_true), "score": list(y_pred)}).dropna()
    if frame.empty:
        return float("nan")
    n = max(1, int(math.ceil(len(frame) * 0.2)))
    return float(frame.nlargest(n, "score")["y"].mean() - frame.nsmallest(n, "score")["y"].mean())


def model_metrics(df: pd.DataFrame, pred: np.ndarray, split: str) -> dict[str, Any]:
    mask = df["split"].eq(split).to_numpy()
    if not mask.any():
        return {"split": split, "n": 0}
    y = df.loc[mask, "y_hedged"].astype(float).to_numpy()
    p = pred[mask]
    return {
        "split": split,
        "n": int(mask.sum()),
        "rank_corr": rank_corr(y, p),
        "top_quintile_mean_y": top_quintile_mean(y, p),
        "long_short_quintile_spread": long_short_spread(y, p),
        "mae": float(mean_absolute_error(y, p)),
        "mean_y": float(np.mean(y)),
        "mean_score": float(np.mean(p)),
    }


def preprocessor(numeric_features: list[str], categorical_features: list[str], *, scale_numeric: bool) -> ColumnTransformer:
    numeric_steps: list[tuple[str, Any]] = [("imputer", SimpleImputer(strategy="median"))]
    if scale_numeric:
        numeric_steps.append(("scaler", StandardScaler()))
    return ColumnTransformer(
        transformers=[
            ("num", Pipeline(numeric_steps), numeric_features),
            (
                "cat",
                Pipeline(
                    [
                        ("imputer", SimpleImputer(strategy="constant", fill_value="Unknown")),
                        ("onehot", one_hot_encoder()),
                    ]
                ),
                categorical_features,
            ),
        ],
        remainder="drop",
    )


def fit_linear_baseline(
    train: pd.DataFrame,
    features_num: list[str],
    features_cat: list[str],
) -> Pipeline:
    model = Pipeline(
        [
            ("prep", preprocessor(features_num, features_cat, scale_numeric=True)),
            ("model", HuberRegressor(alpha=0.01, epsilon=1.35, max_iter=1000)),
        ]
    )
    model.fit(train[features_num + features_cat], train["y_hedged"].astype(float))
    return model


def fit_tree_model(
    train: pd.DataFrame,
    val: pd.DataFrame,
    features_num: list[str],
    features_cat: list[str],
) -> tuple[Any, dict[str, Any]]:
    feature_cols = features_num + features_cat
    if XGBRegressor is not None and len(val) > 0:
        prep = preprocessor(features_num, features_cat, scale_numeric=False)
        x_train = prep.fit_transform(train[feature_cols])
        x_val = prep.transform(val[feature_cols])
        model = XGBRegressor(
            objective="reg:pseudohubererror",
            n_estimators=800,
            learning_rate=0.03,
            max_depth=3,
            min_child_weight=5,
            subsample=0.7,
            colsample_bytree=0.7,
            reg_lambda=5.0,
            reg_alpha=0.1,
            random_state=42,
            n_jobs=4,
        )
        try:
            model.fit(
                x_train,
                train["y_hedged"].astype(float),
                eval_set=[(x_val, val["y_hedged"].astype(float))],
                verbose=False,
                early_stopping_rounds=30,
            )
        except TypeError:
            model.fit(x_train, train["y_hedged"].astype(float), eval_set=[(x_val, val["y_hedged"].astype(float))], verbose=False)
        pipe = Pipeline([("prep", prep), ("model", model)])
        return pipe, {"model": "xgboost", "objective": "reg:pseudohubererror"}

    best: tuple[float, int, Pipeline] | None = None
    for n_estimators in [50, 100, 200, 400]:
        pipe = Pipeline(
            [
                ("prep", preprocessor(features_num, features_cat, scale_numeric=False)),
                (
                    "model",
                    GradientBoostingRegressor(
                        loss="huber",
                        learning_rate=0.03,
                        n_estimators=n_estimators,
                        max_depth=3,
                        min_samples_leaf=5,
                        subsample=0.7,
                        random_state=42,
                    ),
                ),
            ]
        )
        pipe.fit(train[feature_cols], train["y_hedged"].astype(float))
        if len(val) > 0:
            pred = pipe.predict(val[feature_cols])
            score = rank_corr(val["y_hedged"].astype(float), pred)
            if not np.isfinite(score):
                score = -mean_absolute_error(val["y_hedged"].astype(float), pred)
        else:
            pred = pipe.predict(train[feature_cols])
            score = -mean_absolute_error(train["y_hedged"].astype(float), pred)
        if best is None or score > best[0]:
            best = (float(score), n_estimators, pipe)
    assert best is not None
    return best[2], {
        "model": "sklearn.GradientBoostingRegressor",
        "loss": "huber",
        "selected_n_estimators": best[1],
        "selection_score": best[0],
        "fallback_reason": "xgboost is not installed" if XGBRegressor is None else "validation set unavailable",
    }


def evaluate_ablation(
    df: pd.DataFrame,
    train: pd.DataFrame,
    val: pd.DataFrame,
    *,
    full_test_rank_corr: float,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for group, cols in FEATURE_GROUPS.items():
        num = [c for c in NUMERIC_FEATURES if c not in cols]
        cat = [c for c in CATEGORICAL_FEATURES if c not in cols]
        if not num and not cat:
            continue
        model, info = fit_tree_model(train, val, num, cat)
        preds = model.predict(df[num + cat])
        test = model_metrics(df, preds, "test")
        rank = test.get("rank_corr")
        rows.append(
            {
                "removed_group": group,
                "model": info["model"],
                "test_rank_corr": rank,
                "delta_vs_full": (
                    full_test_rank_corr - rank
                    if rank is not None and np.isfinite(rank) and np.isfinite(full_test_rank_corr)
                    else float("nan")
                ),
                "test_top_quintile_mean_y": test.get("top_quintile_mean_y"),
            }
        )
    return rows


def train_and_score(df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    train = df[df["split"].eq("train")].copy()
    val = df[df["split"].eq("val")].copy()
    if len(train) < 30:
        raise RuntimeError(f"Need at least 30 train rows; got {len(train)}")
    full_features = NUMERIC_FEATURES + CATEGORICAL_FEATURES
    linear = fit_linear_baseline(train, NUMERIC_FEATURES, CATEGORICAL_FEATURES)
    linear_pred = linear.predict(df[full_features])
    tree, model_info = fit_tree_model(train, val, NUMERIC_FEATURES, CATEGORICAL_FEATURES)
    tree_pred = tree.predict(df[full_features])
    out = df.copy()
    out["alpha_score"] = tree_pred.astype(float)
    out["alpha_dir"] = np.where(out["alpha_score"] >= 0, "long", "short")

    metrics = {
        "model_info": model_info,
        "split_counts": {str(k): int(v) for k, v in out["split"].value_counts().sort_index().items()},
        "linear_baseline": [model_metrics(out, linear_pred, split) for split in ["train", "val", "test"]],
        "alpha_model": [model_metrics(out, tree_pred, split) for split in ["train", "val", "test"]],
    }
    full_test = next((row for row in metrics["alpha_model"] if row["split"] == "test"), {})
    full_test_rank = finite_float(full_test.get("rank_corr")) or float("nan")
    metrics["ablations"] = evaluate_ablation(out, train, val, full_test_rank_corr=full_test_rank)
    train_rank = next((row.get("rank_corr") for row in metrics["alpha_model"] if row["split"] == "train"), float("nan"))
    test_rank = full_test.get("rank_corr", float("nan"))
    metrics["overfit_gap_rank_corr_train_minus_test"] = (
        train_rank - test_rank
        if train_rank is not None and test_rank is not None and np.isfinite(train_rank) and np.isfinite(test_rank)
        else float("nan")
    )
    metrics["success"] = bool(
        full_test.get("n", 0) > 0
        and full_test.get("rank_corr") is not None
        and full_test.get("rank_corr") > 0
        and full_test.get("top_quintile_mean_y") is not None
        and full_test.get("top_quintile_mean_y") > 0
    )
    return out, metrics


def fmt_pct(value: Any) -> str:
    value = finite_float(value)
    if value is None:
        return "na"
    return f"{value * 100:+.2f}%"


def fmt_float(value: Any, digits: int = 4) -> str:
    value = finite_float(value)
    if value is None:
        return "na"
    return f"{value:.{digits}f}"


def metrics_table(rows: list[dict[str, Any]]) -> str:
    lines = ["| split | n | rank_corr | top_q_mean_y | q_spread | mae | mean_y |", "|---|---:|---:|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(
            "| {split} | {n} | {rank} | {top} | {spread} | {mae} | {mean_y} |".format(
                split=row.get("split"),
                n=row.get("n", 0),
                rank=fmt_float(row.get("rank_corr")),
                top=fmt_pct(row.get("top_quintile_mean_y")),
                spread=fmt_pct(row.get("long_short_quintile_spread")),
                mae=fmt_pct(row.get("mae")),
                mean_y=fmt_pct(row.get("mean_y")),
            )
        )
    return "\n".join(lines)


def write_results(
    path: Path,
    *,
    df: pd.DataFrame,
    source_meta: dict[str, Any],
    diagnostics: dict[str, Any],
    imputation: dict[str, Any],
    fundamentals_refresh: dict[str, Any],
    fundamentals_coverage: dict[str, Any],
    metrics: dict[str, Any],
) -> None:
    abs_med = float(df["y_hedged"].abs().median()) if not df.empty else float("nan")
    label_mean = float(df["y_hedged"].mean()) if not df.empty else float("nan")
    success = "PASS" if metrics["success"] else "FAIL"
    lines = [
        "# Alpha Model Results",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Dataset",
        "",
        f"- Source rows from `historical_ml_observations`: {source_meta['source_rows']}",
        f"- Deduped valid candidates expected: {source_meta['unique_candidates']}",
        f"- Rows written: {len(df)}",
        f"- Label drops: {diagnostics['dropped_label_rows']}",
        f"- Median `|y_hedged|`: {fmt_pct(abs_med)}",
        f"- Mean `y_hedged`: {fmt_pct(label_mean)}",
        f"- Split counts: {metrics['split_counts']}",
        "",
        "The split uses the 10-trading-day embargo windows explicitly: "
        "`train <= 2025-12-31`, `val = 2026-01-10..2026-03-31`, "
        "`test >= 2026-04-10`; rows inside the two gaps are marked `embargo`.",
        "",
        "## Fundamentals",
        "",
        "Caveat: yfinance `.info` is point-in-time-now, not as-of-event, so these "
        "features carry mild historical look-ahead.",
        "",
        f"- Refresh attempted: {fundamentals_refresh.get('attempted', 0)}",
        f"- Refresh stored: {fundamentals_refresh.get('stored', 0)}",
        f"- Refresh failed: {fundamentals_refresh.get('failed', 0)}",
        f"- Already current for final run: {fundamentals_refresh.get('already_current', 0)}",
        f"- Symbols with any fundamentals row: {fundamentals_coverage['symbols_with_rows']} / {fundamentals_coverage['symbols_total']}",
        f"- Missing symbols, first 30: {fundamentals_coverage['missing_symbols'][:30]}",
        "",
        "Missing numeric feature values were median-imputed after coverage was measured; "
        "`ALPHA_RESULTS.md` reports that imputation instead of hiding it.",
        "",
        "## Model",
        "",
        f"- Selected model: {metrics['model_info']}",
        f"- Success criterion on test: **{success}** "
        "(rank_corr > 0 and top-quintile mean `y_hedged` > 0).",
        f"- Train-minus-test rank-corr gap: {fmt_float(metrics['overfit_gap_rank_corr_train_minus_test'])}",
        "",
        "### Linear Baseline",
        "",
        metrics_table(metrics["linear_baseline"]),
        "",
        "### Alpha Model",
        "",
        metrics_table(metrics["alpha_model"]),
        "",
        "### Ablations",
        "",
        "| removed_group | test_rank_corr | delta_vs_full | test_top_q_mean_y |",
        "|---|---:|---:|---:|",
    ]
    for row in metrics["ablations"]:
        lines.append(
            f"| {row['removed_group']} | {fmt_float(row['test_rank_corr'])} | "
            f"{fmt_float(row['delta_vs_full'])} | {fmt_pct(row['test_top_quintile_mean_y'])} |"
        )
    lines.extend(
        [
            "",
            "## Contract Notes",
            "",
            "- `feat_connection_strength` reads `historical_asset_world_assets.connection_strength` when present; "
            "legacy worlds without that field default to `1.0` and should be treated as unscored.",
            "- `pre_drop_earnings_defense=True` means the candidate survives the old manual pre-drop "
            "of earnings and defense archetypes.",
            f"- Momentum feature uses `historical_momentum_parameter_results.momentum_value` at "
            f"`range_period={MOMENTUM_RANGE_PERIOD}`, `range_multiplier={MOMENTUM_RANGE_MULTIPLIER}`.",
            "",
            "## Imputation Counts",
            "",
            "```json",
            json.dumps(imputation["missing_before"], indent=2, sort_keys=True),
            "```",
        ]
    )
    if diagnostics["dropped_examples"]:
        lines.extend(["", "## Dropped Label Examples", "", "```json", json.dumps(diagnostics["dropped_examples"], indent=2), "```"])
    if fundamentals_refresh.get("failures"):
        lines.extend(["", "## Fundamentals Failures", "", "```json", json.dumps(fundamentals_refresh["failures"], indent=2), "```"])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def assert_parquet_engine() -> None:
    try:
        import pyarrow  # noqa: F401
    except Exception as pyarrow_error:
        try:
            import fastparquet  # noqa: F401
        except Exception:
            raise RuntimeError(
                "Writing data/candidates.parquet requires pyarrow or fastparquet. "
                "Install one, e.g. `.venv\\Scripts\\python.exe -m pip install pyarrow`."
            ) from pyarrow_error


async def build(args: argparse.Namespace) -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    conn = await connect()
    try:
        await ensure_alpha_schema(conn)
    finally:
        await conn.close()

    observations, source_meta = await load_observations(
        run_ids=args.run_ids,
        statuses=args.status,
        dedupe=not args.keep_duplicates,
    )
    if args.run_ids:
        print(f"[dataset] restricted to run_ids={list(args.run_ids)}")
    candidate_symbols = sorted({str(row["symbol"]) for row in observations})
    conn = await connect()
    try:
        if args.skip_fundamentals_refresh:
            fundamentals_refresh = {"attempted": 0, "stored": 0, "failed": 0, "skipped": True}
        else:
            print(
                "[fundamentals] yfinance .info is point-in-time-now; "
                "refreshing missing current rows with that look-ahead caveat."
            )
            fundamentals_refresh = await refresh_fundamentals(
                conn,
                candidate_symbols,
                as_of=TODAY_UTC,
                workers=args.fundamentals_workers,
                limit=args.fundamentals_limit,
            )
        fundamentals = await load_fundamentals(conn, candidate_symbols)
        metadata = await load_asset_metadata(conn, candidate_symbols)
        world_features = await load_world_features(conn, list(source_meta["run_ids"]))
        predictions = await load_old_predictions(conn, list(source_meta["run_ids"]))
        momentum = await load_momentum(conn, list(source_meta["run_ids"]))
    finally:
        await conn.close()

    hedge_symbols = {str(row.get("sector_etf") or SPY) for row in observations}
    symbols_for_prices = set(candidate_symbols) | hedge_symbols | {SPY}
    print(f"[dataset] loading daily prices for {len(symbols_for_prices)} symbols")
    daily_prices, probabilities = await asyncio.gather(
        load_price_series(symbols_for_prices, "1d"),
        load_probability_series({row["market_id"] for row in observations}),
    )
    print(
        f"[dataset] loaded daily price symbols={len(daily_prices)} "
        f"probability markets={len(probabilities)}"
    )
    df, diagnostics = build_rows(
        observations,
        prices=daily_prices,
        probabilities=probabilities,
        fundamentals=fundamentals,
        metadata=metadata,
        world_features=world_features,
        predictions=predictions,
        momentum=momentum,
        max_holding_days=args.max_holding_days,
    )
    if diagnostics["dropped_label_rows"]:
        print(f"[dataset] dropped label rows={diagnostics['dropped_label_rows']}")
    df, imputation = impute_required_features(df)
    scored, metrics = train_and_score(df)
    symbols_with_rows = sorted(fundamentals)
    fundamentals_coverage = {
        "symbols_total": len(candidate_symbols),
        "symbols_with_rows": len(symbols_with_rows),
        "missing_symbols": [symbol for symbol in candidate_symbols if symbol not in fundamentals],
    }

    feature_cols = [col for col in scored.columns if col.startswith("feat_")]
    ordered_cols = CONTRACT_COLUMNS[:13] + feature_cols + [
        "pre_drop_earnings_defense",
        "asset_return",
        "sector_return",
        "entry_close_ts",
        "exit_close_ts",
        "exit_price",
    ] + CONTRACT_COLUMNS[13:]
    ordered_cols = [col for col in ordered_cols if col in scored.columns]
    scored = scored[ordered_cols]

    args.output.parent.mkdir(parents=True, exist_ok=True)
    assert_parquet_engine()
    scored.to_parquet(args.output, index=False)
    write_results(
        args.results,
        df=scored,
        source_meta=source_meta,
        diagnostics=diagnostics,
        imputation=imputation,
        fundamentals_refresh=fundamentals_refresh,
        fundamentals_coverage=fundamentals_coverage,
        metrics=metrics,
    )
    print(f"[done] wrote {len(scored)} rows -> {args.output}")
    print(f"[done] wrote results -> {args.results}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build alpha candidates parquet and Model-1 report.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--status", action="append", default=list(DEFAULT_RUN_STATUSES))
    parser.add_argument(
        "--run-ids",
        action="append",
        default=None,
        help=(
            "Restrict observations to these run_id(s). Use after rebuilding worlds with the "
            "two-pass prompt so the dataset is not contaminated by older peer-dump runs. "
            "Repeat the flag for multiple runs."
        ),
    )
    parser.add_argument("--keep-duplicates", action="store_true")
    parser.add_argument("--skip-fundamentals-refresh", action="store_true")
    parser.add_argument("--fundamentals-workers", type=int, default=8)
    parser.add_argument("--fundamentals-limit", type=int)
    parser.add_argument("--max-holding-days", type=int, default=10)
    return parser.parse_args()


def main() -> None:
    asyncio.run(build(parse_args()))


if __name__ == "__main__":
    main()
