"""Polymarket: the original venue. Thin wrapper over feeds/markets and
feeds/polymarket so nothing about 5m/15m Polymarket operation moves."""
from __future__ import annotations

import json
import urllib.parse

from ..feeds.markets import MarketMeta, parse_market
from ..feeds.polymarket import GAMMA_BASE, duration_tag
from ..feeds.markets import SLUG_RE
from .base import GetJson, Venue


class PolymarketVenue(Venue):
    name = "polymarket"
    book_transport = "websocket"
    taker_delay_ms = 0.0
    publishes_strike = False

    def slug(self, asset: str, epoch: int, duration_min: int) -> str:
        return f"{asset.lower()}-updown-{duration_tag(duration_min)}-{epoch}"

    def parse_slug(self, slug: str) -> tuple[str, int, int] | None:
        m = SLUG_RE.match(slug or "")
        if not m:
            return None
        return m.group("asset").upper(), int(m.group("dur").rstrip("m")), int(m.group("epoch"))

    async def fetch_market(self, get_json: GetJson, slug: str) -> dict | None:
        rows, _ = await get_json(f"{GAMMA_BASE}/markets?slug={urllib.parse.quote(slug)}")
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    def parse_market(self, row: dict) -> MarketMeta | None:
        return parse_market(row)

    async def outcome(self, get_json: GetJson, slug: str) -> bool | None:
        row = await self.fetch_market(get_json, slug)
        if not row:
            return None
        try:
            outcomes = json.loads(row.get("outcomes") or "[]")
            prices = [float(p) for p in json.loads(row.get("outcomePrices") or "[]")]
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        if len(outcomes) != len(prices) or not prices:
            return None
        lowered = [str(o).strip().lower() for o in outcomes]
        if "up" not in lowered:
            return None
        p_up = prices[lowered.index("up")]
        if p_up > 0.9:
            return True
        if p_up < 0.1:
            return False
        return None
