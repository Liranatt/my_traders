"""Environment behavior checks: long-only masking, single entry, sane P&L."""
from rl.env import TradingEnv, has_valid_episode
from rl.config import ENTER_ACTIONS
from rl.tests._synth import make_prices, make_probs, make_candidate


def _setup():
    prices = make_prices({"AAA": (100.0, 0.01), "SPY": (400.0, 0.0)})  # asset +1%/day, bench flat
    probs = make_probs("M", level=0.85, start_idx=20)  # band fires at t_theta
    row = make_candidate("AAA", "M", t_theta_idx=20, t_e_idx=60)
    return row, prices, probs


def test_long_only_and_single_entry_and_profit():
    row, prices, probs = _setup()
    assert has_valid_episode(row, prices, probs, "SPY")
    env = TradingEnv(row, prices, probs, "SPY")
    env.reset()

    # First eligible FLAT day: ENTER + HOLD allowed, EXIT (short) is NOT.
    mask = env.get_action_mask()
    assert mask[env.HOLD] and mask[env.ENTER] and not mask[env.EXIT]
    assert sum(mask[action] for action in ENTER_ACTIONS) == len(ENTER_ACTIONS)

    entries = 0
    done = False
    steps = 0
    while not done and steps < 500:
        mask = env.get_action_mask()
        if env.state == "FLAT" and any(mask[action] for action in ENTER_ACTIONS):
            action = env.ENTER
            entries += 1
        elif env.state == "LONG" and not mask[env.HOLD]:  # forced exit on last day
            action = env.EXIT
        else:
            action = env.HOLD
        _, _, done, _ = env.step(action)
        steps += 1

    assert env.traded and entries == 1          # exactly one long entry, never re-enters
    assert env.entry_step == 0                   # entered on the first eligible day
    # asset compounded +1%/day vs flat benchmark over the hold => clearly net positive
    assert env.equity_curve[-1] > 1.0


def test_flat_episode_when_band_never_fires():
    prices = make_prices({"AAA": (100.0, 0.01), "SPY": (400.0, 0.0)})
    probs = make_probs("M", level=0.10, start_idx=20)  # never reaches enter_floor
    row = make_candidate("AAA", "M", t_theta_idx=20, t_e_idx=60)
    assert not has_valid_episode(row, prices, probs, "SPY")
    env = TradingEnv(row, prices, probs, "SPY")
    env.reset()
    done = False
    steps = 0
    while not done and steps < 500:
        mask = env.get_action_mask()
        assert not any(mask[action] for action in ENTER_ACTIONS)  # never enterable
        _, _, done, _ = env.step(env.HOLD)
        steps += 1
    assert not env.traded
