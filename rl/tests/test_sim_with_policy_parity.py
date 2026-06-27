"""sim_with_policy must reproduce sim_opp_cost's rotation accounting.

Replay the exact trade simulate_one produces (same entry/exit day + price)
through both simulators on one candidate and assert identical final equity.
"""
import pandas as pd
import torch

import run_experiments as RE
from pipeline.strategy import simulate_one, DEFAULT_POLICY
from rl.config import ACTION_DIM, ACTION_HOLD, action_for_entry_params
from rl.sim_with_policy import sim_with_policy
from rl.tests._synth import make_prices, make_probs, make_candidate, DATES


class StaticLogitPolicy:
    def __init__(self, *, enter_action: int, exit_while_long: bool = False):
        self.enter_action = enter_action
        self.exit_while_long = exit_while_long

    def forward(self, obs):
        logits = torch.zeros((obs.shape[0], ACTION_DIM), dtype=torch.float32)
        logits[:, ACTION_HOLD] = 5.0
        logits[:, self.enter_action] = 10.0
        if self.exit_while_long:
            logits[:, -1] = 12.0
        value = torch.zeros((obs.shape[0], 1), dtype=torch.float32)
        return logits, value


def test_accounting_matches_sim_opp_cost():
    prices = make_prices({"AAA": (100.0, 0.008), "SPY": (400.0, 0.001)})
    probs = make_probs("M", level=0.85, start_idx=20)
    row = make_candidate("AAA", "M", t_theta_idx=20, t_e_idx=60)
    df = pd.DataFrame([row])  # index 0

    policy = {**DEFAULT_POLICY, "position_size_pct": 0.10, "max_concurrent": 10}
    start, end = DATES[20], DATES[60]

    # The trade simulate_one yields (also what sim_opp_cost replays internally).
    trade = simulate_one(row, prices, probs, policy)
    assert trade is not None

    cem_trades, cem_eq, cem_stats, _ = RE.sim_opp_cost(
        df, prices, probs, policy, bench_sym="SPY", initial=RE.INITIAL_CAPITAL,
        use_kelly=False, start_date=start, end_date=end,
    )

    fixed = {0: {
        "entry_date": trade["entry_date"], "exit_date": trade["exit_date"],
        "entry_price": trade["entry_price"], "exit_price": trade["exit_price"],
    }}
    rl_trades, rl_eq, rl_stats = sim_with_policy(
        df, prices, probs, policy=None, fixed_trades=fixed, bench_sym="SPY",
        initial=RE.INITIAL_CAPITAL, use_kelly=False, base_ps=0.10, max_concurrent=10,
        start_date=start, end_date=end,
    )

    assert cem_stats["n_trades"] == rl_stats["n_trades"] == 1
    # Same rotation math => same final equity (allow a few cents of rounding).
    assert abs(cem_stats["final"] - rl_stats["final"]) < 1.0, (
        f"final equity diverged: sim_opp_cost={cem_stats['final']} vs "
        f"sim_with_policy={rl_stats['final']}"
    )


def test_policy_enter_action_controls_position_size_and_profit_lock():
    prices = make_prices({"AAA": (100.0, 0.008), "SPY": (400.0, 0.001)})
    probs = make_probs("M", level=0.85, start_idx=20)
    row = make_candidate("AAA", "M", t_theta_idx=20, t_e_idx=60)
    df = pd.DataFrame([row])
    enter_action = action_for_entry_params(0.20, 0.08)

    trades, _, stats = sim_with_policy(
        df, prices, probs, policy=StaticLogitPolicy(enter_action=enter_action), bench_sym="SPY",
        initial=RE.INITIAL_CAPITAL, use_kelly=False, base_ps=0.10, max_concurrent=10,
        start_date=DATES[20], end_date=DATES[60],
    )

    assert stats["n_trades"] == 1
    assert trades["_position_size_pct"].iloc[0] == 0.20
    assert trades["_lock_activate"].iloc[0] == 0.08


