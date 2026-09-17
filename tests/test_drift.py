"""The pricer's drift hook: an expected order-flow drift shifts the settling mean."""
from __future__ import annotations

import math

import pytest

from troll_poly_bot.pricing.twap import TwapState, twap_fair_value, twap_variance

T0 = 1_000_000_000.0


def _state(price=100.0):
    st = TwapState()
    for s in range(120):
        st.update(price, T0 - 120_000.0 + s * 1000.0)
    st.update(price, T0)
    return st


def test_zero_drift_is_unchanged_and_positive_drift_raises_p_up():
    st = _state()
    base = twap_fair_value(st, 100.0, 1e-4, T0, T0 + 200_000.0)
    same = twap_fair_value(st, 100.0, 1e-4, T0, T0 + 200_000.0, drift_log=0.0)
    up = twap_fair_value(st, 100.0, 1e-4, T0, T0 + 200_000.0, drift_log=2e-4)
    down = twap_fair_value(st, 100.0, 1e-4, T0, T0 + 200_000.0, drift_log=-2e-4)
    assert base.p_up == pytest.approx(0.5, abs=1e-6)
    assert same.p_up == base.p_up
    assert up.p_up > base.p_up > down.p_up
    assert up.p_up - 0.5 == pytest.approx(0.5 - down.p_up, abs=1e-9)


def test_drift_moves_the_mean_by_the_unobserved_fraction():
    """Before the settling window the whole drift applies; inside it only the
    still-random fraction u / window does. Checked through z * sd = mean shift."""
    st = _state()
    drift = 1e-4
    for left_s, expected_fraction in ((200.0, 1.0), (30.0, 0.5), (15.0, 0.25)):
        with_d = twap_fair_value(st, 100.0, 1e-4, T0, T0 + left_s * 1000.0, drift_log=drift)
        without = twap_fair_value(st, 100.0, 1e-4, T0, T0 + left_s * 1000.0)
        sd = math.sqrt(twap_variance(left_s, 1e-4))
        assert (with_d.z - without.z) * sd == pytest.approx(drift * expected_fraction, rel=1e-6)


def test_drift_matters_less_deep_in_the_tails():
    st = _state()
    atm = twap_fair_value(st, 100.0, 1e-4, T0, T0 + 200_000.0, drift_log=1e-4)
    deep = twap_fair_value(st, 99.0, 1e-4, T0, T0 + 200_000.0, drift_log=1e-4)
    deep0 = twap_fair_value(st, 99.0, 1e-4, T0, T0 + 200_000.0)
    assert (atm.p_up - 0.5) > (deep.p_up - deep0.p_up)
