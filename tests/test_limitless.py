"""Limitless venue adapter against captured live responses (tests/fixtures/limitless,
2026-09-22). Every shape the adapter depends on is pinned here so a venue-side
change fails a test rather than a trading day."""
from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from troll_poly_bot.config import BotConfig
from troll_poly_bot.feeds.markets import StrikeTracker
from troll_poly_bot.live import LiveBot
from troll_poly_bot.market.discovery import AssetRegistry, FetchFailed
from troll_poly_bot.venues import make_venue
from troll_poly_bot.venues.limitless import UNITS, LimitlessVenue

FX = pathlib.Path(__file__).parent / "fixtures" / "limitless"


def fx(name: str):
    return json.loads((FX / name).read_text())


def run(coro):
    return asyncio.run(coro)


# ───────────────────────────── slugs ────────────────────────────────

def test_slug_round_trip():
    v = LimitlessVenue()
    assert v.slug("BTC", 1790107200, 15) == "btc-up-or-down-15-min-1790107200"
    assert v.parse_slug("eth-up-or-down-15-min-1790107200") == ("ETH", 15, 1790107200)
    assert v.parse_slug("btc-updown-15m-1790107200") is None          # not ours
    assert make_venue("polymarket").parse_slug("btc-updown-15m-1790107200") == ("BTC", 15, 1790107200)


def test_registry_probes_with_the_venue_slug():
    asked = []

    async def fetch(slug):
        asked.append(slug); return {"slug": slug}

    reg = AssetRegistry(candidates=("BTC",), durations=(15,), slug_fn=LimitlessVenue().slug)
    run(reg.refresh(fetch, 1790107579.0))
    assert asked == ["btc-up-or-down-15-min-1790107200"]


# ───────────────────────────── rows ─────────────────────────────────

def test_parse_current_market():
    v = LimitlessVenue()
    m = v.parse_market(fx("market_current.json"))
    assert m is not None
    assert m.asset == "BTC" and m.market.slug == "btc-up-or-down-15-min-1790107200"
    assert m.market.open_ts == 1790107200_000.0 and m.market.close_ts == 1790108100_000.0
    assert m.duration_s == 900.0
    assert m.market.yes_token_id != m.market.no_token_id and len(m.market.yes_token_id) > 60
    assert m.market.tick_size == 0.001 and m.market.min_size == 1.0
    assert m.accepting_orders is True                                    # FUNDED
    assert m.fee.rate == pytest.approx(0.12) and m.fee.maker_rebate == pytest.approx(0.3)
    assert "chain.link" in m.resolution_source and m.twap_lookback_s == 60.0
    assert v.taker_delay_ms == 500.0


def test_next_window_is_parsed_but_not_yet_accepting():
    m = LimitlessVenue().parse_market(fx("market_next.json"))
    assert m is not None and m.accepting_orders is False                # CREATED
    assert m.market.open_ts == 1790108100_000.0


def test_fee_rate_is_configurable():
    m = LimitlessVenue(fee_rate=0.07).parse_market(fx("market_current.json"))
    assert m.fee.rate == pytest.approx(0.07)


def test_row_with_mismatched_expiry_is_refused():
    row = fx("market_current.json"); row["expirationTimestamp"] = row["expirationTimestamp"] + 600_000
    assert LimitlessVenue().parse_market(row) is None


# ───────────────────────────── outcome ──────────────────────────────

def test_outcome_mapping():
    v = LimitlessVenue()
    assert v.outcome_of(fx("market_resolved.json")) is False           # winningOutcomeIndex 1 = Down
    assert v.outcome_of(fx("market_current.json")) is None
    assert v.outcome_of({"winningOutcomeIndex": 0}) is True
    assert v.outcome_of({"winningOutcomeIndex": None, "payoutNumerators": [1, 0]}) is True
    assert v.outcome_of({"winningOutcomeIndex": None, "payoutNumerators": [0, 0]}) is None


def test_fetch_market_absent_vs_failed():
    v = LimitlessVenue()

    async def absent(url): return None, 30.0
    async def failed(url): raise FetchFailed("dns")

    assert run(v.fetch_market(absent, "btc-up-or-down-15-min-1")) is None
    with pytest.raises(FetchFailed):
        run(v.fetch_market(failed, "btc-up-or-down-15-min-1"))


# ───────────────────────────── books ────────────────────────────────

def test_orderbook_to_yes_and_mirrored_no_snapshots():
    v = LimitlessVenue(); meta = v.parse_market(fx("market_current.json"))
    snaps = dict(v.snapshots_of(fx("orderbook.json"), meta, 1.0))
    yes, no = snaps[meta.market.yes_token_id], snaps[meta.market.no_token_id]
    raw = fx("orderbook.json")
    best_bid, best_ask = raw["bids"][0], raw["asks"][0]
    assert yes["bids"][0]["price"] == best_bid["price"] and yes["bids"][0]["size"] == best_bid["size"] / UNITS
    assert yes["asks"][0]["price"] == best_ask["price"]
    # NO bid at 1 - YES ask, same size
    assert no["bids"][0]["price"] == pytest.approx(1.0 - best_ask["price"]) and no["bids"][0]["size"] == best_ask["size"] / UNITS
    assert no["asks"][0]["price"] == pytest.approx(1.0 - best_bid["price"])
    assert all(0.0 < l["price"] < 1.0 for side in (yes, no) for k in ("bids", "asks") for l in side[k])


