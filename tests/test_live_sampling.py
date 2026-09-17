"""The archive sampler must keep recording through a decided window.

Probed live on the raw venue book, 76 seconds before close:

    UP   (loser)   bids=0             asks=99+  best 0.01
    DOWN (winner)  bids=99+ best 0.99   asks=0

Once a window is decided the books go one-sided in a complementary way --
nobody will sell the winner, nobody will buy the loser -- so `mid` is None
for BOTH tokens. The first fix recovered one token from the other; it had
nothing to recover from. The side that IS quoted is the marketable price.
"""
from __future__ import annotations

import pytest

from troll_poly_bot.live import LiveBot
from troll_poly_bot.types import BookLevel, OrderBook

price = LiveBot._token_price
recover = LiveBot._recover_mids


def _book(bids=(), asks=()):
    return OrderBook("T", bids=[BookLevel(p, s) for p, s in bids],
                     asks=[BookLevel(p, s) for p, s in asks], ts=0.0)


# ───────────────────────── the premise the fix rests on ─────────────────────


def test_one_sided_book_has_no_mid():
    """If this ever fails, `mid` changed and the fallback is unnecessary."""
    assert _book(asks=[(0.01, 100)]).mid is None
    assert _book(bids=[(0.99, 100)]).mid is None


# ───────────────────────────── _token_price ────────────────────────────────


def test_two_sided_book_uses_the_mid():
    assert price(_book(bids=[(0.48, 10)], asks=[(0.49, 10)])) == pytest.approx(0.485)


def test_asks_only_uses_best_ask():
    """The loser near expiry: what you would pay for it."""
    assert price(_book(asks=[(0.01, 100), (0.02, 50)])) == pytest.approx(0.01)


def test_bids_only_uses_best_bid():
    """The winner near expiry: what you would get for it."""
    assert price(_book(bids=[(0.99, 100), (0.98, 50)])) == pytest.approx(0.99)


def test_empty_book_is_none():
    assert price(_book()) is None
    assert price(None) is None


def test_the_live_probe_case_prices_both_tokens_and_sums_to_one():
    """Exactly what the venue served at 76s out. Sampling must not stop here."""
    up = _book(asks=[(0.01, 104)])                 # loser: asks only
    dn = _book(bids=[(0.99, 105)])                 # winner: bids only
    u, d = recover(price(up), price(dn))
    assert u == pytest.approx(0.01)
    assert d == pytest.approx(0.99)
    assert u + d == pytest.approx(1.0)


# ───────────────────────────── _recover_mids ───────────────────────────────


def test_missing_down_is_recovered_from_up():
    up, down = recover(0.985, None)
    assert up == 0.985
    assert down == pytest.approx(0.015)


def test_missing_up_is_recovered_from_down():
    up, down = recover(None, 0.015)
    assert up == pytest.approx(0.985)
    assert down == 0.015


def test_both_present_pass_through_unchanged():
    assert recover(0.6, 0.4) == (0.6, 0.4)


def test_both_missing_stays_missing():
    """Nothing to recover from; the caller must skip the sample, not invent one."""
    assert recover(None, None) == (None, None)


def test_recovered_pair_still_sums_to_one():
    for known in (0.01, 0.25, 0.5, 0.985):
        up, down = recover(known, None)
        assert up + down == pytest.approx(1.0)
        up, down = recover(None, known)
        assert up + down == pytest.approx(1.0)
