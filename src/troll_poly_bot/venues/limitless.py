"""Limitless Exchange (limitless.exchange, Base), CLOB up/down markets.

Verified against the live API on 2026-09-22 (fixtures in tests/fixtures/limitless):

    GET /markets/{slug}                slug  <asset>-up-or-down-15-min-<epoch>
                                       epoch is the window START, on a 900s
                                       boundary; the next two windows already
                                       exist with status CREATED, the running
                                       one is FUNDED, a settled one RESOLVED
      tokens.yes / tokens.no           CLOB token ids (77 / 75 digit decimals)
      expirationTimestamp              close, ms;  startAt: open, ISO
      priceOracleMetadata.ticker       BTC / ETH ...; chainlinkStreamUrl is the
                                       SAME Chainlink 60s TWAP Polymarket settles on
      metadata.externalSlug            the Polymarket slug of the same window
      settings.c / rebateRate          fee parameters (see FEE below)
      settings.takerDelayMs            500: taker orders are held half a second
      winningOutcomeIndex              0 = Up, 1 = Down, null until decided
                                       (observed ~7 min after close; RESOLVED
                                       status ~20 min after)
    GET /markets/{slug}/orderbook      YES book, BEST FIRST (bids descending,
                                       asks ascending), sizes in 6-decimal
                                       units, prices to 3 places
    GET /markets/active/slugs          every active market with `strikePrice`
                                       -- checked against the venue's own
                                       oracle-candles it IS the Price to Beat,
                                       i.e. the strike the bot otherwise has
                                       to reconstruct from its spot proxy
    404 {"message": "Market not found for slug: ..."}   = absent

No public websocket: the socket.io feed wants an X-API-Key at the handshake,
so paper mode polls the book. Two markets at one poll a second is nothing.

FEE
---
The venue does not publish the taker formula in its OpenAPI spec. The market
carries ``c: 3`` and a 30% maker rebate, which reads like Polymarket's
``rate * p(1-p)`` hump with ``c`` as the fee at the midpoint in percent:
3% at p = 0.5, i.e. ``0.12 * p(1-p)``. That is 1.7x Polymarket's 0.07 and is
treated as an ASSUMPTION -- ``TPB_LIMITLESS_FEE_RATE`` overrides it, and a
funded account's ``effectiveFeeBps`` on a real fill is how to pin it down.
Erring high only makes the bot trade less.

MINIMUM SIZE
------------
``settings.minSize`` is 50 (6-decimal) but a 0.997-share fill was observed
in the trade events, and the orderbook doc calls minSize the threshold for
*displayed* aggregation. The venue minimum is taken as 1 share until a live
rejection says otherwise.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import urllib.parse

from ..feeds.markets import MarketMeta
from ..signals.costs import FeeSchedule
from ..types import Market
from .base import GetJson, Venue

log = logging.getLogger(__name__)

API_BASE = "https://api.limitless.exchange"
SLUG_RE = re.compile(r"^(?P<asset>[a-z0-9]+)-up-or-down-(?P<dur>\d+)-min-(?P<epoch>\d+)$")
UNITS = 1_000_000.0            # collateral / share base units
DEFAULT_FEE_RATE = 0.12        # see FEE above


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _iso_ms(value) -> float | None:
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000.0
    except (TypeError, ValueError):
        return None


class LimitlessVenue(Venue):
    name = "limitless"
    book_transport = "poll"
    poll_s = 1.0
    publishes_strike = True

    def __init__(self, fee_rate: float = DEFAULT_FEE_RATE, api_base: str = API_BASE,
                 min_size_shares: float = 1.0) -> None:
        self.fee_rate = float(fee_rate)
        self.api_base = api_base.rstrip("/")
        self.min_size_shares = float(min_size_shares)
        self.taker_delay_ms = 500.0        # updated from each market's settings

    # ------------------------------------------------------------- slugs

    def slug(self, asset: str, epoch: int, duration_min: int) -> str:
        return f"{asset.lower()}-up-or-down-{int(duration_min)}-min-{epoch}"

    def parse_slug(self, slug: str) -> tuple[str, int, int] | None:
        m = SLUG_RE.match(slug or "")
        if not m:
            return None
        return m.group("asset").upper(), int(m.group("dur")), int(m.group("epoch"))

    # ------------------------------------------------------------- rows

    async def fetch_market(self, get_json: GetJson, slug: str) -> dict | None:
        row, _ = await get_json(f"{self.api_base}/markets/{urllib.parse.quote(slug)}")
        return row if isinstance(row, dict) and row.get("slug") else None

    def parse_market(self, row: dict) -> MarketMeta | None:
        parsed = self.parse_slug(str(row.get("slug") or ""))
        if parsed is None:
            return None
        slug_asset, duration_min, epoch = parsed
        oracle = row.get("priceOracleMetadata") or {}
        asset = str(oracle.get("ticker") or slug_asset).upper()
        tokens = row.get("tokens") or {}
        yes, no = str(tokens.get("yes") or ""), str(tokens.get("no") or "")
        if not yes or not no:
            log.warning("market %s has no yes/no tokens", row.get("slug"))
            return None
        open_ts = float(epoch) * 1000.0
        start = _iso_ms(row.get("startAt"))
        if start is not None and abs(start - open_ts) > 60_000.0:
            log.warning("market %s: slug epoch %s but startAt %s; skipping", row.get("slug"), open_ts, start)
            return None
        close_ts = _f(row.get("expirationTimestamp"))
        if close_ts <= 0.0:
            return None
        duration = duration_min * 60.0
        if abs((open_ts + duration * 1000.0) - close_ts) > 60_000.0:
            log.warning("market %s: %gs from slug but expirationTimestamp says %s; skipping",
                        row.get("slug"), duration, close_ts)
            return None
        settings = row.get("settings") or {}
        self.taker_delay_ms = _f(settings.get("takerDelayMs"), self.taker_delay_ms)
        fee = FeeSchedule(rate=self.fee_rate, exponent=1.0, taker_only=True,
                          maker_rebate=_f(settings.get("rebateRate"), 0.0),
                          enabled=bool((row.get("metadata") or {}).get("fee", True)))
        return MarketMeta(
            market=Market(
                condition_id=str(row.get("conditionId") or ""),
                asset=asset,
                yes_token_id=yes,
                no_token_id=no,
                strike=0.0,
                open_ts=open_ts,
                close_ts=close_ts,
                tick_size=0.001,                       # prices carry three places
                min_size=self.min_size_shares,
                slug=str(row["slug"]),
            ),
            resolution_source=str(oracle.get("chainlinkStreamUrl") or ""),
            accepting_orders=str(row.get("status") or "").upper() == "FUNDED",
            duration_s=duration,
            question=str(row.get("title") or ""),
            fee=fee,
            liquidity=0.0,                              # not published; the book is
            volume=_f(row.get("volumeFormatted")),
            twap_lookback_s=60.0,
        )

    # ------------------------------------------------------------- outcome

    @staticmethod
    def outcome_of(row: dict) -> bool | None:
        idx = row.get("winningOutcomeIndex")
        if idx in (0, 1):
            return idx == 0
        pay = row.get("payoutNumerators")
        if isinstance(pay, list) and len(pay) == 2 and pay != [0, 0]:
            return _f(pay[0]) > _f(pay[1])
        return None

    async def outcome(self, get_json: GetJson, slug: str) -> bool | None:
        row = await self.fetch_market(get_json, slug)
        return self.outcome_of(row) if row else None

    # ------------------------------------------------------------- books

    @staticmethod
    def snapshots_of(book: dict, meta: MarketMeta, now_ms: float) -> list[tuple[str, dict]]:
        """The YES book as served, and the NO book it implies: a bid for NO at
        q is an ask for YES at 1-q with the same size, and vice versa."""
        def levels(raw):
            out = []
            for lvl in raw or []:
                try:
                    p, s = float(lvl["price"]), float(lvl["size"]) / UNITS
                except (KeyError, TypeError, ValueError):
                    continue
                if s > 0.0 and 0.0 < p < 1.0:
                    out.append((p, s))
            return out
        bids, asks = levels(book.get("bids")), levels(book.get("asks"))
        ts = now_ms
        yes = {"bids": [{"price": p, "size": s} for p, s in bids],
               "asks": [{"price": p, "size": s} for p, s in asks], "timestamp": ts}
        no = {"bids": [{"price": round(1.0 - p, 6), "size": s} for p, s in asks],
              "asks": [{"price": round(1.0 - p, 6), "size": s} for p, s in bids], "timestamp": ts}
        return [(meta.market.yes_token_id, yes), (meta.market.no_token_id, no)]

    async def book_snapshots(self, get_json: GetJson, meta: MarketMeta) -> list[tuple[str, dict]]:
        import time
        book, _ = await get_json(f"{self.api_base}/markets/{urllib.parse.quote(meta.market.slug)}/orderbook")
        if not isinstance(book, dict):
            return []
        return self.snapshots_of(book, meta, time.time() * 1000.0)

    # ------------------------------------------------------------- strike

    def hints_of(self, rows) -> dict[str, float]:
        out: dict[str, float] = {}
        for x in rows or []:
            if not isinstance(x, dict):
                continue
            slug = str(x.get("slug") or "")
            if self.parse_slug(slug) is None:
                continue
            px = _f(x.get("strikePrice"))
            if px > 0.0:
                out[slug] = px
        return out

    async def strike_hints(self, get_json: GetJson) -> dict[str, float]:
        rows, _ = await get_json(f"{self.api_base}/markets/active/slugs")
        return self.hints_of(rows if isinstance(rows, list) else [])
