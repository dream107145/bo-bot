"""Tests for TWAP-aware pricing.

The Monte Carlo test is the one that matters: the analytic variance is the
entire basis of the near-expiry edge, so it gets checked against simulated
Brownian paths rather than against my own algebra.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from troll_poly_bot.pricing.twap import (
    TWAP_WINDOW_S,
    TwapState,
    twap_fair_value,
    twap_variance,
)

SIGMA = 9e-5          # ~0.9 bps/s, BTC at ~50% annualised


# ------------------------------------------------------------- the variance


def test_variance_branches_agree_at_the_window_boundary():
    """A discontinuity here would make prices jump at exactly 60s left."""
    lo = twap_variance(TWAP_WINDOW_S - 1e-9, SIGMA)
    hi = twap_variance(TWAP_WINDOW_S + 1e-9, SIGMA)
    assert lo == pytest.approx(hi, rel=1e-6)
    assert hi == pytest.approx(SIGMA**2 * 20.0, rel=1e-9)


def test_variance_matches_the_documented_numbers():
    s2 = SIGMA**2
    assert twap_variance(300.0, SIGMA) == pytest.approx(s2 * 260.0)
    assert twap_variance(60.0, SIGMA) == pytest.approx(s2 * 20.0)
    assert twap_variance(30.0, SIGMA) == pytest.approx(s2 * 2.5)
    assert twap_variance(10.0, SIGMA) == pytest.approx(s2 * 10.0 / 108.0)


def test_twap_is_far_more_certain_than_spot_near_expiry():
    """The whole reason this module exists."""
    for u, expected_ratio in ((300.0, 260 / 300), (60.0, 20 / 60), (10.0, 1 / 108)):
        naive = SIGMA**2 * u
        assert twap_variance(u, SIGMA) / naive == pytest.approx(expected_ratio, rel=1e-6)


def test_variance_is_monotonic_in_time_remaining():
    us = [1.0, 5.0, 20.0, 59.0, 60.0, 120.0, 300.0]
    vs = [twap_variance(u, SIGMA) for u in us]
    assert vs == sorted(vs)


@pytest.mark.parametrize("u", [10.0, 30.0, 60.0, 150.0, 300.0])
def test_variance_matches_monte_carlo(u):
    """Check the algebra against simulated Brownian paths."""
    rng = np.random.default_rng(12345)
    w = TWAP_WINDOW_S
    dt = 0.05                                  # 50ms steps
    n_paths = 4000
    horizon = u
    n_steps = int(round(horizon / dt))
    # log-price increments from now to close
    incr = rng.standard_normal((n_paths, n_steps)) * (SIGMA * math.sqrt(dt))
    path = np.cumsum(incr, axis=1)             # X_{t+k} - X_t
    # the settling window is the final `w` seconds (or what remains of it)
    start = max(0, n_steps - int(round(w / dt)))
    tail = path[:, start:]
    # observed part before `now` is zero here (we price from t with X_t = 0),
    # so any part of the averaging window already elapsed contributes 0
    elapsed_in_window = max(0.0, w - u)
    b = (tail.sum(axis=1) * dt + 0.0 * elapsed_in_window) / w
    assert np.var(b) == pytest.approx(twap_variance(u, SIGMA), rel=0.08)


# ------------------------------------------------------------ TwapState


def _feed(state: TwapState, price_at, t0: float, t1: float, step: float = 250.0):
    t = t0
    while t <= t1:
        state.update(price_at(t), t)
        t += step


def test_trailing_twap_of_a_flat_series_is_the_price():
    st = TwapState()
    _feed(st, lambda t: 100_000.0, 0.0, 120_000.0)
    assert st.trailing_twap(120_000.0) == pytest.approx(100_000.0, rel=1e-9)


def test_trailing_twap_of_a_ramp_is_below_the_latest_price():
    """A rising series sits above its own trailing average -- which is exactly
    why these markets do not open at 50/50."""
    st = TwapState()
    _feed(st, lambda t: 100_000.0 * math.exp(1e-7 * t), 0.0, 120_000.0)
    now = 120_000.0
    twap = st.trailing_twap(now)
    latest = 100_000.0 * math.exp(1e-7 * now)
    assert twap is not None
    assert twap < latest
    # 60s window on a steady ramp -> average sits ~30s back
    expected = 100_000.0 * math.exp(1e-7 * (now - 30_000.0))
    assert twap == pytest.approx(expected, rel=1e-3)


def test_state_not_ready_without_samples():
    assert not TwapState().ready


# ------------------------------------------------------- the fair value


def test_market_does_not_open_at_fifty_fifty_after_a_trend():
    """Spot above the trailing average means P(up) > 0.5 the moment it opens."""
    st = TwapState()
    _feed(st, lambda t: 100_000.0 * math.exp(2e-7 * t), 0.0, 60_000.0)
    strike = st.trailing_twap(60_000.0)
    fv = twap_fair_value(st, strike, SIGMA, now_ms=60_000.0, close_ts_ms=360_000.0)
    assert fv.p_up > 0.5, "a rising series must open above 50/50"


def test_flat_series_opens_at_fifty_fifty():
    st = TwapState()
    _feed(st, lambda t: 100_000.0, 0.0, 60_000.0)
    strike = st.trailing_twap(60_000.0)
    fv = twap_fair_value(st, strike, SIGMA, now_ms=60_000.0, close_ts_ms=360_000.0)
    assert fv.p_up == pytest.approx(0.5, abs=1e-6)


def test_confidence_grows_sharply_inside_the_settling_window():
    """Same distance from strike, less time: TWAP converges far faster."""
    st = TwapState()
    _feed(st, lambda t: 100_000.0, 0.0, 300_000.0, step=250.0)
    st.update(100_050.0, 300_000.0)            # a 5bp jump
    close = 300_000.0
    ps = []
    for u in (120.0, 60.0, 30.0, 10.0):
        fv = twap_fair_value(
            st, 100_000.0, SIGMA,
            now_ms=close - u * 1000.0, close_ts_ms=close,
        )
        ps.append(fv.p_up)
    assert ps == sorted(ps), "confidence must rise as expiry approaches"
    assert ps[-1] > 0.99


def test_info_lag_still_reduces_confidence():
    st = TwapState()
    _feed(st, lambda t: 100_000.0, 0.0, 200_000.0)
    st.update(100_050.0, 200_000.0)
    kw = dict(state=st, strike=100_000.0, sigma_per_sec=SIGMA,
              now_ms=200_000.0, close_ts_ms=260_000.0)
    fresh = twap_fair_value(info_lag_s=0.0, **kw)
    stale = twap_fair_value(info_lag_s=0.5, **kw)
    assert 0.5 < stale.p_up < fresh.p_up


def test_degenerate_inputs_return_a_coin_flip():
    st = TwapState()
    fv = twap_fair_value(st, 100_000.0, SIGMA, now_ms=0.0, close_ts_ms=300_000.0)
    assert fv.p_up == pytest.approx(0.5)


# ───────────────────── regression: info-lag double-count ──────────────────
#
# A live run priced a token at fair=1.000 that the market had at 1 cent, and
# lost on it. Cause: the observed integral ran to `now` while the unknown
# remainder was ALSO weighted by u = time_left + info_lag, counting info_lag
# twice. The mean inflated by ~info_lag/window * ln(price) -- on BTC that is
# ~0.09 in log space, i.e. believing price is 9% higher than it is.


def _flat_state(price=76_000.0, span_s=200.0, step_ms=250.0):
    st = TwapState()
    t = 0.0
    while t <= span_s * 1000.0:
        st.update(price, t)
        t += step_ms
    return st, span_s * 1000.0


@pytest.mark.parametrize("lag_s", [0.0, 0.25, 0.5, 1.0, 2.0])
def test_info_lag_never_inflates_the_mean(lag_s):
    """On a dead-flat series every probability must stay at 0.5 regardless of
    lag. Any drift away from 0.5 here is pure accounting error."""
    st, now = _flat_state()
    fv = twap_fair_value(
        st, strike=76_000.0, sigma_per_sec=SIGMA,
        now_ms=now, close_ts_ms=now + 30_000.0, info_lag_s=lag_s,
    )
    assert fv.p_up == pytest.approx(0.5, abs=0.02), (
        f"flat series with {lag_s}s lag priced at {fv.p_up:.4f}, not 0.5"
    )


@pytest.mark.parametrize("secs_left", [5.0, 15.0, 30.0, 45.0, 59.0])
def test_mean_stays_within_observed_range_inside_the_window(secs_left):
    """The settling average is a weighted mean of log prices, so it cannot sit
    outside the range of its inputs. This invariant is what the bug violated."""
    st, now = _flat_state()
    close = now + secs_left * 1000.0
    fv = twap_fair_value(
        st, strike=76_000.0, sigma_per_sec=SIGMA,
        now_ms=now, close_ts_ms=close, info_lag_s=0.5,
    )
    # a flat series at the strike cannot justify near-certainty either way
    assert 0.02 < fv.p_up < 0.98, f"{secs_left}s left -> p_up {fv.p_up:.4f}"


def test_penny_longshot_is_not_priced_as_a_certainty():
    """The exact live failure: 60s left, price essentially at the strike, and
    a sub-second feed lag. Must not produce a confident call."""
    st, now = _flat_state(price=76_000.0)
    st.update(76_005.0, now)                     # a 0.7bp nudge up
    fv = twap_fair_value(
        st, strike=76_000.0, sigma_per_sec=SIGMA,
        now_ms=now, close_ts_ms=now + 60_000.0, info_lag_s=0.5,
    )
    assert fv.p_up < 0.95, f"priced a coin flip at {fv.p_up:.4f}"


def test_observed_range_bounds_are_reported():
    st, now = _flat_state()
    lo, hi = st.observed_range(now - 60_000.0, now)
    assert lo is not None and hi is not None
    assert lo == pytest.approx(math.log(76_000.0))
    assert hi == pytest.approx(math.log(76_000.0))


def test_coverage_reports_partial_windows():
    """A strike taken before the window is fully observed is not the venue's
    strike, and on a trending market it is biased with the trend."""
    st = TwapState()
    _feed(st, lambda t: 76_000.0, 0.0, 30_000.0)      # only 30s of history
    assert st.coverage_s(30_000.0) == pytest.approx(30.0, abs=1.0)
    assert not st.fully_covered(30_000.0)
    _feed(st, lambda t: 76_000.0, 30_250.0, 65_000.0)
    assert st.fully_covered(65_000.0)


def test_partial_window_strike_is_biased_by_the_trend():
    """Demonstrates the cost: on a rising series a short window averages higher
    than the full 60s window, so the strike comes out too high."""
    st = TwapState()
    _feed(st, lambda t: 76_000.0 * math.exp(2e-7 * t), 0.0, 30_000.0)
    partial = st.trailing_twap(30_000.0)
    _feed(st, lambda t: 76_000.0 * math.exp(2e-7 * t), 30_250.0, 90_000.0)
    full = st.trailing_twap(90_000.0)
    # both measured on the same rising path; the partial one sits nearer spot
    assert partial is not None and full is not None
    assert partial > 76_000.0 * math.exp(2e-7 * 15_000.0) * 0.999
