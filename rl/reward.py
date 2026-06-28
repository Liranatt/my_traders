"""Reward helpers used by RL experiments and tests."""
from __future__ import annotations


def _clip(value: float, low: float, high: float) -> float:
    return float(max(low, min(high, value)))


def calculate_step_reward(
    *,
    action: int,
    step_asset_ret: float,
    step_bench_ret: float,
    txn_cost_pct: float,
    prev_cum_asset_ret: float,
    prev_cum_bench_ret: float,
    curr_cum_asset_ret: float,
    curr_cum_bench_ret: float,
) -> dict[str, float]:
    """Calculate the reward for a single step in the MDP."""
    step_excess = step_asset_ret - step_bench_ret
    
    # 1. Base Step Reward
    if action == 1:
        reward = step_excess - txn_cost_pct
        # "if he went into a trade and lost to the index thats not a penalty if he lost less then 3%"
        # Applied to cumulative loss since entry as per user choice.
        cum_excess = curr_cum_asset_ret - curr_cum_bench_ret
        if reward < 0 and cum_excess >= -0.03:
            reward = 0.0
            
        # "if he is in a loosing situation but dont sell and it end up beating the index -> big reward."
        prev_excess = prev_cum_asset_ret - prev_cum_bench_ret
        if prev_excess < 0 and cum_excess > 0:
            reward += 0.05
    else:
        # Action == 0 (INDEX)
        # If asset beat index, penalty for leaving money on the table
        # If index beat asset, positive reward for avoiding the loser
        # Transaction costs are a strict penalty because they eat capital.
        reward = -step_excess - txn_cost_pct

    total_reward = float(_clip(reward * 100.0, -20.0, 20.0))
    return {
        "reward": total_reward,
        "step_excess": float(step_excess),
        "txn_cost_pct": float(txn_cost_pct),
    }


class DifferentialSharpeRatio:
    """Online differential Sharpe reward using pre-update moments."""

    def __init__(self, eta: float = 0.01):
        self.eta = float(eta)
        self.A = 0.0
        self.B = 0.0

    def reset(self) -> None:
        self.A = 0.0
        self.B = 0.0

    def update(self, value: float) -> float:
        r = float(value)
        a_prev = self.A
        b_prev = self.B
        d_a = r - a_prev
        d_b = r * r - b_prev
        var = b_prev - a_prev * a_prev
        if var <= 1e-12:
            dsr = 0.0
        else:
            dsr = (b_prev * d_a - 0.5 * a_prev * d_b) / (var ** 1.5)
        self.A = a_prev + self.eta * d_a
        self.B = b_prev + self.eta * d_b
        return float(dsr)
