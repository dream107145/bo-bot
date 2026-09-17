"""Dynamic asset discovery and the richer market parse."""
from __future__ import annotations

import asyncio
import json

from troll_poly_bot.feeds.markets import parse_market
from troll_poly_bot.market.discovery import AssetRegistry, FetchFailed, probe_assets, slug_for_epoch


def _row(asset: str, epoch: int) -> dict:
    return {
        "slug": f"{asset.lower()}-updown-5m-{epoch}",
        "endDate": "2026-09-17T14:00:00Z",
        "clobTokenIds": json.dumps(["111", "222"]),
        "outcomes": json.dumps(["Up", "Down"]),
        "conditionId": "0xabc",
        "question": f"{asset} Up or Down - 9:55AM-10:00AM ET",
        "feeSchedule": {"rate": 0.07, "exponent": 1, "takerOnly": True, "rebateRate": 0.2},
        "feesEnabled": True,
        "liquidityNum": 1234.5, "volumeNum": 118,
        "cryptoMarketConfig": {"asset": asset.lower(), "duration": "5m", "twapLookbackSeconds": 60},
        "resolutionSource": f"https://data.chain.link/streams/{asset.lower()}-usd-twap-60s-streams",
    }


EPOCH = 1789653300      # closes 14:00:00Z


def test_probe_keeps_only_listed_assets():
    listed = {"BTC", "HYPE", "BNB"}

    async def fetch(slug):
        asset = slug.split("-")[0].upper()
        return _row(asset, EPOCH) if asset in listed else None

    found, failed = asyncio.run(probe_assets(("BTC", "ETH", "HYPE", "BNB", "ADA"), EPOCH, fetch))
    assert set(found) == listed and failed == set()
    assert found["HYPE"]["slug"] == slug_for_epoch("HYPE", EPOCH)


def test_registry_refresh_pin_and_reprobe():
    async def fetch(slug):
        asset = slug.split("-")[0].upper()
        return _row(asset, EPOCH) if asset in {"BTC", "ETH", "DOGE"} else None

    reg = AssetRegistry(candidates=("BTC", "ETH", "DOGE", "ADA"), reprobe_s=600)
    assert reg.due(EPOCH + 1)
    assets = asyncio.run(reg.refresh(fetch, EPOCH + 1))
    assert assets == ("BTC", "DOGE", "ETH")
    assert not reg.due(EPOCH + 100) and reg.due(EPOCH + 700)
    pinned = AssetRegistry(candidates=("BTC", "ETH", "DOGE"), pinned=("ETH",))
    assert asyncio.run(pinned.refresh(fetch, EPOCH + 1)) == ("ETH",)


def test_parse_market_reads_fee_question_and_any_asset():
    meta = parse_market(_row("HYPE", EPOCH))
    assert meta is not None
    assert meta.market.asset == "HYPE"
    assert meta.fee.rate == 0.07 and meta.fee.taker_only
    assert meta.question.startswith("HYPE")
    assert meta.liquidity == 1234.5 and meta.volume == 118
    assert meta.twap_lookback_s == 60.0
    assert meta.market.yes_token_id == "111"


def test_failed_probe_keeps_the_asset_and_retries_soon():
    """The DNS-outage bug: a probe that cannot be answered is not a delisting."""
    reachable = {"BTC", "ETH", "SOL"}

    async def fetch_ok(slug):
        return _row(slug.split("-")[0].upper(), EPOCH)

    async def fetch_flaky(slug):
        asset = slug.split("-")[0].upper()
        if asset in reachable:
            return _row(asset, EPOCH)
        raise FetchFailed("gaierror")

    reg = AssetRegistry(candidates=("BTC", "ETH", "SOL", "XRP", "DOGE"), reprobe_s=1800, retry_s=60)
    assert asyncio.run(reg.refresh(fetch_ok, EPOCH)) == ("BTC", "DOGE", "ETH", "SOL", "XRP")
    assert asyncio.run(reg.refresh(fetch_flaky, EPOCH + 1)) == ("BTC", "DOGE", "ETH", "SOL", "XRP")
    assert reg.unresolved == {"XRP", "DOGE"} and reg.probe_failures == 1
    assert reg.due(EPOCH + 1 + 61) and not reg.due(EPOCH + 1 + 30)      # retry cadence while unresolved

    async def fetch_all_down(slug):
        raise FetchFailed("gaierror")

    assert asyncio.run(reg.refresh(fetch_all_down, EPOCH + 100)) == ("BTC", "DOGE", "ETH", "SOL", "XRP")

    async def fetch_delisted(slug):
        asset = slug.split("-")[0].upper()
        return _row(asset, EPOCH) if asset != "DOGE" else None        # venue positively says: gone

    assert asyncio.run(reg.refresh(fetch_delisted, EPOCH + 200)) == ("BTC", "ETH", "SOL", "XRP")
    assert reg.unresolved == set()
