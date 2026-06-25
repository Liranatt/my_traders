"""Day-by-day Portfolio Layer v1 simulator.

The policy acts at daily closes. Positions entered on day D earn their first
return from D close to D+1 close. Positions exited on day D include the return
through that close, then leave the book.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .config import PolicyConfig, SimConfig
from .contract import cand_key
from .data import Candidate, World
from .metrics import objective_value, summarize


@dataclass(frozen=True)
class TradeRecord:
    event_id: str
    market_id: str
    symbol: str
    sector_etf: str
    pass_number: int
    direction: int
    entry_idx: int
    exit_idx: int
    entry_date: str
    exit_date: str
    exit_reason: str
    pnl: float
    return_pct: float


@dataclass
class Position:
    candidate: Candidate
    direction: int
    entry_idx: int
    entry_asset: float
    entry_hedge: float
    best_return: float = 0.0
    best_idx: int = -1               # last day the cumulative return made a new high


@dataclass(frozen=True)
class SimulationResult:
    daily_returns: np.ndarray
    trades: list[TradeRecord]
    summary: dict
    policy: PolicyConfig


def policy_grid() -> list[PolicyConfig]:
    """B1 sweep: entry band x theta_out x stop type x level."""
    policies: list[PolicyConfig] = []
    for entry_strong in (0.60, 0.65, 0.70):
        for theta_out in (0.50, 0.55):
            policies.append(
                PolicyConfig(
                    entry_strong_probability=entry_strong,
                    theta_out=theta_out,
                    stop_type="none",
                    stop_level=0.0,
                )
            )
            for stop_type, levels in (
                ("fixed", (0.03, 0.05, 0.08)),
                ("fixed_trailing", (0.03, 0.05, 0.08)),
                ("close_vol_trailing", (1.5, 2.0, 3.0)),
            ):
                for level in levels:
                    policies.append(
                        PolicyConfig(
                            entry_strong_probability=entry_strong,
                            theta_out=theta_out,
                            stop_type=stop_type,
                            stop_level=level,
                        )
                    )
    return policies


def exit_ablation_policies(entry_strong: float = 0.60) -> list[tuple[str, PolicyConfig]]:
    """Exit rules A-E, each tested SEPARATELY against a hold-to-resolution base.

    Base = confirmed entry + resolution exit only (no Polymarket exit, no stop). Each variant
    turns on exactly one new rule so its marginal effect on the naive hold is isolated.
    """
    from dataclasses import replace

    base = PolicyConfig(
        entry_strong_probability=entry_strong,
        stop_type="none",
        stop_level=0.0,
        use_polymarket_exit=False,
        use_resolution_exit=True,
    )
    out: list[tuple[str, PolicyConfig]] = [("base_hold_to_resolution", base)]
    for lvl in (0.04, 0.06, 0.08):  # A. take-profit (fixed)
        out.append((f"A_take_profit_fixed@{lvl:g}", replace(base, use_take_profit=True, take_profit_level=lvl, take_profit_mode="fixed")))
    out.append(("A_take_profit_rf_magnitude", replace(base, use_take_profit=True, take_profit_mode="rf")))
    for arm, gb in ((0.03, 0.015), (0.04, 0.02), (0.06, 0.03)):  # B. peak ratchet
        out.append((f"B_ratchet_{arm:g}/{gb:g}", replace(base, use_peak_ratchet=True, ratchet_arm=arm, ratchet_giveback=gb)))
    for d in (1, 2, 3):  # C. pre-event exit
        out.append((f"C_pre_event_{d}d", replace(base, use_pre_event_exit=True, pre_event_days=d)))
    for d in (2, 3, 5):  # D. momentum stall
        out.append((f"D_stall_{d}d", replace(base, use_stall_exit=True, stall_days=d)))
    for p in (0.80, 0.85, 0.90):  # E. probability ceiling
        out.append((f"E_prob_ceiling_{p:g}", replace(base, use_prob_ceiling_exit=True, prob_ceiling=p)))
    return out


def no_toolbox_policy(entry_strong_probability: float = 0.60) -> PolicyConfig:
    """Baseline: confirmed entry, no Polymarket exit, no stop, resolution exit only."""
    return PolicyConfig(
        entry_strong_probability=entry_strong_probability,
        stop_type="none",
        stop_level=0.0,
        use_polymarket_exit=False,
        use_resolution_exit=True,
    )


def eligible_entry_idx(world: World, c: Candidate, policy: PolicyConfig) -> int | None:
    if not c.tradeable:
        return None
    p0 = c.prob_at_trigger
    if not np.isfinite(p0):
        path = world.prob.get(c.market_id)
        if path is not None and 0 <= c.entry_idx < path.size:
            p0 = float(path[c.entry_idx])
    if not np.isfinite(p0) or p0 < policy.entry_confirm_floor:
        return None
    if p0 >= policy.entry_strong_probability:
        return c.entry_idx

    days = max(1, int(policy.entry_confirmation_days))
    end_idx = c.entry_idx + days - 1
    if end_idx > c.resolution_idx or end_idx >= world.calendar.size:
        return None
    path = world.prob.get(c.market_id)
    if path is None or end_idx >= path.size:
        return None
    window = np.array(path[c.entry_idx : end_idx + 1], dtype=float)
    if window.size:
        window[0] = p0 if not np.isfinite(window[0]) else window[0]
    if np.all(np.isfinite(window)) and np.all(window >= policy.entry_confirm_floor):
        return end_idx
    return None


def _close(world: World, symbol: str, idx: int) -> float:
    arr = world.close.get(symbol)
    if arr is None or idx < 0 or idx >= arr.size:
        return float("nan")
    return float(arr[idx])


def _ret(world: World, symbol: str, idx: int) -> float:
    arr = world.ret.get(symbol)
    if arr is None or idx < 0 or idx >= arr.size:
        return 0.0
    value = float(arr[idx])
    return value if np.isfinite(value) else 0.0


def _hedge_symbol(world: World, c: Candidate) -> str:
    if c.sector_etf in world.close:
        return c.sector_etf
    return "SPY"


def _daily_position_return(world: World, pos: Position, idx: int) -> float:
    hedge = _hedge_symbol(world, pos.candidate)
    return pos.direction * (
        _ret(world, pos.candidate.symbol, idx) - _ret(world, hedge, idx)
    )


def _cumulative_return(world: World, pos: Position, idx: int, basis: str) -> float:
    c = pos.candidate
    asset_now = _close(world, c.symbol, idx)
    if not np.isfinite(asset_now) or pos.entry_asset <= 0:
        return 0.0
    asset_ret = asset_now / pos.entry_asset - 1.0
    if basis == "asset":
        return pos.direction * asset_ret

    hedge_now = _close(world, _hedge_symbol(world, c), idx)
    if not np.isfinite(hedge_now) or pos.entry_hedge <= 0:
        return pos.direction * asset_ret
    hedge_ret = hedge_now / pos.entry_hedge - 1.0
    return pos.direction * (asset_ret - hedge_ret)


def _rolling_spread_vol(world: World, pos: Position, idx: int, lookback: int) -> float:
    c = pos.candidate
    start = max(pos.entry_idx + 1, idx - max(2, lookback) + 1)
    if start > idx:
        return 0.0
    hedge = _hedge_symbol(world, c)
    vals = [
        _ret(world, c.symbol, i) - _ret(world, hedge, i)
        for i in range(start, idx + 1)
    ]
    arr = np.asarray(vals, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return 0.0
    return float(np.std(arr, ddof=1))


def _stop_reason(world: World, pos: Position, idx: int, policy: PolicyConfig, current: float) -> str | None:
    if policy.stop_type == "none" or idx <= pos.entry_idx:
        return None
    if policy.stop_type == "fixed":
        return "fixed_stop" if current <= -policy.stop_level else None
    if policy.stop_type == "fixed_trailing":
        return "fixed_trailing_stop" if current <= pos.best_return - policy.stop_level else None
    if policy.stop_type == "close_vol_trailing":
        vol = _rolling_spread_vol(world, pos, idx, policy.vol_lookback)
        if vol <= 0:
            return None
        return (
            "close_vol_trailing_stop"
            if current <= pos.best_return - policy.stop_level * vol
            else None
        )
    raise ValueError(f"unknown stop_type {policy.stop_type!r}")


def _polymarket_exit_reason(world: World, pos: Position, idx: int, policy: PolicyConfig) -> str | None:
    if not policy.use_polymarket_exit or idx <= pos.entry_idx:
        return None
    path = world.prob.get(pos.candidate.market_id)
    if path is None or idx >= path.size:
        return None
    current = float(path[idx])
    prev = float(path[idx - 1]) if idx > 0 else float("nan")
    if np.isfinite(current) and current < policy.theta_out:
        if not np.isfinite(prev) or prev >= policy.theta_out:
            return "polymarket_reversal"
    return None


def _take_profit_target(policy: PolicyConfig, pos: Position) -> float:
    if policy.take_profit_mode == "rf":
        target = abs(pos.candidate.alpha_score)
        return target if target > 0 else policy.take_profit_level
    return policy.take_profit_level


def _exit_reason(world: World, pos: Position, idx: int, policy: PolicyConfig) -> str | None:
    # Track the favorable peak once per day (shared by the stop, ratchet and stall rules).
    current = _cumulative_return(world, pos, idx, policy.stop_basis)
    if current > pos.best_return:
        pos.best_return = current
        pos.best_idx = idx
    if idx <= pos.entry_idx:
        return None

    # Polymarket reversal: the mispricing reverses back below the band.
    reason = _polymarket_exit_reason(world, pos, idx, policy)
    if reason:
        return reason
    # E. probability-ceiling: the YES has effectively resolved; information has diffused.
    if policy.use_prob_ceiling_exit:
        path = world.prob.get(pos.candidate.market_id)
        if path is not None and idx < path.size:
            p = float(path[idx])
            if np.isfinite(p) and p >= policy.prob_ceiling:
                return "prob_ceiling_exit"
    # A. take-profit: bank the favorable move at a fixed level or the RF-predicted magnitude.
    if policy.use_take_profit and current >= _take_profit_target(policy, pos):
        return "take_profit"
    # Stop-loss toolbox (fixed / fixed-trailing / close-vol-trailing).
    reason = _stop_reason(world, pos, idx, policy, current)
    if reason:
        return reason
    # B. peak give-back ratchet: lock in once meaningfully up.
    if (
        policy.use_peak_ratchet
        and pos.best_return >= policy.ratchet_arm
        and current <= pos.best_return - policy.ratchet_giveback
    ):
        return "peak_ratchet"
    # D. momentum-stall: the post-crossing drift is spent.
    if policy.use_stall_exit and pos.best_idx >= 0 and (idx - pos.best_idx) >= policy.stall_days:
        return "momentum_stall"
    # C. pre-event exit: out before the resolution gap.
    if policy.use_pre_event_exit and idx >= pos.candidate.resolution_idx - max(0, policy.pre_event_days):
        return "pre_event_exit"
    # Resolution exit (1 td before t_e).
    if policy.use_resolution_exit and idx >= pos.candidate.resolution_idx:
        return "resolution_exit"
    return None


def _open_position(world: World, c: Candidate, direction: int, idx: int) -> Position | None:
    asset = _close(world, c.symbol, idx)
    hedge = _close(world, _hedge_symbol(world, c), idx)
    if not np.isfinite(asset) or not np.isfinite(hedge) or asset <= 0 or hedge <= 0:
        return None
    return Position(
        candidate=c,
        direction=direction,
        entry_idx=idx,
        entry_asset=asset,
        entry_hedge=hedge,
        best_idx=idx,
    )


def _close_trade(
    world: World,
    pos: Position,
    idx: int,
    reason: str,
    notional: float,
) -> TradeRecord:
    ret = _cumulative_return(world, pos, idx, "spread")
    c = pos.candidate
    return TradeRecord(
        event_id=c.event_id,
        market_id=c.market_id,
        symbol=c.symbol,
        sector_etf=_hedge_symbol(world, c),
        pass_number=c.pass_number,
        direction=pos.direction,
        entry_idx=pos.entry_idx,
        exit_idx=idx,
        entry_date=str(world.calendar[pos.entry_idx]),
        exit_date=str(world.calendar[idx]),
        exit_reason=reason,
        pnl=ret * notional,
        return_pct=ret,
    )


def simulate(
    world: World,
    candidates: Iterable[Candidate],
    alpha_direction: dict[tuple, int],
    policy: PolicyConfig,
    sim: SimConfig,
) -> SimulationResult:
    selected = sorted(candidates, key=lambda c: (c.entry_idx, c.market_id, c.symbol))
    entry_by_day: dict[int, list[Candidate]] = {}
    for c in selected:
        direction = alpha_direction.get(cand_key(c))
        if direction not in (-1, 1):
            continue
        entry_idx = eligible_entry_idx(world, c, policy)
        if entry_idx is None:
            continue
        entry_by_day.setdefault(entry_idx, []).append(c)

    if not entry_by_day:
        empty = np.array([], dtype=float)
        return SimulationResult(
            daily_returns=empty,
            trades=[],
            summary=summarize(empty, [], 0, sim.book * sim.position_fraction, sim.book, 0),
            policy=policy,
        )

    start = min(entry_by_day)
    end = max(max(c.resolution_idx, day) for day, rows in entry_by_day.items() for c in rows)
    end = min(end, world.calendar.size - 1)
    notional = sim.book * sim.position_fraction
    active: dict[tuple, Position] = {}
    daily_returns: list[float] = []
    trades: list[TradeRecord] = []
    n_entries = 0
    active_days = 0

    for idx in range(start, end + 1):
        day_pnl = 0.0
        if active:
            active_days += 1
        for pos in active.values():
            if idx > pos.entry_idx:
                day_pnl += _daily_position_return(world, pos, idx) * notional
        daily_returns.append(day_pnl / sim.book)

        to_close: list[tuple[tuple, str]] = []
        for key, pos in active.items():
            reason = _exit_reason(world, pos, idx, policy)
            if reason:
                to_close.append((key, reason))
        for key, reason in to_close:
            pos = active.pop(key)
            trades.append(_close_trade(world, pos, idx, reason, notional))

        if idx not in entry_by_day:
            continue
        active_symbols = {pos.candidate.symbol for pos in active.values()}
        sector_counts: dict[str, int] = {}
        for pos in active.values():
            sector_counts[_hedge_symbol(world, pos.candidate)] = (
                sector_counts.get(_hedge_symbol(world, pos.candidate), 0) + 1
            )
        for c in entry_by_day[idx]:
            if len(active) >= sim.max_positions or c.symbol in active_symbols:
                continue
            hedge = _hedge_symbol(world, c)
            if sector_counts.get(hedge, 0) >= sim.sector_cap:
                continue
            direction = alpha_direction[cand_key(c)]
            pos = _open_position(world, c, direction, idx)
            if pos is None:
                continue
            active[cand_key(c)] = pos
            active_symbols.add(c.symbol)
            sector_counts[hedge] = sector_counts.get(hedge, 0) + 1
            n_entries += 1

    for key, pos in list(active.items()):
        trades.append(_close_trade(world, pos, end, "calendar_end", notional))
        active.pop(key, None)

    returns = np.asarray(daily_returns, dtype=float)
    trade_pnls = [trade.pnl for trade in trades]
    return SimulationResult(
        daily_returns=returns,
        trades=trades,
        summary=summarize(returns, trade_pnls, n_entries, notional, sim.book, active_days),
        policy=policy,
    )


def score_summary(summary: dict, objective: str, maxdd_cap: float) -> float:
    return objective_value(summary, objective, maxdd_cap)
