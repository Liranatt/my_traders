"""Portfolio metrics computed from a daily return series. OOS Sharpe is the headline."""
from __future__ import annotations

import numpy as np

from .config import TRADING_DAYS_PER_YEAR


def equity_curve(daily_ret: np.ndarray) -> np.ndarray:
    return np.cumprod(1.0 + np.asarray(daily_ret, dtype=float))


def annualized_return(daily_ret: np.ndarray) -> float:
    r = np.asarray(daily_ret, dtype=float)
    if r.size == 0:
        return 0.0
    total = float(np.prod(1.0 + r))
    if total <= 0:
        return -1.0
    return total ** (TRADING_DAYS_PER_YEAR / r.size) - 1.0


def annualized_vol(daily_ret: np.ndarray) -> float:
    r = np.asarray(daily_ret, dtype=float)
    if r.size < 2:
        return 0.0
    return float(np.std(r, ddof=1)) * np.sqrt(TRADING_DAYS_PER_YEAR)


def sharpe(daily_ret: np.ndarray) -> float:
    """Annualized Sharpe of the daily portfolio return series (rf = 0)."""
    r = np.asarray(daily_ret, dtype=float)
    if r.size < 2:
        return 0.0
    sd = float(np.std(r, ddof=1))
    if sd == 0:
        return 0.0
    return float(np.mean(r)) / sd * np.sqrt(TRADING_DAYS_PER_YEAR)


def max_drawdown(daily_ret: np.ndarray) -> float:
    """Largest peak-to-trough drop of the equity curve, as a positive fraction."""
    r = np.asarray(daily_ret, dtype=float)
    if r.size == 0:
        return 0.0
    eq = equity_curve(r)
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    return float(-dd.min())


def calmar(daily_ret: np.ndarray) -> float:
    mdd = max_drawdown(daily_ret)
    if mdd <= 1e-9:
        return 0.0
    return annualized_return(daily_ret) / mdd


def summarize(daily_ret: np.ndarray, trade_pnls: list[float], n_entries: int,
              notional: float, book: float, n_active_days: int) -> dict:
    """One row of headline metrics for a daily return series + its trade log."""
    r = np.asarray(daily_ret, dtype=float)
    pnls = np.asarray(trade_pnls, dtype=float)
    years = max(r.size / TRADING_DAYS_PER_YEAR, 1e-9)
    return {
        "n_trades": int(pnls.size),
        "n_days": int(r.size),
        "active_days": int(n_active_days),
        "sharpe": sharpe(r),
        "maxdd": max_drawdown(r),
        "calmar": calmar(r),
        "ann_return": annualized_return(r),
        "net_pct": float(np.prod(1.0 + r) - 1.0) if r.size else 0.0,
        "hit": float((pnls > 0).mean()) if pnls.size else 0.0,
        # one-way turnover: entry notional traded per year, in multiples of the book.
        "turnover": float(n_entries * notional / book / years),
    }


def objective_value(summary: dict, objective: str, maxdd_cap: float) -> float:
    """Scalar a sweep maximizes. return_maxdd = net return, hard-penalized if maxDD breaches the cap."""
    if objective == "sharpe":
        return summary["sharpe"]
    if objective == "calmar":
        return summary["calmar"]
    if objective == "return_maxdd":
        if summary["maxdd"] > maxdd_cap:
            return summary["net_pct"] - 100.0 * (summary["maxdd"] - maxdd_cap)
        return summary["net_pct"]
    raise ValueError(f"unknown objective {objective!r}")
