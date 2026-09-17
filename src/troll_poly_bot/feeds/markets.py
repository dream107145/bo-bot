"""Discovery of Polymarket 5-minute crypto up/down markets via the Gamma API.

Verified against the live API:

    slug              btc-updown-5m-<epoch_seconds>   <- epoch is the window START
    endDate           ISO-8601 Z                      <- the window END / resolution
    clobTokenIds      JSON *string* holding [tokenA, tokenB]
    outcomes          JSON *string* holding ["Up", "Down"]
    conditionId       0x...
    orderPriceMinTickSize   0.01
    orderMinSize            5
    negRisk                 false
    resolutionSource  https://data.chain.link/streams/<asset>-usd-twap-60s-streams
    feeSchedule       {rate: 0.07, exponent: 1, takerOnly: true, rebateRate: 0.2}
    cryptoMarketConfig {asset, duration, twapEnabled, twapLookbackSeconds: 60}

Two fields are JSON encoded *inside* JSON, which is easy to miss and produces a
list of single characters if you forget to parse the inner string.

The strike is not in the API
----------------------------
Nothing in the market payload tells you the reference price. The market resolves
"Up" if the oracle TWAP at the END of the range is >= the TWAP at the
BEGINNING, so the strike is whatever the resolution feed printed at the window
open -- and you only know that if you were watching when it happened.

``StrikeTracker`` therefore snapshots the oracle price at each window open. A
market whose open you missed is untradeable: you would be pricing every ``z``
against a guessed strike, and the resulting bias points the same way all day.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass, field

from ..signals.costs import FeeSchedule
from ..types import Market

log = logging.getLogger(__name__)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

#: btc-updown-5m-1766162100
SLUG_RE = re.compile(r"^(?P<asset>[a-z0-9]+)-updown-(?P<dur>\d+m)-(?P<epoch>\d+)$")


def _parse_nested_json(value, default):
    """Gamma returns some list fields as JSON-encoded strings."""
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        log.warning("could not parse nested json: %r", value)
        return default


def _iso_to_ms(value: str) -> float:
    return (
        dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000.0
    )


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


@dataclass(slots=True)
class MarketMeta:
    """A discovered crypto market (the ``CryptoMarket`` of the design), before
    the strike is known. Book-derived fields live on the live book, not here."""
    market: Market
    resolution_source: str
    accepting_orders: bool
    duration_s: float
    question: str = ""
    fee: FeeSchedule = field(default_factory=FeeSchedule)
    liquidity: float = 0.0
    volume: float = 0.0
    twap_lookback_s: float = 60.0

    @property
    def has_strike(self) -> bool:
        return self.market.strike > 0.0

    @property
    def asset(self) -> str:
        return self.market.asset


def parse_market(row: dict) -> MarketMeta | None:
    """Turn one Gamma row into a MarketMeta, or None if it is not a 5m up/down.

    The asset is taken from the slug itself, so any asset the venue lists is
    accepted -- discovery decides which slugs to ask for, not this parser.
    """
    slug = row.get("slug") or ""
    m = SLUG_RE.match(slug)
    if not m:
        return None
    asset = m.group("asset").upper()

    tokens = _parse_nested_json(row.get("clobTokenIds"), [])
    outcomes = _parse_nested_json(row.get("outcomes"), [])
    if len(tokens) != 2 or len(outcomes) != 2:
        log.warning("market %s has %d tokens / %d outcomes", slug, len(tokens), len(outcomes))
        return None

    # Never assume index 0 is "Up" -- map by the outcome label. If the ordering
    # ever flips, assuming it would silently invert every position.
    lowered = [str(o).strip().lower() for o in outcomes]
    try:
        up_i = lowered.index("up")
    except ValueError:
        log.warning("market %s has no 'Up' outcome: %r", slug, outcomes)
        return None
    down_i = 1 - up_i

    open_ts = float(m.group("epoch")) * 1000.0
    close_ts = _iso_to_ms(row["endDate"])

    duration = float(m.group("dur").rstrip("m")) * 60.0
    # Trust endDate for resolution, but sanity-check it against the slug epoch:
    # a mismatch means one of the two assumptions is wrong and every horizon
    # computed from it would be wrong too.
    expected_close = open_ts + duration * 1000.0
    if abs(expected_close - close_ts) > 60_000.0:
        log.warning(
            "market %s: slug epoch + %gs = %s but endDate = %s; skipping",
            slug, duration, expected_close, close_ts,
        )
        return None

    cfg = row.get("cryptoMarketConfig") or {}
    return MarketMeta(
        market=Market(
            condition_id=row.get("conditionId", ""),
            asset=asset,
            yes_token_id=str(tokens[up_i]),
            no_token_id=str(tokens[down_i]),
            strike=0.0,                     # filled in by StrikeTracker
            open_ts=open_ts,
            close_ts=close_ts,
            tick_size=float(row.get("orderPriceMinTickSize") or 0.01),
            min_size=float(row.get("orderMinSize") or 5),
            slug=slug,
        ),
        resolution_source=row.get("resolutionSource", ""),
        accepting_orders=bool(row.get("acceptingOrders", False)),
        duration_s=duration,
        question=str(row.get("question") or ""),
        fee=FeeSchedule.from_gamma(row),
        liquidity=_f(row.get("liquidityNum", row.get("liquidity"))),
        volume=_f(row.get("volumeNum", row.get("volume"))),
        twap_lookback_s=_f(cfg.get("twapLookbackSeconds"), 60.0) if isinstance(cfg, dict) else 60.0,
    )


def parse_markets(rows: list[dict]) -> list[MarketMeta]:
    out = []
    for row in rows:
        try:
            meta = parse_market(row)
        except Exception:
            log.exception("failed to parse market %r", (row or {}).get("slug"))
            continue
        if meta is not None:
            out.append(meta)
    return out


@dataclass(slots=True)
class StrikeTracker:
    """Snapshots the oracle price at each window open.

    Call ``on_price`` with every oracle tick and ``register`` with each
    discovered market. When a market's ``open_ts`` passes, the most recent price
    at or before that instant becomes its strike.

    ``tolerance_ms`` guards the case where the feed was down across the window
    open. Stamping a strike from a price 8 seconds stale would bias every
    subsequent ``z`` for that market, so it refuses instead.
    """

    tolerance_ms: float = 2000.0
    _last_price: dict[str, tuple[float, float]] = field(default_factory=dict)
    _pending: list[MarketMeta] = field(default_factory=list)
    _struck: dict[str, MarketMeta] = field(default_factory=dict)
    _abandoned: int = 0

    def on_price(self, asset: str, price: float, ts: float) -> None:
        self._last_price[asset] = (price, ts)

    def register(self, meta: MarketMeta) -> None:
        slug = meta.market.slug
        if slug in self._struck or any(p.market.slug == slug for p in self._pending):
            return
        self._pending.append(meta)

    def poll(self, now: float) -> list[MarketMeta]:
        """Strike any market whose window has opened. Returns newly struck ones."""
        newly: list[MarketMeta] = []
        still: list[MarketMeta] = []
        for meta in self._pending:
            if now < meta.market.open_ts:
                still.append(meta)
                continue
            last = self._last_price.get(meta.market.asset)
            if last is None:
                self._abandoned += 1
                log.warning("no oracle price for %s; cannot strike %s",
                            meta.market.asset, meta.market.slug)
                continue
            price, ts = last
            if abs(meta.market.open_ts - ts) > self.tolerance_ms:
                self._abandoned += 1
                log.warning(
                    "oracle price for %s is %.0fms from window open; "
                    "refusing to guess a strike for %s",
                    meta.market.asset, meta.market.open_ts - ts, meta.market.slug,
                )
                continue
            meta.market.strike = price
            self._struck[meta.market.slug] = meta
            newly.append(meta)
        self._pending = still
        return newly

    def tradeable(self, now: float) -> list[MarketMeta]:
        return [
            m for m in self._struck.values()
            if m.has_strike and now < m.market.close_ts
        ]

    def drop_closed(self, now: float, grace_ms: float = 120_000.0) -> None:
        for slug in [
            s for s, m in self._struck.items()
            if now > m.market.close_ts + grace_ms
        ]:
            del self._struck[slug]

    @property
    def abandoned_count(self) -> int:
        """Markets skipped for want of a trustworthy strike. Watch this number:
        if it is not near zero your oracle feed is not keeping up."""
        return self._abandoned