def test_snapshots_feed_livebook_best_first():
    from troll_poly_bot.live import LiveBook
    v = LimitlessVenue(); meta = v.parse_market(fx("market_current.json"))
    (tid, yes), _ = v.snapshots_of(fx("orderbook.json"), meta, 5.0)
    lb = LiveBook(tid); lb.apply_snapshot(yes); ob = lb.to_order_book()
    assert ob.bids[0].price == max(l.price for l in ob.bids)
    assert ob.asks[0].price == min(l.price for l in ob.asks)
    assert ob.ts == 5.0


# ───────────────────────────── strike ───────────────────────────────

def test_strike_hints_from_active_slugs():
    v = LimitlessVenue()
    hints = v.hints_of(fx("active_slugs_15m.json"))
    assert hints["btc-up-or-down-15-min-1790107200"] == pytest.approx(86210.31)
    assert all(v.parse_slug(s) for s in hints)


def test_tracker_takes_the_venue_strike_and_corrects_the_proxy():
    v = LimitlessVenue(); meta = v.parse_market(fx("market_current.json"))
    t = StrikeTracker(); t.register(meta)
    # before the open: nothing, stays pending
    assert t.strike_from_venue(meta.market.slug, 86210.31, meta.market.open_ts - 1) == (None, None)
    # after the open, no proxy yet: struck by the venue
    m, before = t.strike_from_venue(meta.market.slug, 86210.31, meta.market.open_ts + 1)
    assert m is meta and before is None and meta.market.strike == 86210.31
    # the proxy had it slightly wrong: corrected, previous value reported
    meta.market.strike = 86200.0
    m, before = t.strike_from_venue(meta.market.slug, 86210.31, meta.market.open_ts + 2)
    assert m is meta and before == 86200.0 and meta.market.strike == 86210.31
    assert t.strike_from_venue(meta.market.slug, 86210.31, meta.market.open_ts + 3) == (None, None)


# ───────────────────────────── config / bot ─────────────────────────

def test_env_selects_the_venue(monkeypatch):
    monkeypatch.setenv("TPB_MODE", "paper"); monkeypatch.setenv("TPB_VENUE", "limitless")
    monkeypatch.setenv("TPB_LIMITLESS_FEE_RATE", "0.10")
    cfg = BotConfig.from_env()
    assert cfg.feeds.venue == "limitless" and cfg.feeds.limitless_fee_rate == 0.10


def test_bot_wires_the_venue_and_charges_the_taker_delay(monkeypatch, tmp_path):
    cfg = BotConfig(); cfg.feeds.venue = "limitless"; cfg.apply_durations((15,))
    margin = cfg.engine.latency_safety_margin_ms
    bot = LiveBot(assets=("BTC",), cfg=cfg, state_path=str(tmp_path / "s.json"),
                  trade_log=str(tmp_path / "t.jsonl"), durations=(15,))
    assert bot.venue.name == "limitless" and bot.venue.book_transport == "poll"
    assert bot.cfg.engine.latency_safety_margin_ms == margin + 500.0
    assert bot.registry.slug_fn("BTC", 1790107200, 15) == "btc-up-or-down-15-min-1790107200"
    assert bot.snapshot()["venue"] == "limitless"


def test_polymarket_stays_default(tmp_path):
    bot = LiveBot(assets=("BTC",), state_path=str(tmp_path / "s.json"), trade_log=str(tmp_path / "t.jsonl"))
    assert bot.venue.name == "polymarket" and bot.venue.book_transport == "websocket"
    assert bot.venue.slug("BTC", 1790107200, 5) == "btc-updown-5m-1790107200"


def test_venue_strike_recovers_a_window_our_feed_missed():
    """Restart mid-window on Limitless: the proxy cannot strike (no price at the
    open), but the venue's Price to Beat is known, so the window is tradeable."""
    v = LimitlessVenue(); meta = v.parse_market(fx("market_current.json"))
    t = StrikeTracker(); t.register(meta)
    assert t.poll(meta.market.open_ts + 60_000) == []                   # no price ever seen: abandoned
    assert t.abandoned_count == 1 and t.tradeable(meta.market.open_ts + 60_000) == []
    m, before = t.strike_from_venue(meta.market.slug, 86210.31, meta.market.open_ts + 61_000)
    assert m is meta and before is None and meta.market.strike == 86210.31
    assert t.tradeable(meta.market.open_ts + 61_000) == [meta]
    # but never after the close
    v2 = LimitlessVenue(); meta2 = v2.parse_market(fx("market_next.json")); t2 = StrikeTracker(); t2.register(meta2)
    t2.poll(meta2.market.open_ts + 1)
    assert t2.strike_from_venue(meta2.market.slug, 1.0, meta2.market.close_ts + 1) == (None, None)
