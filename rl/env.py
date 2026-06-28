"""
Single-step, strategy-selection trading environment for PPO.

One episode is one candidate's life. The agent evaluates the candidate at entry,
selects a target strategy (SKIP, ENTER_75, ENTER_100, ENTER_200), and the
environment automatically simulates the trade to completion using the chosen
target and a fixed trailing stop, returning the final shaped reward.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from rl.config import (
    ACTION_DIM,
    ACTION_INDEX,
    ACTION_ASSET,
    HARD_STOP_LOSS_PCT,
    POSITION_SIZE_PCT,
)
from rl.features import (
    build_observation,
    compute_market_state,
    compute_position_state,
    entry_signal_day,
    llm_target_from_row,
    static_features_from_row,
)
from rl.reward import calculate_step_reward
from rl.shared import (
    DEFAULT_POLICY,
    as_utc_day,
    ib_cost,
    _calendar_dates,
    _close_on,
)


def has_valid_episode(candidate_row, prices: dict, probs: dict, bench_sym: str, entry_policy: dict | None = None) -> bool:
    market_id = candidate_row["market_id"]
    t_theta = as_utc_day(candidate_row["t_theta"])
    t_e = as_utc_day(candidate_row["t_e"])
    
    sig = entry_signal_day(probs, market_id, t_theta, entry_policy or DEFAULT_POLICY)
    if sig is None:
        return False
        
    dates = _calendar_dates(prices, bench_sym, sig, t_e)
    if len(dates) < 2:
        return False
        
    return True


class TradingEnv:
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
        self.all_dates = _calendar_dates(self.prices, self.bench_sym, start, self.t_e)
        if not self.all_dates:
            self.all_dates = [start]
        resolution_cut = self.t_e - pd.Timedelta(days=1)
        self.all_dates = [d for d in self.all_dates if d <= resolution_cut]

        self.decision_dates = []
        if self.all_dates:
            self.decision_dates.append(self.all_dates[0])
            for d in self.all_dates[1:-1]:
                if d.dayofweek == 4: # Friday
                    self.decision_dates.append(d)
            if len(self.all_dates) > 1 and self.all_dates[-1] not in self.decision_dates:
                self.decision_dates.append(self.all_dates[-1])

        self.reset()

    def reset(self):
        self.step_idx = 0
        self.done = False
        self.current_action = ACTION_INDEX # Start holding index
        self.is_holding = False
        if not self.decision_dates:
            self.entry_price = 0.0
            self.bench_entry = 0.0
        else:
            self.entry_price = float(_close_on(self.prices, self.symbol, self.decision_dates[0]) or 0)
            self.bench_entry = float(_close_on(self.prices, self.bench_sym, self.decision_dates[0]) or 0)
        
        self.trade_asset_ret = 0.0
        self.trade_bench_ret = 0.0
        self.trade_txn_costs = 0.0
        self.peak_ret = 0.0
        return self._get_obs()

    def get_action_mask(self) -> np.ndarray:
        mask = np.ones(ACTION_DIM, dtype=bool)
        if self.entry_sig is None or len(self.decision_dates) < 2:
            mask[ACTION_ASSET] = False
        return mask

    def step(self, action: int):
        if self.done:
            return self._get_obs(), 0.0, True, {}

        self.current_action = action

        if self.step_idx >= len(self.decision_dates) - 1:
            self.done = True
            return self._get_obs(), 0.0, True, {"reward": 0.0, "step_excess": 0.0}

        curr_day = self.decision_dates[self.step_idx]
        next_day = self.decision_dates[self.step_idx + 1]

        curr_asset = float(_close_on(self.prices, self.symbol, curr_day) or 0)
        curr_bench = float(_close_on(self.prices, self.bench_sym, curr_day) or 0)

        if len(self.decision_dates) < 2 or curr_asset <= 0 or curr_bench <= 0:
            self.done = True
            return self._get_obs(), 0.0, True, {"reward": 0.0, "step_excess": 0.0}

        txn_cost_pct = 0.0

        # Handle entry/exit at curr_day based on action
        if action == ACTION_ASSET and not self.is_holding:
            self.is_holding = True
            self.entry_price = curr_asset
            self.bench_entry = curr_bench
            self.peak_ret = 0.0
            asset_qty = 10000.0 / curr_asset
            bench_qty = 10000.0 / curr_bench
            txn_cost_pct = (ib_cost(asset_qty, curr_asset, False) + ib_cost(bench_qty, curr_bench, True)) / 10000.0
            self.trade_txn_costs = txn_cost_pct
        elif action == ACTION_INDEX and self.is_holding:
            self.is_holding = False
            self.entry_price = 0.0
            self.bench_entry = 0.0
            self.peak_ret = 0.0
            asset_qty = 10000.0 / curr_asset
            bench_qty = 10000.0 / curr_bench
            txn_cost_pct = (ib_cost(asset_qty, curr_asset, True) + ib_cost(bench_qty, curr_bench, False)) / 10000.0
            self.trade_txn_costs += txn_cost_pct

        self.current_action = action

        next_asset = float(_close_on(self.prices, self.symbol, next_day) or curr_asset)
        next_bench = float(_close_on(self.prices, self.bench_sym, next_day) or curr_bench)

        step_asset_ret = next_asset / curr_asset - 1.0 if curr_asset > 0 else 0.0
        step_bench_ret = next_bench / curr_bench - 1.0 if curr_bench > 0 else 0.0

        # Hard stop loss check mid-week
        hard_stop_hit = False
        if self.is_holding:
            start_idx = self.all_dates.index(curr_day) if curr_day in self.all_dates else 0
            end_idx = self.all_dates.index(next_day) if next_day in self.all_dates else len(self.all_dates) - 1
            
            for d in self.all_dates[start_idx + 1 : end_idx + 1]:
                ap = float(_close_on(self.prices, self.symbol, d) or curr_asset)
                if ap / self.entry_price - 1.0 <= -HARD_STOP_LOSS_PCT:
                    # Hit hard stop
                    exit_px = ap
                    bench_at_exit = float(_close_on(self.prices, self.bench_sym, d) or curr_bench)
                    
                    asset_part = exit_px / curr_asset
                    index_part = next_bench / bench_at_exit if bench_at_exit > 0 else 1.0
                    step_asset_ret = (asset_part * index_part) - 1.0
                    
                    asset_qty = 10000.0 / exit_px
                    bench_qty = 10000.0 / bench_at_exit
                    mid_week_txn_cost = (ib_cost(asset_qty, exit_px, True) + ib_cost(bench_qty, bench_at_exit, False)) / 10000.0
                    txn_cost_pct += mid_week_txn_cost
                    self.trade_txn_costs += mid_week_txn_cost
                    
                    self.is_holding = False
                    self.current_action = ACTION_INDEX
                    self.entry_price = 0.0
                    self.bench_entry = 0.0
                    self.peak_ret = 0.0
                    hard_stop_hit = True
                    break

        prev_trade_asset = self.trade_asset_ret
        prev_trade_bench = self.trade_bench_ret
        prev_cum_excess = prev_trade_asset - prev_trade_bench - (self.trade_txn_costs - txn_cost_pct)

        if self.is_holding:
            self.trade_asset_ret = next_asset / self.entry_price - 1.0
            self.trade_bench_ret = next_bench / self.bench_entry - 1.0
            self.peak_ret = max(self.peak_ret, self.trade_asset_ret)
        else:
            self.trade_asset_ret = 0.0
            self.trade_bench_ret = 0.0
            self.peak_ret = 0.0
            
        curr_cum_excess = self.trade_asset_ret - self.trade_bench_ret - self.trade_txn_costs

        self.step_idx += 1
        self.done = self.step_idx >= len(self.decision_dates) - 1

        reward_info = calculate_step_reward(
            action=action,
            step_asset_ret=step_asset_ret,
            step_bench_ret=step_bench_ret,
            txn_cost_pct=txn_cost_pct,
            prev_cum_asset_ret=prev_trade_asset,
            prev_cum_bench_ret=prev_trade_bench, # Note: using curr/prev cum_excess implicitly incorporates costs now
            curr_cum_asset_ret=self.trade_asset_ret - self.trade_txn_costs, # Shift costs directly to the return params so calculate_step_reward doesn't need signature change
            curr_cum_bench_ret=self.trade_bench_ret,
        )

        return self._get_obs(), reward_info["reward"], self.done, reward_info

    def _get_obs(self) -> np.ndarray:
        if not self.decision_dates:
            day = self.t_theta
        else:
            day = self.decision_dates[self.step_idx] if self.step_idx < len(self.decision_dates) else self.decision_dates[-1]
            
        market = compute_market_state(self.prices, self.probs, self.symbol, self.market_id, self.bench_sym, day)
        
        total_steps = max(1, len(self.decision_dates) - 1)
        fraction = min(1.0, self.step_idx / total_steps)
        
        asset_px = float(_close_on(self.prices, self.symbol, day) or self.entry_price)
        position = compute_position_state(
            is_long=(self.current_action == ACTION_ASSET),
            entry_price=self.entry_price if self.current_action == ACTION_ASSET else 0.0,
            asset_price=asset_px,
            peak_ret=self.peak_ret if self.current_action == ACTION_ASSET else 0.0,
            window_fraction=fraction,
            position_size_pct=POSITION_SIZE_PCT if self.current_action == ACTION_ASSET else 0.0,
        )
        return build_observation(self.static, position, market, self.scaler)
