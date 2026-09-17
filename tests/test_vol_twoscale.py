"""Tests for the two-scale volatility estimator.

These guard a bug that already cost money in a live paper run: a sigma
estimated from 1-second returns was ~2x too low for the 5-minute horizon being
priced, which made every probability too extreme and manufactured a 13-cent
"edge" against a liquid market.
"""
from __future__ import annotations

import math
import statistics

import numpy as np
import pytest

from troll_poly_bot.pricing.vol import EwmaVol, TwoScaleVol


def _feed(vol, prices, start=0.0, step_ms=1000.0):
    t = start
    for p in prices:
        vol.update(p, t)
        t += step_ms


def _iid_path(n, sigma, seed=0, start=100_000.0):
    rng = np.random.default_rng(seed)
    px, out = start, []
    for _ in range(n):
        px *= math.exp(sigma * rng.standard_normal())
        out.append(px)
    return out


def _trending_path(n, sigma, phi, seed=0, start=100_000.0):
    """AR(1) in the drift: positive phi makes returns autocorrelate, so
    multi-second variance grows faster than linearly in time."""
    rng = np.random.default_rng(seed)
    px, drift, out = start, 0.0, []
    for _ in range(n):
        drift = phi * drift + sigma * rng.standard_normal()
        px *= math.exp(drift)
        out.append(px)
    return out


def _batch_sigma(prices, step):
    sub = prices[::step]
    r = [math.log(sub[i] / sub[i - 1]) for i in range(1, len(sub))]
    return statistics.pstdev(r) / math.sqrt(step)


# ───────────────────────────── the core claim ─────────────────────────────


def test_one_second_sampling_understates_a_trending_series():
    """The premise. If this fails the estimator is solving a non-problem."""
    px = _trending_path(9000, 3e-5, phi=0.9, seed=1)
    assert _batch_sigma(px, 60) > _batch_sigma(px, 1) * 1.3


def test_two_scale_beats_plain_ewma_on_a_trending_series():
    px = _trending_path(9000, 3e-5, phi=0.9, seed=2)
    truth = _batch_sigma(px, 60)

    old, new = EwmaVol(), TwoScaleVol()
    _feed(old, px)
    _feed(new, px)

    old_err = abs(old.sigma_per_sec - truth) / truth
    new_err = abs(new.sigma_per_sec - truth) / truth
    assert new_err < old_err, f"two-scale {new_err:.2f} should beat ewma {old_err:.2f}"
    assert new.sigma_per_sec > old.sigma_per_sec


def test_errs_high_rather_than_low_when_it_errs():
    """Direction matters. Too-low sigma invents edges; too-high only forgoes
    them, so the estimator must not undershoot at realistic trend levels.

    phi=0.7 gives a variance ratio in the range actually measured on live BTC
    and ETH (1.35-3.6). Beyond that the max_ratio clamp binds on purpose --
    see test_clamp_limits_the_correction_on_a_pathological_series.
    """
    px = _trending_path(9000, 3e-5, phi=0.7, seed=3)
    truth = _batch_sigma(px, 60)
    v = TwoScaleVol()
    _feed(v, px)
    assert v.sigma_per_sec > truth * 0.85


def test_iid_series_needs_little_correction():
    """No trend means no horizon problem; the ratio should stay near 1."""
    px = _iid_path(9000, 9e-5, seed=4)
    v = TwoScaleVol()
    _feed(v, px)
    assert 0.7 < v.variance_ratio < 2.0
    assert v.sigma_per_sec == pytest.approx(_batch_sigma(px, 60), rel=0.6)


# ───────────────────────────────── guards ─────────────────────────────────


def test_clamp_limits_the_correction_on_a_pathological_series():
    """The clamp is a deliberate trade-off, and it has a cost worth naming: on
    an extremely trending series the correction is capped and sigma is left
    UNDER the truth. Capping a runaway ratio is still the safer failure --
    an unbounded ratio would let one weird regime blow sigma up and halt all
    trading -- but it means the clamp is not free."""
    px = _trending_path(9000, 3e-5, phi=0.99, seed=5)
    v = TwoScaleVol()
    _feed(v, px)
    assert v.sigma_per_sec <= v.fast.sigma_per_sec * v.max_ratio + 1e-12
    assert v.sigma_per_sec < _batch_sigma(px, 60)      # the documented cost


