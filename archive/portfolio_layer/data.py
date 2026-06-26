"""Load the `candidates` contract + raw price/probability series and shape them for the sim.

Prefers Agent A's ``data/candidates.parquet`` when present; otherwise rebuilds the contract
scaffold straight from Postgres (replicating the label definition) so Agent B is never blocked.
Everything is aligned to one global daily trading calendar so the simulator is a simple walk.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from database.db_connection import connect
from database.backtesting.schema import SCHEMA
from .config import (
    CANDIDATES_PARQUET, PRE_DROP_ARCHETYPES, TEST_START, TRAIN_END, VAL_END, VAL_START,
)


@dataclass(frozen=True)
class Candidate:
    event_id: str
    market_id: str
    symbol: str
    pass_number: int
    sector_etf: str
    t_theta: datetime
    t_e: datetime
    entry_idx: int          # calendar index of first close >= t_theta
    resolution_idx: int     # calendar index of the resolution exit (1 td before t_e)
    prob_at_trigger: float
    archetype: str
    y_hedged: float         # contract label (sector-hedged return, mocked from DB prices)
    realized_dir: int       # sign(y_hedged)
    realized_abs_move: float
    split: str              # train | val | test  ('' => embargoed/out of range)
    in_pre_drop: bool       # True => survives the earnings/defense pre-drop (task B3)
    alpha_score: float = 0.0
    alpha_dir: str = ""
    ml_dir: str = ""
    relevance: float = 1.0  # final relevance = question_relevance x connection_strength

    @property
    def tradeable(self) -> bool:
        return self.entry_idx >= 0 and self.resolution_idx > self.entry_idx


@dataclass
class World:
    candidates: list[Candidate]
    calendar: np.ndarray                 # datetime64[D], sorted unique trading days
    close: dict[str, np.ndarray]         # symbol -> close ffilled onto calendar (NaN before first bar)
    ret: dict[str, np.ndarray]           # symbol -> daily simple return on calendar (0 where flat)
    prob: dict[str, np.ndarray]          # market_id -> daily probability on calendar (ffill; NaN pre-start)


def _utc_naive(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _ffill(a: np.ndarray) -> np.ndarray:
    """Forward-fill NaNs; leading NaNs (before first observation) stay NaN."""
    valid = np.where(~np.isnan(a))[0]
    if valid.size == 0:
        return a
    pos = np.searchsorted(valid, np.arange(a.size), side="right") - 1
    out = np.where(pos >= 0, a[valid[np.clip(pos, 0, None)]], np.nan)
    return out


def _split_of(t_theta: datetime) -> str:
    d = _utc_naive(t_theta).date().isoformat()
    if d <= TRAIN_END:
        return "train"
    if VAL_START <= d <= VAL_END:
        return "val"
    if d >= TEST_START:
        return "test"
    return ""  # embargo gap -> dropped


def _j(v):
    return v if isinstance(v, dict) else json.loads(v)


def _float_or_nan(value) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return float("nan")
    return out if np.isfinite(out) else float("nan")


def _to_datetime(value) -> datetime:
    import pandas as pd

    if pd.isna(value):
        raise ValueError("missing datetime")
    dt = pd.to_datetime(value).to_pydatetime()
    return _utc_naive(dt)


async def _load_raw_market_data(
    symbols: set[str],
    market_ids: set[str],
) -> tuple[list, list]:
    conn = await connect()
    try:
        bars = await conn.fetch(
            f"""SELECT symbol, ts, close FROM {SCHEMA}.historical_price_bars
                WHERE resolution='1d' AND symbol = ANY($1::text[]) ORDER BY symbol, ts""",
            sorted(symbols),
        )
        prob_pts = await conn.fetch(
            f"""SELECT DISTINCT ON (market_id, (hour_ts AT TIME ZONE 'UTC')::date)
                       market_id, (hour_ts AT TIME ZONE 'UTC')::date AS d, probability
                FROM {SCHEMA}.historical_probability_points
                WHERE market_id = ANY($1::text[])
                ORDER BY market_id, (hour_ts AT TIME ZONE 'UTC')::date, hour_ts DESC""",
            sorted(market_ids),
        )
    finally:
        await conn.close()
    return list(bars), list(prob_pts)


def _shape_series(bars: list, prob_pts: list) -> tuple[
    np.ndarray,
    dict[np.datetime64, int],
    dict[str, np.ndarray],
    dict[str, np.ndarray],
    dict[str, np.ndarray],
    dict[str, np.ndarray],
]:
    by_sym: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for b in bars:
        by_sym[b["symbol"]].append((_utc_naive(b["ts"]), float(b["close"])))
    all_dates: set[np.datetime64] = set()
    sym_dates: dict[str, np.ndarray] = {}
    sym_close: dict[str, np.ndarray] = {}
    for sym, rows in by_sym.items():
        rows.sort()
        ded: dict[np.datetime64, float] = {}
        for ts, c in rows:
            ded[np.datetime64(ts, "D")] = c
        ds = np.array(sorted(ded), dtype="datetime64[D]")
        sym_dates[sym] = ds
        sym_close[sym] = np.array([ded[d] for d in ds], dtype=float)
        all_dates.update(ds.tolist())
    calendar = np.array(sorted(all_dates), dtype="datetime64[D]")
    cal_index = {d: i for i, d in enumerate(calendar)}

    close: dict[str, np.ndarray] = {}
    ret: dict[str, np.ndarray] = {}
    for sym in sym_dates:
        scattered = np.full(calendar.size, np.nan)
        idx = np.searchsorted(calendar, sym_dates[sym])
        scattered[idx] = sym_close[sym]
        c = _ffill(scattered)
        close[sym] = c
        prev = np.roll(c, 1)
        prev[0] = np.nan
        r = np.where(np.isfinite(c) & np.isfinite(prev) & (prev > 0), c / prev - 1.0, 0.0)
        ret[sym] = r

    prob_by_mkt: dict[str, list[tuple[np.datetime64, float]]] = defaultdict(list)
    for p in prob_pts:
        prob_by_mkt[p["market_id"]].append((np.datetime64(p["d"], "D"), float(p["probability"])))
    prob: dict[str, np.ndarray] = {}
    for mkt, rows in prob_by_mkt.items():
        rows.sort()
        scattered = np.full(calendar.size, np.nan)
        if calendar.size == 0:
            prob[mkt] = scattered
            continue
        ds = np.array([d for d, _ in rows], dtype="datetime64[D]")
        keep = (ds >= calendar[0]) & (ds <= calendar[-1])
        ds, vals = ds[keep], np.array([v for _, v in rows], dtype=float)[keep]
        if ds.size:
            idx = np.searchsorted(calendar, ds)
            idx = idx[idx < calendar.size]
            scattered[idx] = vals[: idx.size]
        prob[mkt] = _ffill(scattered)
    return calendar, cal_index, sym_dates, close, ret, prob


async def _load_world_from_parquet(path: Path) -> World:
    import pandas as pd

    df = pd.read_parquet(path)
    if df.empty:
        return World(
            candidates=[],
            calendar=np.array([], dtype="datetime64[D]"),
            close={},
            ret={},
            prob={},
        )

    symbols = {str(s) for s in df["symbol"].dropna().unique()}
    if "sector_etf" in df.columns:
        symbols |= {str(s) for s in df["sector_etf"].dropna().unique()}
    symbols.add("SPY")
    market_ids = {str(m) for m in df["market_id"].dropna().unique()}
    bars, prob_pts = await _load_raw_market_data(symbols, market_ids)
    calendar, cal_index, sym_dates, close, ret, prob = _shape_series(bars, prob_pts)

    def _first_idx_ge(sym: str, t: datetime) -> int:
        ds = sym_dates.get(sym)
        if ds is None:
            return -1
        d0 = np.datetime64(_utc_naive(t), "D")
        i = int(np.searchsorted(ds, d0, side="left"))
        if i >= ds.size:
            return -1
        return cal_index[ds[i]]

    def _idx_from_contract_close(sym: str, value, fallback_t: datetime) -> int:
        if value is not None and not pd.isna(value):
            d0 = np.datetime64(_to_datetime(value), "D")
            ds = sym_dates.get(sym)
            if ds is not None:
                i = int(np.searchsorted(ds, d0, side="left"))
                if i < ds.size:
                    return cal_index[ds[i]]
        return _first_idx_ge(sym, fallback_t)

    candidates: list[Candidate] = []
    for _, row in df.iterrows():
        sym = str(row["symbol"])
        t_theta = _to_datetime(row["t_theta"])
        t_e = _to_datetime(row["t_e"])
        etf = str(row.get("sector_etf") or "SPY")
        entry_idx = _idx_from_contract_close(sym, row.get("entry_close_ts"), t_theta)
        te_idx = _first_idx_ge(sym, t_e)
        if te_idx > 0:
            resolution_idx = te_idx - 1
        elif calendar.size:
            resolution_idx = calendar.size - 1
        else:
            resolution_idx = -1
        p0 = _float_or_nan(row.get("feat_prob_at_trigger"))
        if not np.isfinite(p0):
            p = prob.get(str(row["market_id"]))
            if p is not None and 0 <= entry_idx < p.size:
                p0 = float(p[entry_idx])
        candidates.append(Candidate(
            event_id=str(row["event_id"]),
            market_id=str(row["market_id"]),
            symbol=sym,
            pass_number=int(row.get("pass_number", 0)),
            sector_etf=etf if etf else "SPY",
            t_theta=t_theta,
            t_e=t_e,
            entry_idx=entry_idx,
            resolution_idx=resolution_idx,
            prob_at_trigger=p0,
            archetype=str(row.get("feat_archetype") or row.get("event_archetype") or "unknown"),
            y_hedged=_float_or_nan(row.get("y_hedged")),
            realized_dir=int(row.get("realized_dir", 0) or 0),
            realized_abs_move=_float_or_nan(row.get("realized_abs_move")),
            split=str(row.get("split") or ""),
            in_pre_drop=bool(row.get("pre_drop_earnings_defense", True)),
            alpha_score=_float_or_nan(row.get("alpha_score")),
            alpha_dir=str(row.get("alpha_dir") or ""),
            ml_dir=str(row.get("feat_ml_dir") or ""),
            relevance=(lambda v: v if np.isfinite(v) else 1.0)(_float_or_nan(row.get("feat_connection_strength"))),
        ))
    return World(candidates=candidates, calendar=calendar, close=close, ret=ret, prob=prob)


async def load_world(run_id: str, candidates_path: str | Path = CANDIDATES_PARQUET) -> World:
    path = Path(candidates_path)
    if path.exists():
        return await _load_world_from_parquet(path)
    return await _load_world_from_db(run_id)


async def _load_world_from_db(run_id: str) -> World:
    conn = await connect()
    try:
        obs = await conn.fetch(
            f"""SELECT o.event_id, o.market_id, o.symbol, o.first_pass_number AS pass_number,
                       o.first_pass_at AS t_theta, o.label_available_at AS t_e,
                       o.event_archetype, o.features, o.research_data
                FROM {SCHEMA}.historical_ml_observations o
                WHERE o.run_id=$1 AND o.valid_for_training""",
            run_id,
        )
        sector = {
            r["symbol"]: r["sector_etf"]
            for r in await conn.fetch(
                f"SELECT symbol, sector_etf FROM {SCHEMA}.historical_asset_metadata "
                f"WHERE sector_etf IS NOT NULL"
            )
        }
        symbols = {r["symbol"] for r in obs}
        symbols |= {sector.get(s) for s in list(symbols)}
        symbols |= {"SPY"}
        symbols = [s for s in symbols if s]
        bars = await conn.fetch(
            f"""SELECT symbol, ts, close FROM {SCHEMA}.historical_price_bars
                WHERE resolution='1d' AND symbol = ANY($1::text[]) ORDER BY symbol, ts""",
            symbols,
        )
        market_ids = list({r["market_id"] for r in obs})
        prob_pts = await conn.fetch(
            f"""SELECT DISTINCT ON (market_id, (hour_ts AT TIME ZONE 'UTC')::date)
                       market_id, (hour_ts AT TIME ZONE 'UTC')::date AS d, probability
                FROM {SCHEMA}.historical_probability_points
                WHERE market_id = ANY($1::text[])
                ORDER BY market_id, (hour_ts AT TIME ZONE 'UTC')::date, hour_ts DESC""",
            market_ids,
        )
    finally:
        await conn.close()

    # --- build the global daily calendar from all price bars ---
    by_sym: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    for b in bars:
        by_sym[b["symbol"]].append((_utc_naive(b["ts"]), float(b["close"])))
    all_dates: set[np.datetime64] = set()
    sym_dates: dict[str, np.ndarray] = {}
    sym_close: dict[str, np.ndarray] = {}
    for sym, rows in by_sym.items():
        rows.sort()
        # collapse to one close per date (keep last)
        ded: dict[np.datetime64, float] = {}
        for ts, c in rows:
            ded[np.datetime64(ts, "D")] = c
        ds = np.array(sorted(ded), dtype="datetime64[D]")
        sym_dates[sym] = ds
        sym_close[sym] = np.array([ded[d] for d in ds], dtype=float)
        all_dates.update(ds.tolist())
    calendar = np.array(sorted(all_dates), dtype="datetime64[D]")
    cal_index = {d: i for i, d in enumerate(calendar)}

    close: dict[str, np.ndarray] = {}
    ret: dict[str, np.ndarray] = {}
    for sym in sym_dates:
        scattered = np.full(calendar.size, np.nan)
        idx = np.searchsorted(calendar, sym_dates[sym])
        scattered[idx] = sym_close[sym]
        c = _ffill(scattered)
        close[sym] = c
        prev = np.roll(c, 1)
        prev[0] = np.nan
        r = np.where(np.isfinite(c) & np.isfinite(prev) & (prev > 0), c / prev - 1.0, 0.0)
        ret[sym] = r

    # --- daily probability per market, ffilled onto the calendar ---
    prob_by_mkt: dict[str, list[tuple[np.datetime64, float]]] = defaultdict(list)
    for p in prob_pts:
        prob_by_mkt[p["market_id"]].append((np.datetime64(p["d"], "D"), float(p["probability"])))
    prob: dict[str, np.ndarray] = {}
    for mkt, rows in prob_by_mkt.items():
        rows.sort()
        scattered = np.full(calendar.size, np.nan)
        ds = np.array([d for d, _ in rows], dtype="datetime64[D]")
        # clip prob dates to the calendar range, map to nearest existing calendar slot
        keep = (ds >= calendar[0]) & (ds <= calendar[-1])
        ds, vals = ds[keep], np.array([v for _, v in rows], dtype=float)[keep]
        if ds.size:
            scattered[np.searchsorted(calendar, ds)] = vals
        prob[mkt] = _ffill(scattered)

    def _first_idx_ge(sym: str, t: datetime) -> int:
        ds = sym_dates.get(sym)
        if ds is None:
            return -1
        d0 = np.datetime64(_utc_naive(t), "D")
        i = int(np.searchsorted(ds, d0, side="left"))  # first bar-date >= t_theta's date
        if i >= ds.size:
            return -1
        return cal_index[ds[i]]

    candidates: list[Candidate] = []
    for o in obs:
        sym = o["symbol"]
        etf = sector.get(sym) or "SPY"
        feats = _j(o["features"])
        rd = _j(o["research_data"])
        entry_idx = _first_idx_ge(sym, o["t_theta"])
        te_idx = _first_idx_ge(sym, o["t_e"])
        resolution_idx = (te_idx - 1) if te_idx > 0 else (calendar.size - 1)
        # contract label y_hedged on calendar closes: exit = min(t_e-1td, entry+10td)
        y_hedged = float("nan")
        if entry_idx >= 0:
            label_exit = min(resolution_idx, entry_idx + 10)
            label_exit = max(label_exit, entry_idx)
            ca, ce = close[sym], close.get(etf, close["SPY"])
            if (np.isfinite(ca[entry_idx]) and np.isfinite(ca[label_exit])
                    and np.isfinite(ce[entry_idx]) and np.isfinite(ce[label_exit])
                    and ca[entry_idx] > 0 and ce[entry_idx] > 0):
                y_hedged = (ca[label_exit] / ca[entry_idx] - 1.0) - (ce[label_exit] / ce[entry_idx] - 1.0)
        mx = rd.get("maximum_change")
        mn = rd.get("minimum_change")
        abs_move = max(abs(float(mx)) if mx is not None else 0.0,
                       abs(float(mn)) if mn is not None else 0.0)
        candidates.append(Candidate(
            event_id=o["event_id"], market_id=o["market_id"], symbol=sym,
            pass_number=o["pass_number"], sector_etf=etf,
            t_theta=_utc_naive(o["t_theta"]), t_e=_utc_naive(o["t_e"]),
            entry_idx=entry_idx, resolution_idx=resolution_idx,
            prob_at_trigger=float(feats.get("polymarket_probability_at_trigger", float("nan"))),
            archetype=o["event_archetype"],
            y_hedged=y_hedged,
            realized_dir=int(np.sign(y_hedged)) if np.isfinite(y_hedged) else 0,
            realized_abs_move=abs_move,
            split=_split_of(o["t_theta"]),
            in_pre_drop=o["event_archetype"] not in PRE_DROP_ARCHETYPES,
        ))
    return World(candidates=candidates, calendar=calendar, close=close, ret=ret, prob=prob)
