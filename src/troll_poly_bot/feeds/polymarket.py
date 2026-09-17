"""Polymarket CLOB market-data adapter.

Verified against the live API on a running `btc-updown-5m-*` market.

Two things here will silently corrupt a strategy if you assume the obvious:

1. **The book arrives WORST FIRST.**

       bids: [{price: 0.01 ...}, ... , {price: 0.74 ...}]   ascending
       asks: [{price: 0.99 ...}, ... , {price: 0.75 ...}]   descending

   So ``bids[0]`` is the *worst* bid and ``asks[0]`` is the *worst* ask. Code
   that reads ``asks[0]`` as the touch sees 0.99 instead of 0.75 and concludes
   every market is a screaming buy. ``parse_book`` sorts explicitly rather than
   reversing, so it stays correct even if the venue changes convention.

2. **Prices and sizes are strings**, not numbers.

Also observed: Gamma's summary ``bestBid``/``bestAsk`` fields disagreed with the
live CLOB book (0.49/0.50 against an actual 0.74/0.75). They appear to be
cached. Quote from ``/book``; never trade off the Gamma summary.
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from ..types import BookLevel, OrderBook

log = logging.getLogger(__name__)

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"

#: Polymarket 5m crypto up/down windows are aligned to 300s UTC boundaries and
#: the slug carries the window START. Discovery is therefore deterministic --
#: no listing endpoint, no pagination. (The Gamma listing endpoint was observed
#: returning months-old rows for `order=endDate&ascending=true`, so constructing
#: the slug is both cheaper and more reliable.)
WINDOW_S = 300


def window_epoch(now_s: float, offset_windows: int = 0) -> int:
    return int(now_s) // WINDOW_S * WINDOW_S + offset_windows * WINDOW_S


def slug_for(asset: str, now_s: float, offset_windows: int = 0) -> str:
    return f"{asset.lower()}-updown-5m-{window_epoch(now_s, offset_windows)}"


def parse_book(payload: dict, token_id: str | None = None) -> OrderBook:
    """Normalise a CLOB ``/book`` payload into an OrderBook.

    Sorts both sides explicitly: bids best (highest) first, asks best (lowest)
    first, which is what the rest of the codebase assumes.
    """
    def levels(raw, reverse: bool) -> list[BookLevel]:
        out: list[BookLevel] = []
        for lvl in raw or []:
            try:
                price = float(lvl["price"])
                size = float(lvl["size"])
            except (KeyError, TypeError, ValueError):
                log.warning("skipping malformed book level %r", lvl)
                continue
            if size > 0.0:
                out.append(BookLevel(price, size))
        out.sort(key=lambda l: l.price, reverse=reverse)
        return out

    ts = payload.get("timestamp")
    try:
        ts_ms = float(ts) if ts is not None else time.time() * 1000.0
    except (TypeError, ValueError):
        ts_ms = time.time() * 1000.0

    return OrderBook(
        token_id=str(payload.get("asset_id") or token_id or ""),
        bids=levels(payload.get("bids"), reverse=True),    # best = highest
        asks=levels(payload.get("asks"), reverse=False),   # best = lowest
        ts=ts_ms,
    )


@dataclass(slots=True)
class LatencySample:
    """One observed round trip."""
    ms: float
    at: float


@dataclass(slots=True)
class MeasuredLatency:
    """Records real observed round trips.

    In live paper mode you do not need to *simulate* your network -- you can
    measure it. Feed these samples back into the paper engine so fills are
    simulated against the latency you actually have, not a guessed profile.
    """
    samples: list[LatencySample] = field(default_factory=list)
    max_samples: int = 2000

    def record(self, ms: float, at: float | None = None) -> None:
        self.samples.append(LatencySample(ms, at if at is not None else time.time() * 1000.0))
        if len(self.samples) > self.max_samples:
            del self.samples[: len(self.samples) - self.max_samples]

    def percentile(self, q: float) -> float:
        if not self.samples:
            return 0.0
        vals = sorted(s.ms for s in self.samples)
        i = min(len(vals) - 1, max(0, int(q * len(vals))))
        return vals[i]

    def summary(self) -> dict[str, float]:
        if not self.samples:
            return {}
        vals = [s.ms for s in self.samples]
        return {
            "n": float(len(vals)),
            "min_ms": min(vals),
            "median_ms": self.percentile(0.50),
            "p95_ms": self.percentile(0.95),
            "p99_ms": self.percentile(0.99),
            "max_ms": max(vals),
        }


class ClobClient:
    """Minimal read-only CLOB/Gamma client.

    Read-only on purpose: this repo has no order-signing path, so there is no
    code here that could place a real order even by accident.
    """

    def __init__(self, timeout: float = 10.0, latency: MeasuredLatency | None = None) -> None:
        self.timeout = timeout
        self.latency = latency or MeasuredLatency()

    def _get(self, url: str) -> dict | list:
        req = urllib.request.Request(url, headers={"User-Agent": "troll-poly-bot/0.1"})
        t0 = time.perf_counter()
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = json.load(resp)
        self.latency.record((time.perf_counter() - t0) * 1000.0)
        return body

    def book(self, token_id: str) -> OrderBook:
        payload = self._get(f"{CLOB_BASE}/book?token_id={urllib.parse.quote(token_id)}")
        return parse_book(payload, token_id)

    def market_by_slug(self, slug: str) -> dict | None:
        rows = self._get(f"{GAMMA_BASE}/markets?slug={urllib.parse.quote(slug)}")
        if isinstance(rows, list) and rows:
            return rows[0]
        return None