def test_prior_is_used_before_the_slow_estimator_is_warm():
    v = TwoScaleVol()
    _feed(v, _iid_path(200, 9e-5, seed=6))       # 200s: fast warm, slow is not
    assert v.sigma_per_sec == pytest.approx(
        v.fast.sigma_per_sec * v.default_ratio, rel=1e-9
    )


def test_floor_and_ceiling_are_respected():
    v = TwoScaleVol()
    _feed(v, [100_000.0] * 4000)                 # frozen feed
    assert v.sigma_per_sec >= v.fast.floor_per_sec
    assert v.sigma_per_sec <= v.fast.ceil_per_sec


def test_frozen_feed_never_implies_certainty():
    from troll_poly_bot.pricing.digital import fair_value
    v = TwoScaleVol()
    _feed(v, [100_000.0] * 4000)
    assert fair_value(100_100.0, 100_000.0, v.sigma_per_sec, 30.0).p_up < 1.0


def test_not_ready_before_the_fast_estimator_warms():
    v = TwoScaleVol()
    assert not v.ready
    _feed(v, _iid_path(60, 9e-5, seed=7))
    assert v.ready


def test_interface_matches_ewma_so_it_is_a_drop_in():
    v = TwoScaleVol()
    _feed(v, _iid_path(500, 9e-5, seed=8))
    for attr in ("sigma_per_sec", "sigma_over", "annualised", "ready", "update"):
        assert hasattr(v, attr)
    assert v.sigma_over(100.0) == pytest.approx(v.sigma_per_sec * 10.0)
    assert v.annualised() > 0


# ───────────────────── winner's-curse shrink ─────────────────────


def test_shrink_moves_fair_toward_the_market_and_shrinks_edge():
    """The correction for trading only when our own noisy estimate is high.

    Measured live: bought at 0.844 on a model claiming 0.923, realised 0.800.
    Buying at price p only pays if the true rate exceeds p, so an 80% hit rate
    on 84c favourites is negative EV.
    """
    from troll_poly_bot.config import StrategyConfig
    from troll_poly_bot.execution.latency import VPS_NEARBY
    from troll_poly_bot.execution.paper import FeeModel
    from troll_poly_bot.pricing.vol import EwmaVol
    from troll_poly_bot.strategy.taker import TakerStrategy
    from troll_poly_bot.types import BookLevel, Market, OrderBook

    def run(shrink):
        cfg = StrategyConfig(use_twap=False, min_edge=0.0, min_edge_sigmas=0.0,
                             market_shrink=shrink, min_fair_for_trade=0.15,
                             max_fair_for_trade=0.85)
        v = EwmaVol()
        for i in range(400):
            v.update(100_000.0 * (1 + 2e-5 * ((-1) ** i)), i * 1000.0)
        strat = TakerStrategy(cfg=cfg, latency=VPS_NEARBY,
                              fees=FeeModel(rate=0.0), vol={"BTC": v})
        now = 400_000.0
        m = Market(condition_id="c", asset="BTC", yes_token_id="UP",
                   no_token_id="DOWN", strike=100_000.0,
                   open_ts=now - 200_000.0, close_ts=now + 60_000.0)
        up = OrderBook("UP", bids=[BookLevel(0.83, 500)],
                       asks=[BookLevel(0.85, 500)], ts=now)
        dn = OrderBook("DOWN", bids=[BookLevel(0.14, 500)],
                       asks=[BookLevel(0.16, 500)], ts=now)
        return strat.evaluate(
            market=m, book_up=up, book_down=dn, spot=100_400.0,
            spot_age_ms=50.0, book_age_ms=50.0, now=now,
            gross_exposure=0.0, position_usdc=0.0, balance=100.0,
        )

    none_, half, full = run(0.0), run(0.5), run(1.0)
    assert none_ is not None and none_.edge > 0
    if half is not None:
        assert half.edge < none_.edge, "shrink must reduce measured edge"
        assert half.fair < half.raw_fair, "fair must move toward the market"
    # full deference to the market can never find an edge against its own mid
    assert full is None or full.edge <= 0.0
