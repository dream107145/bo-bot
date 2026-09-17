"""Exchange message parsers and the composite spot."""
from __future__ import annotations

import math

import pytest

from troll_poly_bot.feeds.spot import (
    CompositeSpot, FeedStatus, SpotQuote, parse_binance, parse_binance_trade, parse_bybit,
    parse_bybit_trades, parse_coinbase, parse_coinbase_trade,
)


def test_parse_binance_book_ticker_microprice():
    msg = {"stream": "btcusdt@bookTicker", "data": {"b": "100", "B": "3", "a": "102", "A": "1"}}
    q = parse_binance(msg, {"btcusdt": "BTC"}, 5.0)
    assert q is not None and q.asset == "BTC" and q.exchange == "binance"
    # weight each side by the size on the OPPOSITE side: (100*1 + 102*3)/4
    assert q.price == pytest.approx(101.5)
    assert parse_binance({"stream": "btcusdt@aggTrade", "data": {}}, {"btcusdt": "BTC"}, 5.0) is None


def test_parse_bybit_merges_one_sided_deltas():
    state = {}
    syms = {"HYPEUSDT": "HYPE"}
    snap = {"topic": "orderbook.1.HYPEUSDT", "ts": 1, "data": {"b": [["82.0", "5"]], "a": [["82.2", "5"]]}}
    q = parse_bybit(snap, syms, 1.0, state)
    assert q is not None and q.bid == 82.0 and q.ask == 82.2
    delta = {"topic": "orderbook.1.HYPEUSDT", "ts": 2, "data": {"b": [["82.1", "1"]], "a": []}}
    q2 = parse_bybit(delta, syms, 2.0, state)
    assert q2 is not None and q2.bid == 82.1 and q2.ask == 82.2
    assert parse_bybit({"topic": "tickers.HYPEUSDT"}, syms, 3.0, state) is None


def test_parse_coinbase_ticker():
    msg = {"type": "ticker", "product_id": "BTC-USD", "best_bid": "100", "best_ask": "101", "price": "100.5"}
    q = parse_coinbase(msg, {"BTC-USD": "BTC"}, 7.0)
    assert q is not None and q.price == pytest.approx(100.5)
    assert parse_coinbase({"type": "heartbeat"}, {"BTC-USD": "BTC"}, 7.0) is None


def _feed(c, ts, binance=None, bybit=None, coinbase=None):
    for ex, px in (("binance", binance), ("bybit", bybit), ("coinbase", coinbase)):
        if px is not None:
            c.update(SpotQuote("BTC", ex, px - 0.01, px + 0.01, px, ts_ms=ts))


def test_composite_uses_only_calibrated_sources_then_medians():
    c = CompositeSpot(max_age_ms=1000, basis_min_samples=3, warmup_ms=0.0)
    _feed(c, 0.0, binance=100.0, bybit=100.5, coinbase=99.0)
    v = c.view("BTC", 100.0)
    assert v is not None and v.n_sources == 1 and v.price == 100.0       # primary only, so far
    for k in range(1, 4):
        _feed(c, 100.0 * k, binance=100.0, bybit=100.5, coinbase=99.0)
    v = c.view("BTC", 400.0)
    assert v.n_sources == 3
    # every source is pulled to the primary's level, so no disagreement remains
    assert v.price == pytest.approx(100.0, abs=1e-6)
    assert v.deviation_bps == pytest.approx(0.0, abs=1e-6)
    assert v.basis_bps["bybit"] == pytest.approx(math.log(100.5 / 100.0) * 1e4, abs=0.01)
    assert v.basis_bps["coinbase"] == pytest.approx(math.log(99.0 / 100.0) * 1e4, abs=0.01)
    assert c.view("ETH", 500.0) is None
    assert c.view("BTC", 5000.0) is None


def test_composite_has_no_level_jump_when_a_source_drops_out():
    """The failure seen live: a raw median jumped by the 9 bps Binance/Coinbase
    basis whenever Coinbase aged out, and the vol estimator read it as 10x vol."""
    c = CompositeSpot(max_age_ms=1000, basis_min_samples=3, warmup_ms=0.0)
    for k in range(5):
        _feed(c, 100.0 * k, binance=100.0, bybit=100.02, coinbase=99.91)
    with_all = c.view("BTC", 450.0).price
    # coinbase and bybit go quiet; only binance stays fresh
    _feed(c, 2000.0, binance=100.0)
    only_primary = c.view("BTC", 2100.0).price
    # binance goes quiet; the others carry on at binance's level
    _feed(c, 4000.0, bybit=100.02, coinbase=99.91)
    others = c.view("BTC", 4100.0)
    assert others.n_sources == 2
    assert abs(with_all - only_primary) < 1e-6
    assert abs(others.price - only_primary) < 1e-6


