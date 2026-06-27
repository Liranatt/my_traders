import math

from rl.reward import DifferentialSharpeRatio


def test_zero_return_gives_zero_dsr():
    dsr = DifferentialSharpeRatio(eta=0.1)
    assert all(math.isclose(dsr.update(0.0), 0.0, abs_tol=1e-9) for _ in range(10))


def test_cost_spike_is_negative():
    dsr = DifferentialSharpeRatio(eta=0.1)
    for _ in range(3):
        dsr.update(0.01)
    assert dsr.update(-0.05) < 0.0


def test_uses_previous_moments_not_post_update():
    # Seed known moments and check the formula uses A_{t-1}, B_{t-1}.
    dsr = DifferentialSharpeRatio(eta=0.1)
    dsr.A, dsr.B = 0.01, 0.001
    R = 0.02
    a_prev, b_prev = dsr.A, dsr.B
    dA, dB = R - a_prev, R * R - b_prev
    var = b_prev - a_prev * a_prev
    expected = (b_prev * dA - 0.5 * a_prev * dB) / var ** 1.5
    got = dsr.update(R)
    assert math.isclose(got, expected, rel_tol=1e-9)
    # moments advanced after computing the reward
    assert math.isclose(dsr.A, a_prev + 0.1 * dA, rel_tol=1e-12)


def test_reset_restores_initial_moments():
    dsr = DifferentialSharpeRatio(eta=0.2)
    for _ in range(5):
        dsr.update(0.03)
    dsr.reset()
    assert dsr.A == 0.0 and dsr.B == 0.0


def test_positive_streak_nonnegative_dsr():
    dsr = DifferentialSharpeRatio(eta=0.1)
    assert all(d >= -1e-9 for d in (dsr.update(0.02) for _ in range(10)))
