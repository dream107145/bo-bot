"""The venue fee curve, verified against the docs example (100 shares @ 0.50 = $1.75)."""
from __future__ import annotations

import pytest

from troll_poly_bot.execution.paper import FeeModel
from troll_poly_bot.signals.costs import CostModel, FeeSchedule


def test_docs_example():
    fs = FeeSchedule()
    assert fs.taker_fee_per_share(0.5) * 100 == pytest.approx(1.75)


def test_curve_is_symmetric_and_vanishes_at_extremes():
    fs = FeeSchedule()
    assert fs.taker_fee_per_share(0.3) == pytest.approx(fs.taker_fee_per_share(0.7))
    assert fs.taker_fee_per_share(0.0) == 0.0
    assert fs.taker_fee_per_share(1.0) == 0.0
    assert fs.taker_fee_per_share(0.95) < fs.taker_fee_per_share(0.5) / 4


def test_old_assumption_understated_the_hurdle():
    old = FeeModel(rate=0.02)                       # legacy shape
    new = FeeModel.from_schedule(FeeSchedule())
    assert new.charge(0.5, 100) / old.charge(0.5, 100) == pytest.approx(1.75)
    assert new.charge(0.8, 100) / old.charge(0.8, 100) == pytest.approx(2.8)


def test_from_gamma_row_and_disabled():
    row = {"feeSchedule": {"rate": 0.05, "exponent": 0.5, "takerOnly": True, "rebateRate": 0.1},
           "feesEnabled": True}
    fs = FeeSchedule.from_gamma(row)
    assert fs.rate == 0.05 and fs.exponent == 0.5 and fs.maker_rebate == 0.1
    assert fs.taker_fee_per_share(0.5) == pytest.approx(0.05 * 0.25 ** 0.5)
    off = FeeSchedule.from_gamma({"feesEnabled": False})
    assert off.taker_fee_per_share(0.5) == 0.0
    assert FeeSchedule.from_gamma({}) == FeeSchedule()


def test_per_token_schedule_overrides_default():
    fm = FeeModel.from_schedule(FeeSchedule(rate=0.07))
    fm.set_token_schedule("T", FeeSchedule(rate=0.0, enabled=False))
    assert fm.charge(0.5, 10, "T") == 0.0
    assert fm.charge(0.5, 10, "other") == pytest.approx(0.175)


def test_cost_breakdown_arithmetic():
    cm = CostModel(fee=FeeSchedule(), expected_slippage=0.005, uncertainty_charge=1.0)
    cb = cm.evaluate(model_p=0.70, ask=0.60, bid=0.59, model_uncertainty=0.02)
    assert cb.gross_edge == pytest.approx(0.10)
    assert cb.fee == pytest.approx(0.07 * 0.6 * 0.4)
    assert cb.half_spread == pytest.approx(0.005)
    assert cb.net_edge == pytest.approx(0.10 - cb.fee - 0.005 - 0.02)
    assert cb.as_dict()["net_edge"] == pytest.approx(cb.net_edge, abs=1e-5)