def test_composite_real_disagreement_still_shows_in_deviation():
    c = CompositeSpot(max_age_ms=1000, basis_min_samples=3, warmup_ms=0.0)
    for k in range(5):
        _feed(c, 100.0 * k, binance=100.0, bybit=100.0, coinbase=100.0)
    _feed(c, 600.0, binance=100.0, bybit=100.0, coinbase=101.0)     # coinbase prints 1% away
    v = c.view("BTC", 650.0)
    assert v.deviation_bps > 50.0


def test_primary_change_recalibrates_the_earlier_source():
    """Coinbase quoted first live and kept a zero basis after Binance took over."""
    c = CompositeSpot(max_age_ms=1000, basis_min_samples=3, warmup_ms=0.0)
    _feed(c, 0.0, coinbase=99.0)                      # coinbase is briefly the primary
    assert c.primary["BTC"] == "coinbase"
    _feed(c, 10.0, binance=100.0)                     # binance outranks it
    assert c.primary["BTC"] == "binance"
    v = c.view("BTC", 20.0)
    assert v.n_sources == 1 and v.price == 100.0      # coinbase is no longer calibrated
    for k in range(1, 5):
        _feed(c, 100.0 * k, binance=100.0, coinbase=99.0)
    v = c.view("BTC", 450.0)
    assert v.n_sources == 2
    assert v.price == pytest.approx(100.0, abs=1e-6)


def test_warmup_hides_the_composite_at_first():
    c = CompositeSpot(max_age_ms=5000, warmup_ms=3000.0)
    _feed(c, 0.0, binance=100.0)
    assert c.view("BTC", 1000.0) is None
    assert c.view("BTC", 3500.0) is not None


def test_quotes_carry_top_of_book_sizes():
    q = parse_binance({"stream": "btcusdt@bookTicker", "data": {"b": "100", "B": "3", "a": "102", "A": "1"}},
                      {"btcusdt": "BTC"}, 5.0)
    assert q.bid_size == 3.0 and q.ask_size == 1.0
    state = {}
    q2 = parse_bybit({"topic": "orderbook.1.ETHUSDT", "ts": 1, "data": {"b": [["10", "7"]], "a": [["11", "2"]]}},
                     {"ETHUSDT": "ETH"}, 1.0, state)
    assert q2.bid_size == 7.0 and q2.ask_size == 2.0
    q3 = parse_coinbase({"type": "ticker", "product_id": "SOL-USD", "best_bid": "1", "best_ask": "2",
                         "best_bid_size": "4", "best_ask_size": "5", "price": "1.5"}, {"SOL-USD": "SOL"}, 1.0)
    assert q3.bid_size == 4.0 and q3.ask_size == 5.0


def test_trade_parsers_and_aggressor_side():
    # binance: m = buyer is maker -> the aggressor SOLD
    t = parse_binance_trade({"stream": "btcusdt@aggTrade", "data": {"p": "100", "q": "0.5", "m": True, "T": 7}},
                            {"btcusdt": "BTC"}, 9.0)
    assert t is not None and t.aggressor_buy is False and t.notional == 50.0 and t.event_ts_ms == 7.0
    t2 = parse_binance_trade({"stream": "btcusdt@aggTrade", "data": {"p": "100", "q": "0.5", "m": False}},
                             {"btcusdt": "BTC"}, 9.0)
    assert t2.aggressor_buy is True
    assert parse_binance_trade({"stream": "btcusdt@bookTicker", "data": {}}, {"btcusdt": "BTC"}, 9.0) is None
    ts = parse_bybit_trades({"topic": "publicTrade.HYPEUSDT", "data": [
        {"p": "80", "v": "2", "S": "Buy", "T": 1}, {"p": "80", "v": "1", "S": "Sell", "T": 2}, {"p": "x"}]},
        {"HYPEUSDT": "HYPE"}, 3.0)
    assert [x.aggressor_buy for x in ts] == [True, False]
    assert parse_bybit_trades({"topic": "orderbook.1.HYPEUSDT"}, {"HYPEUSDT": "HYPE"}, 3.0) == []
    cb = parse_coinbase_trade({"type": "ticker", "product_id": "BTC-USD", "price": "100", "last_size": "0.1",
                               "side": "sell"}, {"BTC-USD": "BTC"}, 4.0)
    assert cb is not None and cb.aggressor_buy is False and cb.notional == pytest.approx(10.0)
    assert parse_coinbase_trade({"type": "ticker", "product_id": "BTC-USD", "price": "100"},
                                {"BTC-USD": "BTC"}, 4.0) is None


def test_feed_status_report():
    st = FeedStatus("binance")
    st.connected, st.since_ms, st.last_msg_ms, st.messages, st.reconnects = True, 1000.0, 4000.0, 12, 1
    d = st.as_dict(5000.0)
    assert d["connected"] and d["state_age_s"] == 4.0 and d["last_msg_age_s"] == 1.0 and d["messages"] == 12
