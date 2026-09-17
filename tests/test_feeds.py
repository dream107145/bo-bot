"""Tests for market discovery and book parsing.

The book-ordering test is the important one: Polymarket serves levels worst
first, and misreading that does not raise -- it just makes every market look
mispriced in the same direction.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from troll_poly_bot.feeds.markets import (
    StrikeTracker,
    parse_market,
    parse_markets,
)
from troll_poly_bot.feeds.polymarket import parse_book, slug_for, window_epoch

# Shape copied from a live `/book` response: bids ascending, asks descending.
LIVE_BOOK = {
    "market": "0xabc",
    "asset_id": "115358912384014",
    "timestamp": "1789485400000",
    "bids": [
        {"price": "0.01", "size": "9836.63"},
        {"price": "0.02", "size": "1873.64"},
        {"price": "0.73", "size": "474"},
        {"price": "0.74", "size": "668"},
    ],
    "asks": [
        {"price": "0.99", "size": "9130.99"},
        {"price": "0.98", "size": "1564.29"},
        {"price": "0.76", "size": "157"},
        {"price": "0.75", "size": "96"},
    ],
    "tick_size": "0.01",
    "min_order_size": "5",
}

# Shape copied from a live Gamma row.
LIVE_MARKET = {
    "slug": "btc-updown-5m-1789485300",
    "question": "Bitcoin Up or Down - September 15, 11:15AM-11:20AM ET",
    "endDate": "2026-09-15T15:20:00Z",
    "conditionId": "0x33f2f9edc51b2f6f",
    "clobTokenIds": json.dumps(["1153589123840142", "2550766234582881"]),
    "outcomes": json.dumps(["Up", "Down"]),
    "orderPriceMinTickSize": "0.01",
    "orderMinSize": "5",
    "negRisk": False,
    "acceptingOrders": True,
    "resolutionSource": "https://data.chain.link/streams/btc-usd",
}


# ------------------------------------------------------------------- book


def test_book_is_sorted_best_first_from_worst_first_payload():
    book = parse_book(LIVE_BOOK)
    assert book.best_bid == pytest.approx(0.74)
    assert book.best_ask == pytest.approx(0.75)
    # reading asks[0] naively would give 0.99 and make this look like free money
    assert book.asks[0].price == pytest.approx(0.75)
    assert book.bids[0].price == pytest.approx(0.74)
    assert [l.price for l in book.bids] == sorted(
        (l.price for l in book.bids), reverse=True
    )
    assert [l.price for l in book.asks] == sorted(l.price for l in book.asks)


def test_book_parses_string_prices_and_sizes():
    book = parse_book(LIVE_BOOK)
    assert isinstance(book.best_bid, float)
    assert book.bids[0].size == pytest.approx(668.0)


def test_book_drops_zero_and_malformed_levels():
    payload = dict(LIVE_BOOK)
    payload["bids"] = [
        {"price": "0.50", "size": "0"},
        {"price": "0.60", "size": "10"},
        {"price": "bogus", "size": "10"},
        {"size": "10"},
    ]
    book = parse_book(payload)
    assert [l.price for l in book.bids] == [pytest.approx(0.60)]


def test_empty_book_has_no_touch():
    book = parse_book({"asset_id": "x", "bids": [], "asks": []})
    assert book.best_bid is None and book.best_ask is None and book.mid is None


# ---------------------------------------------------------------- discovery


def test_slug_is_deterministic_from_the_window_start():
    # 2026-09-15T15:17:34Z falls in the window that STARTS at 15:15:00
    now = dt.datetime(2026, 9, 15, 15, 17, 34, tzinfo=dt.timezone.utc).timestamp()
    assert slug_for("BTC", now) == "btc-updown-5m-1789485300"
    assert window_epoch(now) == 1789485300
    assert window_epoch(now, 1) == 1789485600      # next window
    assert window_epoch(now, -1) == 1789485000     # previous


def test_market_parses_and_maps_outcomes_by_label():
    meta = parse_market(LIVE_MARKET)
    assert meta is not None
    m = meta.market
    assert m.asset == "BTC"
    assert m.yes_token_id == "1153589123840142"    # the "Up" token
    assert m.no_token_id == "2550766234582881"
    assert m.tick_size == pytest.approx(0.01)
    assert m.min_size == pytest.approx(5)
    assert "chain.link" in meta.resolution_source
    assert m.close_ts - m.open_ts == pytest.approx(300_000.0)


def test_outcome_order_is_not_assumed():
    """If the venue ever emits Down first, positions must not silently invert."""
    row = dict(LIVE_MARKET)
    row["outcomes"] = json.dumps(["Down", "Up"])
    meta = parse_market(row)
    assert meta is not None
    assert meta.market.yes_token_id == "2550766234582881"
    assert meta.market.no_token_id == "1153589123840142"


def test_non_updown_markets_are_ignored():
    assert parse_market({"slug": "will-x-happen-2026"}) is None
    assert parse_market({"slug": ""}) is None


def test_slug_and_end_date_disagreement_is_rejected():
    """A mismatch means one assumption is wrong; every horizon would be wrong."""
    row = dict(LIVE_MARKET)
    row["endDate"] = "2026-09-15T16:20:00Z"        # an hour later than the slug implies
    assert parse_market(row) is None


def test_parse_markets_survives_a_bad_row():
    rows = [LIVE_MARKET, {"slug": "btc-updown-5m-1789485300", "outcomes": "{{{"}, {}]
    assert len(parse_markets(rows)) == 1


# ------------------------------------------------------------ strike tracker


def _meta(epoch_ms: float):
    row = dict(LIVE_MARKET)
    meta = parse_market(row)
    assert meta is not None
    meta.market.open_ts = epoch_ms
    meta.market.close_ts = epoch_ms + 300_000.0
    return meta


def test_strike_is_taken_at_the_window_open():
    tr = StrikeTracker()
    meta = _meta(10_000.0)
    tr.register(meta)
    tr.on_price("BTC", 74_000.0, 9_500.0)
    assert tr.poll(9_999.0) == []                  # window has not opened
    struck = tr.poll(10_100.0)
    assert len(struck) == 1
    assert struck[0].market.strike == pytest.approx(74_000.0)
    assert tr.tradeable(11_000.0)


def test_stale_oracle_price_refuses_to_guess_a_strike():
    """Better to skip the market than to bias every z for five minutes."""
    tr = StrikeTracker(tolerance_ms=2000.0)
    meta = _meta(10_000.0)
    tr.register(meta)
    tr.on_price("BTC", 74_000.0, 1_000.0)          # 9s before the open
    assert tr.poll(10_100.0) == []
    assert tr.abandoned_count == 1
    assert not tr.tradeable(11_000.0)


def test_market_with_no_oracle_price_is_abandoned():
    tr = StrikeTracker()
    tr.register(_meta(10_000.0))
    assert tr.poll(10_100.0) == []
    assert tr.abandoned_count == 1


def test_registering_twice_is_a_no_op():
    tr = StrikeTracker()
    meta = _meta(10_000.0)
    tr.register(meta)
    tr.register(_meta(10_000.0))
    tr.on_price("BTC", 74_000.0, 10_000.0)
    assert len(tr.poll(10_100.0)) == 1


def test_closed_markets_are_dropped():
    tr = StrikeTracker()
    tr.register(_meta(10_000.0))
    tr.on_price("BTC", 74_000.0, 10_000.0)
    tr.poll(10_100.0)
    tr.drop_closed(now=10_000.0 + 300_000.0 + 200_000.0)
    assert not tr.tradeable(10_000.0 + 300_000.0 + 200_000.0)
