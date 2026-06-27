import math

from rl.exits import evaluate_hard_exit
from rl.tests._synth import DATES


def test_profit_lock_exit_price_matches_cem_floor():
    prices = {
        "AAA": [
            (DATES[20], 100.0, 100.0, 100.0),
            (DATES[21], 110.0, 100.0, 108.0),
            (DATES[22], 108.0, 105.0, 106.0),
        ]
    }
    signal = evaluate_hard_exit(
        prices=prices,
        probs={"M": [(DATES[20], 0.85), (DATES[22], 0.85)]},
        symbol="AAA",
        market_id="M",
        day=DATES[22],
        entry_day=DATES[20],
        entry_price=100.0,
        peak_ret=0.10,
        lock_activate=0.03,
        t_e=DATES[40],
        is_earnings=False,
    )

    assert signal is not None
    assert signal.reason == "profit_lock_10%"
    assert math.isclose(signal.exit_price, 110.0)
