"""Tests for the latency model and the paper matching engine.

These are the tests that matter most in this repo. A bug here does not raise --
it just prints a better PnL than reality, which is the most expensive kind of
bug a trading system can have.
"""
from __future__ import annotations

import statistics

import pytest

from troll_poly_bot.execution.latency import (
    HOME_BROADBAND,
    INSTANT,
    VPS_NEARBY,
    LatencyModel,
    degraded,
)
from troll_poly_bot.execution.paper import (
    FeeModel,
    PaperExchange,
    RejectReason,
)
from troll_poly_bot.feeds.delayed import DelayedFeed
from troll_poly_bot.pricing.digital import fair_value
from troll_poly_bot.pricing.vol import EwmaVol
from troll_poly_bot.types import BookLevel, Order, OrderBook, Side, TimeInForce


def book(bids, asks, ts=0.0, token="T"):
    return OrderBook(
        token_id=token,
        bids=[BookLevel(p, s) for p, s in bids],
        asks=[BookLevel(p, s) for p, s in asks],
        ts=ts,
    )


# ----------------------------------------------------------------- latency


def test_instant_profile_is_actually_instant():
    m = LatencyModel(INSTANT, seed=1)
    assert m.submit() == 0.0 and m.ack() == 0.0 and m.md_spot() == 0.0


def test_latency_mean_matches_base_plus_jitter():
    m = LatencyModel(VPS_NEARBY, seed=42)
    draws = [m.submit() for _ in range(20_000)]
    expected = VPS_NEARBY.submit_base + VPS_NEARBY.submit_jitter
    # spikes pull the mean up slightly; allow for them
    assert expected <= statistics.fmean(draws) <= expected * 1.25
    assert min(draws) >= VPS_NEARBY.submit_base


