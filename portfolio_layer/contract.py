"""Contract glue: candidate keys, the mocked `alpha_score`/`alpha_dir`, and universe selection.

`alpha_mode` is how Agent B stays unblocked while Agent A's Model 1 is still cooking:
  oracle    -> alpha_dir = realized_dir   (perfect direction; upper bound for the exit toolbox)
  random    -> seeded coin flip           (honest null; what "no edge" looks like)
  long_only -> always long                (the current semantic book's structural direction)
  current_ml -> direction from the legacy ML feature block
  parquet   -> read Agent A's real alpha_dir from data/candidates.parquet (when it ships)
"""
from __future__ import annotations

import os

import numpy as np

from .config import CANDIDATES_PARQUET
from .data import Candidate, World


def cand_key(c: Candidate) -> tuple:
    return (c.event_id, c.market_id, c.symbol, c.pass_number)


def assign_alpha(world: World, mode: str, seed: int) -> dict[tuple, int]:
    """Map each candidate key -> trade direction (+1 long / -1 short)."""
    if mode == "parquet":
        return _alpha_from_candidates_or_parquet(world)
    rng = np.random.default_rng(seed)
    out: dict[tuple, int] = {}
    for c in world.candidates:
        if mode == "oracle":
            out[cand_key(c)] = 1 if c.realized_dir >= 0 else -1
        elif mode == "long_only":
            out[cand_key(c)] = 1
        elif mode == "current_ml":
            out[cand_key(c)] = _dir_from_text(c.ml_dir, default=1)
        elif mode == "random":
            out[cand_key(c)] = 1 if rng.random() >= 0.5 else -1
        else:
            raise ValueError(f"unknown alpha_mode {mode!r}")
    return out


def _dir_from_text(value: str, *, default: int = 1) -> int:
    text = (value or "").strip().lower()
    if text in {"short", "sell", "down", "negative", "-1"}:
        return -1
    if text in {"long", "buy", "up", "positive", "1"}:
        return 1
    return default


def _alpha_from_candidates_or_parquet(world: World) -> dict[tuple, int]:
    from_candidates = {
        cand_key(c): _dir_from_text(c.alpha_dir, default=1 if c.alpha_score >= 0 else -1)
        for c in world.candidates
        if c.alpha_dir or np.isfinite(c.alpha_score)
    }
    if from_candidates:
        return from_candidates
    if not os.path.exists(CANDIDATES_PARQUET):
        raise FileNotFoundError(
            f"alpha_mode='parquet' but {CANDIDATES_PARQUET} is missing — Agent A has not "
            f"delivered the contract yet. Use --alpha oracle|random|long_only meanwhile."
        )
    import pandas as pd

    df = pd.read_parquet(CANDIDATES_PARQUET)
    out: dict[tuple, int] = {}
    for _, row in df.iterrows():
        key = (row["event_id"], row["market_id"], row["symbol"], int(row.get("pass_number", 0)))
        if "alpha_dir" in row and isinstance(row["alpha_dir"], str):
            out[key] = 1 if row["alpha_dir"].lower() == "long" else -1
        else:
            out[key] = 1 if float(row.get("alpha_score", 0.0)) >= 0 else -1
    return out


def select(world: World, split: str, universe: str, min_relevance: float = 0.0) -> list[Candidate]:
    """Tradeable candidates for a split and universe (task B3: all vs pre_drop).

    `min_relevance` hard-filters on the final relevance grade (question_relevance x
    connection_strength) -- a real universe filter, applied consistently here.
    """
    out = []
    for c in world.candidates:
        if c.split != split or not c.tradeable:
            continue
        if universe == "pre_drop" and not c.in_pre_drop:
            continue
        if c.relevance < min_relevance:
            continue
        out.append(c)
    return out
