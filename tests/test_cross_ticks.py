"""The fill-or-kill cross width is a parameter; the default is unchanged.

One tick guarantees a fill against a STATIC book. With ~1s of network delay
the book has often moved a tick by the time the order lands, and a 1-tick
cross is then FOK-rejected -- on real recorded windows at this machine's
delay, every order died that way. Crossing wider trades a worse price for a
fill; whether that is worth it is measured by scripts/replay.py, not assumed.
"""
from __future__ import annotations

import pytest

from troll_poly_bot.config import StrategyConfig
from troll_poly_bot.execution.latency import VPS_NEARBY
from troll_poly_bot.execution.paper import FeeModel
from troll_poly_bot.pricing.vol import EwmaVol
from troll_poly_bot.strategy.taker import TakerStrategy
from troll_poly_bot.types import BookLevel, Market, OrderBook


def _intent(cross):
    cfg = StrategyConfig(use_twap=False, min_edge=0.0, min_edge_sigmas=0.0,
                         market_shrink=0.0, min_fair_for_trade=0.15,
                         max_fair_for_trade=0.85, cross_ticks=cross)
    v = EwmaVol()
    for i in range(400):
        v.update(100_000.0 * (1 + 2e-5 * ((-1) ** i)), i * 1000.0)
    strat = TakerStrategy(cfg=cfg, latency=VPS_NEARBY, fees=FeeModel(rate=0.0),
                          vol={"BTC": v})
    now = 400_000.0
    m = Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DOWN",
               strike=100_000.0, open_ts=now - 200_000.0, close_ts=now + 60_000.0)
    up = OrderBook("UP", bids=[BookLevel(0.83, 500)], asks=[BookLevel(0.85, 500)], ts=now)
    dn = OrderBook("DOWN", bids=[BookLevel(0.14, 500)], asks=[BookLevel(0.16, 500)], ts=now)
    return strat.evaluate(market=m, book_up=up, book_down=dn, spot=100_400.0,
                          spot_age_ms=50.0, book_age_ms=50.0, now=now,
                          gross_exposure=0.0, position_usdc=0.0, balance=100.0)


def test_default_is_one_tick_so_existing_behaviour_is_unchanged():
    assert StrategyConfig().cross_ticks == 1


def test_default_crosses_exactly_one_tick():
    it = _intent(1)
    assert it is not None
    assert it.limit_price == pytest.approx(it.expected_price + 0.01)


def test_wider_cross_raises_the_limit_by_that_many_ticks():
    it = _intent(3)
    assert it is not None
    assert it.limit_price == pytest.approx(it.expected_price + 0.03)


def test_cross_never_exceeds_the_top_of_the_price_range():
    """The payout is capped at 1.00; a limit above 0.99 is a buy that cannot win."""
    it = _intent(50)
    assert it is not None
    assert it.limit_price <= 0.99 + 1e-9


def test_cross_width_does_not_change_whether_a_trade_happens():
    """Sizing and crossing decide HOW; the edge gates alone decide WHETHER."""
    assert (_intent(1) is None) == (_intent(4) is None)
