from __future__ import annotations

import numpy as np
import pytest

from portfolio_layer.config import PolicyConfig, SimConfig
from portfolio_layer.contract import cand_key
from portfolio_layer.data import Candidate, World
from portfolio_layer.simulator import eligible_entry_idx, no_toolbox_policy, simulate


def _ret(close: np.ndarray) -> np.ndarray:
    prev = np.roll(close, 1)
    prev[0] = np.nan
    return np.where(np.isfinite(prev) & (prev > 0), close / prev - 1.0, 0.0)


def _world(asset: list[float], hedge: list[float], prob: list[float]) -> World:
    a = np.asarray(asset, dtype=float)
    h = np.asarray(hedge, dtype=float)
    return World(
        candidates=[],
        calendar=np.array(["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06"], dtype="datetime64[D]"),
        close={"AAA": a, "SPY": h},
        ret={"AAA": _ret(a), "SPY": _ret(h)},
        prob={"m1": np.asarray(prob, dtype=float)},
    )


def _candidate(**kwargs) -> Candidate:
    values = dict(
        event_id="e1",
        market_id="m1",
        symbol="AAA",
        pass_number=1,
        sector_etf="SPY",
        t_theta=None,
        t_e=None,
        entry_idx=0,
        resolution_idx=3,
        prob_at_trigger=0.57,
        archetype="macro",
        y_hedged=0.0,
        realized_dir=1,
        realized_abs_move=0.0,
        split="test",
        in_pre_drop=True,
        alpha_score=0.01,
        alpha_dir="long",
        ml_dir="long",
    )
    values.update(kwargs)
    return Candidate(**values)


def test_confirmation_band_enters_after_two_days_above_floor() -> None:
    world = _world([100, 100, 100, 100], [100, 100, 100, 100], [0.57, 0.56, 0.58, 0.58])
    c = _candidate()
    policy = PolicyConfig(entry_strong_probability=0.65, entry_confirmation_days=2)
    assert eligible_entry_idx(world, c, policy) == 1


def test_confirmation_band_rejects_transient_crossing() -> None:
    world = _world([100, 100, 100, 100], [100, 100, 100, 100], [0.57, 0.54, 0.58, 0.58])
    c = _candidate()
    policy = PolicyConfig(entry_strong_probability=0.65, entry_confirmation_days=2)
    assert eligible_entry_idx(world, c, policy) is None


def test_sector_hedged_long_return_flows_into_portfolio_series() -> None:
    world = _world([100, 110, 110, 110], [100, 103, 103, 103], [0.70, 0.70, 0.70, 0.70])
    c = _candidate(prob_at_trigger=0.70)
    world.candidates.append(c)
    sim = SimConfig(book=100_000.0, position_fraction=1.0, max_positions=1)
    result = simulate(world, [c], {cand_key(c): 1}, no_toolbox_policy(), sim)

    assert result.summary["n_trades"] == 1
    assert result.trades[0].exit_reason == "resolution_exit"
    assert result.trades[0].return_pct == pytest.approx(0.07)
    assert result.summary["net_pct"] == pytest.approx(0.07)


def test_polymarket_reversal_exits_open_position() -> None:
    world = _world([100, 101, 102, 103], [100, 100, 100, 100], [0.70, 0.56, 0.49, 0.49])
    c = _candidate(prob_at_trigger=0.70)
    world.candidates.append(c)
    policy = PolicyConfig(stop_type="none", theta_out=0.50)
    sim = SimConfig(book=100_000.0, position_fraction=1.0, max_positions=1)
    result = simulate(world, [c], {cand_key(c): 1}, policy, sim)

    assert result.summary["n_trades"] == 1
    assert result.trades[0].exit_idx == 2
    assert result.trades[0].exit_reason == "polymarket_reversal"
