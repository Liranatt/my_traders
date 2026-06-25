"""A first 'feel' for a learned Model-2 policy (NOT a full RL): cross-entropy-method search over
the continuous exit knobs (entry band, theta_out, close-vol stop k, take-profit level) to MAXIMIZE
train Sharpe, then report val/test. The point is to see the overfit gap -- a flexible learned policy
on this thin, distribution-mismatched data is guilty-until-OOS.

Usage:  .venv/Scripts/python.exe -m general_testing.rl_feel <run_id>
"""
from __future__ import annotations

import asyncio
import sys

import numpy as np

from portfolio_layer.config import PolicyConfig, SimConfig
from portfolio_layer.contract import assign_alpha, select
from portfolio_layer.data import load_world
from portfolio_layer.simulator import simulate


def to_policy(p: np.ndarray) -> PolicyConfig:
    return PolicyConfig(
        entry_strong_probability=float(np.clip(p[0], 0.55, 0.75)),
        theta_out=float(np.clip(p[1], 0.45, 0.60)),
        stop_type="close_vol_trailing",
        stop_level=float(np.clip(p[2], 0.5, 4.0)),
        use_polymarket_exit=True,
        use_take_profit=True,
        take_profit_level=float(np.clip(p[3], 0.02, 0.15)),
        take_profit_mode="fixed",
        use_resolution_exit=True,
    )


def sharpe(world, cands, alpha, policy, sim) -> float:
    s = simulate(world, cands, alpha, policy, sim).summary["sharpe"]
    return float(s) if np.isfinite(s) else -10.0


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run_id = sys.argv[1] if len(sys.argv) > 1 else "5fb1a1cd-7513-4085-a0c4-499d66c205ab"
    sim = SimConfig(run_id=run_id, alpha_mode="long_only", universe="all")
    world = await load_world(run_id, "data/candidates.parquet")
    alpha = assign_alpha(world, sim.alpha_mode, sim.seed)
    train = select(world, "train", "all")
    val = select(world, "val", "all")
    test = select(world, "test", "all")

    rng = np.random.default_rng(0)
    mean = np.array([0.62, 0.52, 2.0, 0.06])
    std = np.array([0.05, 0.03, 0.8, 0.03])
    for it in range(7):
        samples = rng.normal(mean, std, size=(30, 4))
        scores = np.array([sharpe(world, train, alpha, to_policy(p), sim) for p in samples])
        elite = samples[np.argsort(scores)[-8:]]
        mean, std = elite.mean(0), elite.std(0) + 1e-3
        print(f"iter {it}: best_train_sharpe={scores.max():.2f}  mean={np.round(mean,3)}")

    best = to_policy(mean)
    print(f"\nLEARNED POLICY: {best.label()}")
    for name, cands in (("train", train), ("val", val), ("test", test)):
        s = simulate(world, cands, alpha, best, sim).summary
        print(f"  {name:5} sharpe={s['sharpe']:+.3f}  maxDD={s['maxdd']*100:.2f}%  net={s['net_pct']*100:+.2f}%  "
              f"trades={s['n_trades']}  hit={s['hit']*100:.0f}%")
    print("\nThe train->val/test gap is the 'feel': a learned policy maximizes the in-sample number;")
    print("trust only the OOS columns, and only if they survive the distribution mismatch.")


if __name__ == "__main__":
    asyncio.run(main())