def test_poly_exit_fires_before_policy_hold():
    prices = make_prices({"AAA": (100.0, 0.0), "SPY": (400.0, 0.0)})
    probs = {"M": [(d, 0.85 if i < 23 else 0.50) for i, d in enumerate(DATES) if i >= 20]}
    row = make_candidate("AAA", "M", t_theta_idx=20, t_e_idx=35)
    row["feat_archetype"] = "earnings"
    df = pd.DataFrame([row])

    trades, _, stats = sim_with_policy(
        df, prices, probs, policy=StaticLogitPolicy(enter_action=action_for_entry_params(0.10, 0.03)),
        bench_sym="SPY", initial=RE.INITIAL_CAPITAL, use_kelly=False,
        start_date=DATES[20], end_date=DATES[35],
    )

    assert stats["n_trades"] == 1
    assert trades["realized_exit_reason"].iloc[0] == "poly<0.55"
    assert trades["exit_date"].iloc[0] == str(DATES[23].date())


def test_resolution_exit_is_one_day_before_resolution():
    prices = make_prices({"AAA": (100.0, 0.0), "SPY": (400.0, 0.0)})
    probs = make_probs("M", level=0.85, start_idx=20)
    row = make_candidate("AAA", "M", t_theta_idx=20, t_e_idx=25)
    row["feat_archetype"] = "earnings"
    df = pd.DataFrame([row])

    trades, _, stats = sim_with_policy(
        df, prices, probs, policy=StaticLogitPolicy(enter_action=action_for_entry_params(0.10, 0.03)),
        bench_sym="SPY", initial=RE.INITIAL_CAPITAL, use_kelly=False,
        start_date=DATES[20], end_date=DATES[25],
    )

    assert stats["n_trades"] == 1
    assert trades["realized_exit_reason"].iloc[0] == "resolution-1d"
    assert trades["exit_date"].iloc[0] == str((DATES[25] - pd.Timedelta(days=1)).date())


def test_resolution_exit_uses_previous_tradable_day_when_cut_is_weekend():
    monday_idx = next(i for i, d in enumerate(DATES) if i > 25 and d.weekday() == 0)
    prices = make_prices({"AAA": (100.0, 0.0), "SPY": (400.0, 0.0)})
    probs = make_probs("M", level=0.85, start_idx=monday_idx - 4)
    row = make_candidate("AAA", "M", t_theta_idx=monday_idx - 4, t_e_idx=monday_idx)
    row["feat_archetype"] = "earnings"
    df = pd.DataFrame([row])

    trades, _, stats = sim_with_policy(
        df, prices, probs, policy=StaticLogitPolicy(enter_action=action_for_entry_params(0.10, 0.03)),
        bench_sym="SPY", initial=RE.INITIAL_CAPITAL, use_kelly=False,
        start_date=DATES[monday_idx - 4], end_date=DATES[monday_idx],
    )

    assert stats["n_trades"] == 1
    assert trades["realized_exit_reason"].iloc[0] == "resolution-1d"
    assert trades["exit_date"].iloc[0] == str(DATES[monday_idx - 1].date())


def test_no_entry_when_no_tradable_day_remains_before_resolution_cap():
    monday_idx = next(i for i, d in enumerate(DATES) if i > 25 and d.weekday() == 0)
    prices = make_prices({"AAA": (100.0, 0.0), "SPY": (400.0, 0.0)})
    probs = make_probs("M", level=0.85, start_idx=monday_idx)
    row = make_candidate("AAA", "M", t_theta_idx=monday_idx, t_e_idx=monday_idx)
    row["feat_archetype"] = "earnings"
    df = pd.DataFrame([row])

    trades, _, stats = sim_with_policy(
        df, prices, probs, policy=StaticLogitPolicy(enter_action=action_for_entry_params(0.10, 0.03)),
        bench_sym="SPY", initial=RE.INITIAL_CAPITAL, use_kelly=False,
        start_date=DATES[monday_idx], end_date=DATES[monday_idx],
    )

    assert stats["n_trades"] == 0
    assert trades.empty


def test_policy_exit_only_when_no_hard_exit_fired():
    prices = make_prices({"AAA": (100.0, 0.0), "SPY": (400.0, 0.0)})
    probs = make_probs("M", level=0.85, start_idx=20)
    row = make_candidate("AAA", "M", t_theta_idx=20, t_e_idx=40)
    row["feat_archetype"] = "earnings"
    df = pd.DataFrame([row])

    trades, _, stats = sim_with_policy(
        df, prices, probs,
        policy=StaticLogitPolicy(enter_action=action_for_entry_params(0.10, 0.03), exit_while_long=True),
        bench_sym="SPY", initial=RE.INITIAL_CAPITAL, use_kelly=False,
        start_date=DATES[20], end_date=DATES[40],
    )

    assert stats["n_trades"] == 1
    assert trades["realized_exit_reason"].iloc[0] == "policy_exit"
