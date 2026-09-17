"""Tests for target-win sizing.

The floor exists to make a winning trade net a fixed dollar amount. What it
must NOT do is create edge or bypass the risk caps -- those are the two ways
a sizing change quietly becomes an account-emptying change.
"""
from __future__ import annotations

import pytest

from troll_poly_bot.config import StrategyConfig
from troll_poly_bot.execution.latency import VPS_NEARBY
from troll_poly_bot.execution.paper import FeeModel
from troll_poly_bot.pricing.vol import EwmaVol
from troll_poly_bot.strategy.taker import TakerStrategy
from troll_poly_bot.types import BookLevel, OrderBook


def _strategy(**overrides):
    # generous caps by default so the target is what binds; a test that wants
    # to prove a cap still clamps passes its own tighter value
    kw = dict(
        use_twap=False, min_edge=0.0, min_edge_sigmas=0.0, market_shrink=0.0,
        min_fair_for_trade=0.15, max_fair_for_trade=0.85,
        max_position_usdc=1000.0, max_gross_usdc=1000.0,
        max_shares_per_order=10_000.0, min_order_usdc=0.0,
    )
    kw.update(overrides)
    cfg = StrategyConfig(**kw)
    return TakerStrategy(cfg=cfg, latency=VPS_NEARBY, fees=FeeModel(rate=0.0),
                         vol={"BTC": EwmaVol()})


def _deep_book(price):
    return OrderBook("T", bids=[BookLevel(price - 0.01, 100_000)],
                     asks=[BookLevel(price, 100_000)], ts=0.0)


def _size(strat, p, price, balance=100.0, position=0.0, gross=0.0):
    return strat._size(p, price, p - price, _deep_book(price), balance, gross, position)


# ─────────────────────────── the core contract ────────────────────────────


@pytest.mark.parametrize("price", [0.60, 0.80, 0.85, 0.90])
def test_a_win_nets_the_target(price):
    """shares * (1 - price) == target, across the price band.

    Uses a 2c edge on purpose. That is the regime this floor exists for:
    Kelly on a small edge stakes a dollar or two and a win nets pennies, so
    the floor is what binds. A huge edge would make Kelly the larger of the
    two, and the floor correctly does NOT lower it -- see the next test.
    """
    strat = _strategy(target_win_usdc=1.0)
    shares = _size(strat, p=price + 0.02, price=price)
    assert shares * (1.0 - price) == pytest.approx(1.0, abs=0.02)


def test_floor_only_raises_never_lowers_kelly():
    """If Kelly already wants more than the target needs, Kelly wins."""
    # huge edge, huge bankroll -> Kelly stake dwarfs a $1 target
    strat = _strategy(target_win_usdc=1.0, kelly_fraction=1.0)
    with_floor = _size(strat, p=0.99, price=0.50, balance=100_000.0)
    strat_off = _strategy(target_win_usdc=0.0, kelly_fraction=1.0)
    kelly_only = _size(strat_off, p=0.99, price=0.50, balance=100_000.0)
    assert with_floor == pytest.approx(kelly_only)


def test_zero_target_is_pure_kelly():
    strat = _strategy(target_win_usdc=0.0)
    kelly = _size(strat, p=0.90, price=0.80)
    # Kelly on a 10c edge at 0.80 with 25% fraction: stake = 100*0.25*0.5 = $12.5
    # _size rounds shares to 2dp, so 15.625 -> 15.62; compare at that precision.
    assert kelly == pytest.approx(12.5 / 0.80, abs=0.01)


# ───────────────────── it must not create edge or trades ──────────────────


def test_no_edge_means_no_trade_regardless_of_target():
    """Sizing can only change how much; whether is still the edge's call."""
    strat = _strategy(target_win_usdc=1.0)
    assert _size(strat, p=0.79, price=0.80) == 0.0      # fair below ask


def test_the_loss_scales_with_the_win():
    """The floor rescales the whole bet. A $1 target at 0.90 risks $9, not
    the $5 the old cap allowed. This is the trade-off, stated as a test.
    Small edge again, so the floor (not Kelly) is what set the size."""
    strat = _strategy(target_win_usdc=1.0)
    shares = _size(strat, p=0.92, price=0.90)
    assert shares * 0.90 == pytest.approx(9.0, abs=0.05)


# ─────────────────────── every cap still clamps it ────────────────────────


def test_position_cap_still_binds():
    """At 0.95 a $1 win needs $19 of stake; a $15 cap must win."""
    strat = _strategy(target_win_usdc=1.0, max_position_usdc=15.0)
    shares = _size(strat, p=0.99, price=0.95)
    assert shares * 0.95 <= 15.0 + 1e-6
    assert shares * (1.0 - 0.95) < 1.0                 # target NOT reached, honestly


def test_gross_cap_still_binds():
    strat = _strategy(target_win_usdc=1.0, max_gross_usdc=45.0)
    shares = _size(strat, p=0.99, price=0.90, gross=40.0)
    assert shares * 0.90 <= 5.0 + 1e-6


def test_balance_still_binds():
    strat = _strategy(target_win_usdc=5.0)
    shares = _size(strat, p=0.99, price=0.90, balance=10.0)
    assert shares * 0.90 <= 10.0 + 1e-6


def test_existing_position_reduces_headroom():
    strat = _strategy(target_win_usdc=1.0, max_position_usdc=15.0)
    fresh = _size(strat, p=0.99, price=0.95, position=0.0)
    topped_up = _size(strat, p=0.99, price=0.95, position=12.0)
    assert topped_up < fresh
    assert topped_up * 0.95 <= 3.0 + 1e-6


def test_liquidity_still_binds():
    """Never ask for more than is actually resting."""
    strat = _strategy(target_win_usdc=1.0)
    thin = OrderBook("T", bids=[BookLevel(0.89, 1)], asks=[BookLevel(0.90, 2.0)], ts=0.0)
    shares = strat._size(0.99, 0.90, 0.09, thin, 100.0, 0.0, 0.0)
    assert shares <= 2.0
