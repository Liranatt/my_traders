from rl.exits import evaluate_hard_exit
from rl.tests._synth import DATES


def _flat_prices():
    return {
        "AAA": [
            (DATES[20], 100.0, 100.0, 100.0),
            (DATES[21], 100.0, 100.0, 100.0),
            (DATES[22], 100.0, 100.0, 100.0),
        ]
    }


def test_poly_below_half_exits_immediately():
    signal = evaluate_hard_exit(
        prices=_flat_prices(),
        probs={"M": [(DATES[20], 0.85), (DATES[21], 0.49)]},
        symbol="AAA",
        market_id="M",
        day=DATES[21],
        entry_day=DATES[20],
        entry_price=100.0,
        t_e=DATES[40],
    )

    assert signal is not None
    assert signal.reason == "poly<0.5"


def test_poly_below_threshold_exits_only_when_not_improving():
    improving = evaluate_hard_exit(
        prices=_flat_prices(),
        probs={"M": [(DATES[20], 0.52), (DATES[21], 0.54)]},
        symbol="AAA",
        market_id="M",
        day=DATES[21],
        entry_day=DATES[20],
        entry_price=100.0,
        t_e=DATES[40],
    )
    weakening = evaluate_hard_exit(
        prices=_flat_prices(),
        probs={"M": [(DATES[20], 0.58), (DATES[21], 0.54)]},
        symbol="AAA",
        market_id="M",
        day=DATES[21],
        entry_day=DATES[20],
        entry_price=100.0,
        t_e=DATES[40],
    )

    assert improving is None
    assert weakening is not None
    assert weakening.reason == "poly<0.55"
