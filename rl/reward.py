"""Reward helpers used by RL experiments and tests."""
from __future__ import annotations


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