def test_latency_is_right_skewed_not_constant():
    m = LatencyModel(HOME_BROADBAND, seed=3)
    draws = sorted(m.submit() for _ in range(10_000))
    median = draws[len(draws) // 2]
    mean = statistics.fmean(draws)
    assert mean > median, "latency must be right-skewed, not symmetric"
    assert draws[-1] > draws[0] * 2, "needs a meaningful tail"


def test_latency_is_reproducible_across_runs():
    a = [LatencyModel(HOME_BROADBAND, seed=9).submit() for _ in range(5)]
    b = [LatencyModel(HOME_BROADBAND, seed=9).submit() for _ in range(5)]
    assert a == b, "paper runs must be comparable across strategy changes"


def test_degraded_scales_everything():
    d = degraded(VPS_NEARBY, 2.0)
    assert d.submit_base == VPS_NEARBY.submit_base * 2
    assert d.reaction_lag_ms() == pytest.approx(VPS_NEARBY.reaction_lag_ms() * 2)


# ------------------------------------------------------- the core behaviour


def test_fill_uses_the_book_at_arrival_not_at_decision():
    """The whole point of the simulator.

    Strategy sees a 0.40 ask and sends. While the order is on the wire the ask
    moves to 0.46. The fill must happen at 0.46, not 0.40.
    """
    ex = PaperExchange(LatencyModel(VPS_NEARBY, seed=1), FeeModel(rate=0.0))
    books = {"T": book([], [(0.40, 500)])}

    o = Order(token_id="T", side=Side.BUY, price=0.50, size=10,
              tif=TimeInForce.IOC, expected_price=0.40)
    ex.submit(o, now=0.0)

    # book moves against us mid-flight
    books["T"] = book([], [(0.46, 500)])

    results = []
    t = 0.0
    while t < 500.0 and not results:
        t += 1.0
        results = ex.step(t, lambda tid: books.get(tid))

    assert results, "order should have landed"
    fills = results[0].fills
    assert fills, "IOC should have filled against the worse book"
    assert fills[0].price == pytest.approx(0.46)
    assert fills[0].slippage == pytest.approx(0.06), "6c of latency cost"


def test_fok_is_rejected_when_liquidity_vanishes_in_flight():
    ex = PaperExchange(LatencyModel(VPS_NEARBY, seed=2), FeeModel(rate=0.0))
    books = {"T": book([], [(0.40, 500)])}

    o = Order(token_id="T", side=Side.BUY, price=0.41, size=100,
              tif=TimeInForce.FOK, expected_price=0.40)
    ex.submit(o, now=0.0)
    books["T"] = book([], [(0.55, 500)])   # gapped away

    results = []
    t = 0.0
    while t < 500.0 and not results:
        t += 1.0
        results = ex.step(t, lambda tid: books.get(tid))

    assert results[0].is_rejected
    assert results[0].reject_reason is RejectReason.FOK_UNFILLABLE
    assert not results[0].fills


def test_order_arriving_after_close_is_rejected():
    """At 5m expiries this is a routine event, not an edge case."""
    ex = PaperExchange(LatencyModel(HOME_BROADBAND, seed=4), FeeModel(rate=0.0))
    books = {"T": book([], [(0.90, 500)])}
    close_ts = 50.0                      # closes in 50ms; RTT is ~100ms

    o = Order(token_id="T", side=Side.BUY, price=0.95, size=10, tif=TimeInForce.FOK)
    ex.submit(o, now=0.0)

    results = []
    t = 0.0
    while t < 2000.0 and not results:
        t += 1.0
        results = ex.step(t, lambda tid: books.get(tid), lambda tid: close_ts)

    assert results[0].reject_reason is RejectReason.MARKET_CLOSED


def test_contention_removes_top_of_book_size():
    """Faster participants eat the touch before you get there."""
    profile = VPS_NEARBY            # contention = 0.55
    ex = PaperExchange(LatencyModel(profile, seed=5), FeeModel(rate=0.0))
    books = {"T": book([], [(0.40, 100), (0.41, 1000)])}

    o = Order(token_id="T", side=Side.BUY, price=0.41, size=100, tif=TimeInForce.IOC)
    ex.submit(o, now=0.0)

    results = []
    t = 0.0
    while t < 500.0 and not results:
        t += 1.0
        results = ex.step(t, lambda tid: books.get(tid))

    fills = {f.price: f.size for f in results[0].fills}
    # only 45% of the 100 at the touch survived
    assert fills[0.40] == pytest.approx(45.0)
    assert fills[0.41] == pytest.approx(55.0)


def test_favourable_moves_are_not_suppressed():
    """Pessimism must come from the model, not from a thumb on the scale.

    If the book moves TOWARD us in flight we should get the better price.
    """
    ex = PaperExchange(LatencyModel(VPS_NEARBY, seed=6), FeeModel(rate=0.0))
    books = {"T": book([], [(0.40, 500)])}
    o = Order(token_id="T", side=Side.BUY, price=0.50, size=10,
              tif=TimeInForce.IOC, expected_price=0.40)
    ex.submit(o, now=0.0)
    books["T"] = book([], [(0.35, 500)])

    results = []
    t = 0.0
    while t < 500.0 and not results:
        t += 1.0
        results = ex.step(t, lambda tid: books.get(tid))

    assert results[0].fills[0].price == pytest.approx(0.35)
    assert results[0].fills[0].slippage == pytest.approx(-0.05)


def test_ack_arrives_strictly_after_the_match():
    ex = PaperExchange(LatencyModel(VPS_NEARBY, seed=7), FeeModel(rate=0.0))
    books = {"T": book([], [(0.40, 500)])}
    o = Order(token_id="T", side=Side.BUY, price=0.50, size=10, tif=TimeInForce.IOC)
    ex.submit(o, now=0.0)

    results = []
    t = 0.0
    while t < 500.0 and not results:
        t += 1.0
        results = ex.step(t, lambda tid: books.get(tid))

    fill = results[0].fills[0]
    assert fill.exchange_ts > fill.decision_ts, "cannot match before it is sent"
    assert results[0].ack_ts >= fill.exchange_ts, "cannot know before it happened"
    assert fill.round_trip_ms > 0


def test_fee_is_cheaper_in_the_tails():
    """The Polymarket-shaped fee rewards exactly the trades this bot wants."""
    f = FeeModel(rate=0.02)
    assert f.charge(0.95, 100) < f.charge(0.50, 100)
    assert f.charge(0.95, 100) == pytest.approx(f.charge(0.05, 100))


# ------------------------------------------------------------ delayed feed


def test_delayed_feed_hides_the_present():
    feed: DelayedFeed[float] = DelayedFeed(lambda: 100.0)
    feed.publish(1.0, now=0.0)
    assert feed.view(50.0) is None, "message has not arrived yet"
    assert feed.view(100.0) == 1.0
    feed.publish(2.0, now=200.0)
    assert feed.view(250.0) == 1.0, "newer message still in flight"
    assert feed.view(300.0) == 2.0


def test_delayed_feed_reports_staleness():
    feed: DelayedFeed[float] = DelayedFeed(lambda: 100.0)
    feed.publish(1.0, now=0.0)
    assert feed.view_age_ms(150.0) == pytest.approx(150.0)


def test_truth_differs_from_view():
    feed: DelayedFeed[float] = DelayedFeed(lambda: 100.0)
    feed.publish(1.0, now=0.0)
    feed.publish(2.0, now=10.0)
    assert feed.truth() == 2.0
    # first message visible at t=100, second not until t=110
    assert feed.view(105.0) == 1.0


# --------------------------------------------------------------- pricing


def test_information_lag_pulls_probability_toward_half():
    """Stale data must reduce confidence, and more so near expiry."""
    kw = dict(spot=100_005.0, strike=100_000.0, sigma_per_sec=9e-5)

    fresh = fair_value(seconds_to_close=10.0, info_lag_s=0.0, **kw)
    stale = fair_value(seconds_to_close=10.0, info_lag_s=0.25, **kw)
    assert 0.5 < stale.p_up < fresh.p_up

    # the same lag matters far more with 0.5s left than with 100s left
    near_fresh = fair_value(seconds_to_close=0.5, info_lag_s=0.0, **kw)
    near_stale = fair_value(seconds_to_close=0.5, info_lag_s=0.25, **kw)
    far_fresh = fair_value(seconds_to_close=100.0, info_lag_s=0.0, **kw)
    far_stale = fair_value(seconds_to_close=100.0, info_lag_s=0.25, **kw)
    assert (near_fresh.p_up - near_stale.p_up) > (far_fresh.p_up - far_stale.p_up)


def test_probability_is_monotonic_in_distance_from_strike():
    kw = dict(strike=100_000.0, sigma_per_sec=9e-5, seconds_to_close=60.0)
    ps = [fair_value(spot=s, **kw).p_up for s in (99_900, 99_950, 100_000, 100_050, 100_100)]
    assert ps == sorted(ps)
    assert ps[2] == pytest.approx(0.5, abs=1e-9), "at the strike it is a coin flip"


def test_fat_tails_are_less_confident_than_gaussian():
    from troll_poly_bot.pricing.digital import GaussianPricer, StudentTPricer

    g, t = GaussianPricer(), StudentTPricer(nu=4.0)
    assert t.cdf(3.0) < g.cdf(3.0), "t must be humbler in the tails"
    assert t.cdf(0.0) == pytest.approx(0.5)


def test_vol_floor_prevents_certainty_from_a_frozen_feed():
    v = EwmaVol()
    for i in range(200):
        v.update(100_000.0, i * 1000.0)   # frozen price
    assert v.sigma_per_sec >= v.floor_per_sec > 0
    fv = fair_value(100_100.0, 100_000.0, v.sigma_per_sec, 30.0)
    assert fv.p_up < 1.0, "a frozen feed must never imply certainty"


def test_vol_recovers_realistic_magnitude():
    """Feed a known GBM and check we recover roughly the right sigma."""
    import math
    import numpy as np

    rng = np.random.default_rng(0)
    true_sigma = 9e-5          # ~0.9 bps/s, BTC-ish
    v = EwmaVol(halflife_s=60.0)
    px = 100_000.0
    for i in range(1, 3000):
        px *= math.exp(true_sigma * rng.standard_normal())
        v.update(px, i * 1000.0)
    assert 0.5 * true_sigma < v.sigma_per_sec < 2.0 * true_sigma
