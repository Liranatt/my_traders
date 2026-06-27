"""
Single-candidate, long-only trading environment for PPO.

One episode is one candidate's life, stepped over the benchmark trading
calendar from the day its entry band first fires (strategy.entry_day) to t_e.
Actions: HOLD, ENTER, EXIT back into the benchmark. The episode ends on EXIT
or on the last day (forced EXIT if still long): one trade at most. Rewards are
point-in-time and use realized step outcomes only; there is no future leakage.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from rl.config import (
    ACTION_DIM,
    ACTION_EXIT,
    ACTION_HOLD,
    ENTER_ACTIONS,
    POSITION_SIZE_CHOICES,
    action_for_entry_params,
    entry_params_for_action,
    is_enter_action,
)
from rl.exits import evaluate_hard_exit, update_peak_ret
from rl.features import (
    build_observation,
    compute_market_state,
    compute_position_state,
    entry_signal_day,
    static_features_from_row,
)
from rl.shared import (
    DEFAULT_POLICY,
    as_utc_day,
    ib_cost,
    _calendar_dates,
    _close_on,
)


class TradingEnv:
    HOLD = ACTION_HOLD
    ENTER = action_for_entry_params(POSITION_SIZE_CHOICES[0])
    ENTER_ACTIONS = ENTER_ACTIONS
    EXIT = ACTION_EXIT

    def __init__(
        self,
        candidate_row,
        prices: dict,
        probs: dict,
        bench_sym: str = "SPY",
        *,
        scaler: dict | None = None,
        entry_policy: dict | None = None,
    ):
        self.row = candidate_row
        self.prices = prices
        self.probs = probs
        self.bench_sym = bench_sym
        self.scaler = scaler
        self.entry_policy = entry_policy or DEFAULT_POLICY

        self.symbol = candidate_row["symbol"]
        self.market_id = candidate_row["market_id"]
        self.t_theta = as_utc_day(candidate_row["t_theta"])
        self.t_e = as_utc_day(candidate_row["t_e"])

        self.static = static_features_from_row(candidate_row)
        self.entry_sig = entry_signal_day(
            self.probs, self.market_id, candidate_row["t_theta"], self.entry_policy
        )

        start = self.entry_sig if self.entry_sig is not None else self.t_theta
        self.dates = _calendar_dates(self.prices, self.bench_sym, start, self.t_e)
        if not self.dates:
            self.dates = [start]
        resolution_cut = self.t_e - pd.Timedelta(days=1)
        cap_dates = [d for d in self.dates if d <= resolution_cut]
        self.resolution_exit_day = cap_dates[-1] if cap_dates else None
        self.n_steps = len(self.dates)

        self.force_entry = False
        self.reset()

    # -- gym-style API --------------------------------------------------------
    def reset(self):
        self.current_step = 0
        self.state = "FLAT"
        self.traded = False
        self.entry_price = 0.0
        self.bench_entry_price = 0.0
        self.position_size_pct = 0.0
        self.entry_step = -1
        self.peak_ret = 0.0
        self.prev_asset = None
        self.prev_bench = None
        self.equity_curve = [1.0]
        self.steps_in_drawdown = 0
        self.baseline_pnl = self._compute_baseline_pnl()
        return self._get_obs()

    def can_enter(self, day) -> bool:
        before_resolution_cap = self.resolution_exit_day is not None and day < self.resolution_exit_day
        return (
            self.entry_sig is not None
            and day >= self.entry_sig
            and not self.traded
            and before_resolution_cap
            and self.current_step <= self.n_steps - 2
        )

    def get_action_mask(self) -> np.ndarray:
        mask = np.zeros(ACTION_DIM, dtype=bool)
        day = self.dates[min(self.current_step, self.n_steps - 1)]
        if self.state == "FLAT":
            can_enter = self.can_enter(day)

            # Curriculum: If force_entry is ON and we have a valid entry, disable HOLD
            if self.force_entry and can_enter:
                pass
            else:
                mask[self.HOLD] = True

            if can_enter:
                mask[list(self.ENTER_ACTIONS)] = True
        else:  # LONG
            if self._hard_exit_signal(day) is not None or self.current_step >= self.n_steps - 1:
                mask[self.EXIT] = True
            else:
                mask[self.HOLD] = True
                mask[self.EXIT] = True
        return mask

    def step(self, action: int):
        last = self.current_step >= self.n_steps - 1
        day = self.dates[self.current_step]
        asset_price = _close_on(self.prices, self.symbol, day)
        bench_price = _close_on(self.prices, self.bench_sym, day)

        done = False
        R_t = 0.0

        if self.state == "FLAT":
            if is_enter_action(action) and self.can_enter(day) and asset_price and bench_price:
                params = entry_params_for_action(action)
                self.state = "LONG"
                self.traded = True
                self.position_size_pct = params.position_size_pct
                self.entry_price = float(asset_price)
                self.bench_entry_price = float(bench_price)
                self.entry_step = self.current_step
                self.peak_ret = 0.0
                self.prev_asset = float(asset_price)
                self.prev_bench = float(bench_price)
                self.steps_in_drawdown = 0

                entry_cost = (
                    ib_cost(1, asset_price, False) / asset_price
                    + ib_cost(1, bench_price, True) / bench_price
                )
                R_t = -self.position_size_pct * entry_cost
                if last:  # entered on the final day -> immediately forced out
                    exit_cost = (
                        ib_cost(1, asset_price, True) / asset_price
                        + ib_cost(1, bench_price, False) / bench_price
                    )
                    R_t -= self.position_size_pct * exit_cost
                    self.state = "FLAT"
                    self.position_size_pct = 0.0
                    done = True
            else:
                # Flat HOLD should be free.
                R_t = 0.0
                done = last
        else:  # LONG
            prev_asset = self.prev_asset or self.entry_price
            prev_bench = self.prev_bench or bench_price
            
            if asset_price and self.entry_price:
                from rl.exits import update_peak_ret
                self.peak_ret = update_peak_ret(self.prices, self.symbol, day, self.entry_price, self.peak_ret)
                
            hard_exit = self._hard_exit_signal(day)
            exit_price = hard_exit.exit_price if hard_exit is not None else asset_price
            daily_asset = (exit_price / prev_asset - 1.0) if (exit_price and prev_asset) else 0.0
            daily_bench = (bench_price / prev_bench - 1.0) if (bench_price and prev_bench) else 0.0
            excess = daily_asset - daily_bench

            if hard_exit is not None or action == self.EXIT or last:
                exit_cost = 0.0
                if exit_price and bench_price:
                    exit_cost = (
                        ib_cost(1, exit_price, True) / exit_price
                        + ib_cost(1, bench_price, False) / bench_price
                    )
                R_t = self.position_size_pct * (excess - exit_cost)
                self.state = "FLAT"
                self.position_size_pct = 0.0
                done = True
            else:  # HOLD
                R_t = self.position_size_pct * excess
                self.prev_asset = float(asset_price) if asset_price else prev_asset
                self.prev_bench = float(bench_price) if bench_price else prev_bench

        self.equity_curve.append(self.equity_curve[-1] * (1.0 + R_t))

        if done:
            if self.traded and self.entry_price > 0:
                exit_price_val = asset_price
                if 'hard_exit' in locals() and hard_exit is not None and hard_exit.exit_price:
                    exit_price_val = hard_exit.exit_price
                if not exit_price_val:
                    exit_price_val = self.prev_asset or self.entry_price
                bench_exit_val = bench_price if bench_price else self.prev_bench
                
                asset_ret = float(exit_price_val / self.entry_price - 1.0) if self.entry_price else 0.0
                bench_ret = float(bench_exit_val / self.bench_entry_price - 1.0) if self.bench_entry_price else 0.0
                excess = asset_ret - bench_ret
                
                entry_cost = ib_cost(1, self.entry_price, False) / self.entry_price + ib_cost(1, self.bench_entry_price, True) / self.bench_entry_price
                exit_cost = 0.0
                if exit_price_val and bench_exit_val:
                    exit_cost = ib_cost(1, exit_price_val, True) / exit_price_val + ib_cost(1, bench_exit_val, False) / bench_exit_val
                    
                agent_pnl = (excess - entry_cost - exit_cost) * self.position_size_pct
            else:
                agent_pnl = 0.0
                
            # 1. Base Reward: Absolute PnL (10% profit -> +10.0 reward)
            base_reward = float(agent_pnl * 100.0)
            

            relative_perf = float((agent_pnl - self.baseline_pnl) * 100.0)

            reward = float(base_reward + relative_perf)
            reward = float(np.clip(reward, -10.0, 10.0))
        else:
            reward = 0.0

        self.current_step += 1
        obs = self._get_obs()
        info = {"R_t": R_t}
        if self.state == "FLAT" and done:
            info["exit_reason"] = hard_exit.reason if "hard_exit" in locals() and hard_exit is not None else (
                "end_liquidation" if last and action != self.EXIT else "policy_exit"
            )
        return obs, float(reward), bool(done), info


    def _compute_baseline_pnl(self) -> float:
        if self.entry_sig is None or not self.dates:
            return 0.0
        
        entry_day = self.dates[0]
        entry_price = _close_on(self.prices, self.symbol, entry_day)
        bench_entry_price = _close_on(self.prices, self.bench_sym, entry_day)
        if not entry_price or not bench_entry_price:
            return 0.0
            
        exit_day = self.dates[-1]
        exit_price = _close_on(self.prices, self.symbol, exit_day)
        bench_exit_price = _close_on(self.prices, self.bench_sym, exit_day)
        
        sim_peak_ret = 0.0
        expected_return = max(0.001, abs(float(self.row.get("feat_llm_expected_return", 0.01))))
        
        for day in self.dates:
            from rl.exits import update_peak_ret
            sim_peak_ret = update_peak_ret(self.prices, self.symbol, day, entry_price, sim_peak_ret)
            hard_exit = evaluate_hard_exit(
                prices=self.prices,
                probs=self.probs,
                symbol=self.symbol,
                market_id=self.market_id,
                day=day,
                entry_day=entry_day,
                entry_price=entry_price,
                t_e=self.t_e,
                resolution_exit_day=self.resolution_exit_day,
                expected_return=expected_return,
                peak_ret=sim_peak_ret,
            )
            if hard_exit is not None:
                exit_day = day
                exit_price = hard_exit.exit_price or _close_on(self.prices, self.symbol, day)
                bench_exit_price = _close_on(self.prices, self.bench_sym, day)
                break
                
        if not exit_price or not bench_exit_price:
            return 0.0
            
        asset_ret = float(exit_price / entry_price - 1.0)
        bench_ret = float(bench_exit_price / bench_entry_price - 1.0)
        excess = asset_ret - bench_ret
        
        entry_cost = ib_cost(1, entry_price, False) / entry_price + ib_cost(1, bench_entry_price, True) / bench_entry_price
        exit_cost = ib_cost(1, exit_price, True) / exit_price + ib_cost(1, bench_exit_price, False) / bench_exit_price
        
        baseline_pnl = excess - entry_cost - exit_cost
        return float(baseline_pnl * POSITION_SIZE_CHOICES[0])

    def _hard_exit_signal(self, day):
        if self.state != "LONG":
            return None
            
        expected_return = max(0.001, abs(float(self.row.get("feat_llm_expected_return", 0.01))))
        return evaluate_hard_exit(
            prices=self.prices,
            probs=self.probs,
            symbol=self.symbol,
            market_id=self.market_id,
            day=day,
            entry_day=self.dates[self.entry_step],
            entry_price=self.entry_price,
            t_e=self.t_e,
            resolution_exit_day=self.resolution_exit_day,
            expected_return=expected_return,
            peak_ret=self.peak_ret,
        )

    # -- observation ----------------------------------------------------------
    def _get_obs(self) -> np.ndarray:
        idx = min(self.current_step, self.n_steps - 1)
        day = self.dates[idx]
        asset_price = _close_on(self.prices, self.symbol, day)
        window_fraction = self.current_step / max(1, self.n_steps - 1)

        market = compute_market_state(
            self.prices, self.probs, self.symbol, self.market_id, self.bench_sym, day
        )
        bench_price = _close_on(self.prices, self.bench_sym, day)
        position = compute_position_state(
            is_long=self.state == "LONG",
            entry_price=self.entry_price,
            asset_price=asset_price,
            bench_entry_price=self.bench_entry_price,
            bench_price=bench_price,
            peak_ret=self.peak_ret,
            window_fraction=window_fraction,
            position_size_pct=self.position_size_pct,
        )
        return build_observation(self.static, position, market, self.scaler)


def has_valid_episode(candidate_row, prices: dict, probs: dict, bench_sym: str) -> bool:
    """True if the candidate can ever be entered (band fires + >=2 calendar days)."""
    sig = entry_signal_day(probs, candidate_row["market_id"], candidate_row["t_theta"], DEFAULT_POLICY)
    if sig is None:
        return False
    dates = _calendar_dates(prices, bench_sym, sig, as_utc_day(candidate_row["t_e"]))
    return len(dates) >= 2
