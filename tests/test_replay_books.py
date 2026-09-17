"""The replay must rebuild a decided window as the one-sided book the venue served.

Probed live at 76 s out: the winner had ~100 bid levels to 0.99 and no asks,
the loser ~100 asks from 0.01 and no bids. A replay that rebuilt a two-sided
book from the recorded price alone would let a taker "buy" a winner nobody was
offering -- and that trade would then be blocked by the price band for the
wrong reason. With the touch recorded, it is blocked for the right one: there
is no ask.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import replay  # noqa: E402

DECIDED = {"up": 0.99, "down": 0.01, "ub": 0.99, "ua": None, "db": None, "da": 0.01, "left": 30.0}
OPEN = {"up": 0.485, "down": 0.515, "ub": 0.48, "ua": 0.49, "db": 0.51, "da": 0.52, "left": 200.0}
LEGACY = {"up": 0.485, "down": 0.515, "left": 200.0}      # recorded before the touch existed


def test_decided_winner_has_bids_and_no_asks():
    b = replay.book_from_point("UP", "up", DECIDED, 0.0)
    assert b.best_bid == pytest.approx(0.99)
    assert b.best_ask is None


def test_decided_loser_has_asks_and_no_bids():
    b = replay.book_from_point("DOWN", "down", DECIDED, 0.0)
    assert b.best_ask == pytest.approx(0.01)
    assert b.best_bid is None


def test_a_taker_cannot_buy_the_decided_winner():
    """The strategy skips a token whose best_ask is None. That gate, not the
    price band, is what stops a final-minute buy -- as at the venue."""
    assert replay.book_from_point("UP", "up", DECIDED, 0.0).best_ask is None


def test_open_window_rebuilds_both_sides_from_the_touch():
    b = replay.book_from_point("UP", "up", OPEN, 0.0)
    assert b.best_bid == pytest.approx(0.48)
    assert b.best_ask == pytest.approx(0.49)


def test_legacy_point_without_touch_falls_back_to_mid_reconstruction():
    b = replay.book_from_point("UP", "up", LEGACY, 0.0)
    assert b.best_bid == pytest.approx(0.48)
    assert b.best_ask == pytest.approx(0.49)
    assert b.mid == pytest.approx(0.485)


def test_recorded_touch_wins_over_the_price_when_both_present():
    """If the touch is there it is the truth; the price is derived from it."""
    p = dict(OPEN, up=0.60)                      # inconsistent price, real touch
    b = replay.book_from_point("UP", "up", p, 0.0)
    assert (b.best_bid, b.best_ask) == (pytest.approx(0.48), pytest.approx(0.49))
