"""Multi-exchange spot feed: basis-adjusted composite price plus order flow.

Why more than one exchange
--------------------------
The venue settles on a Chainlink TWAP, which aggregates several exchanges.
No single exchange is that feed. A composite across Binance, Bybit and
Coinbase is closer to it, survives one feed stalling, and covers assets
Binance does not list (HYPE). The cross-exchange deviation is also a
data-quality gate: if sources disagree the strategy says STALE_DATA rather
than trade.

Why the composite is basis-adjusted
-----------------------------------
Exchanges quote at persistently different LEVELS (USDT pairs vs USD pairs,
venue premia): measured live, Coinbase sat ~9 bps under Binance on BTC. A
raw median of such sources jumps by that basis whenever a slow source drops
in or out of the freshness window, and a vol estimator fed those jumps
reported 9 bps/s on BTC where 0.9 is right -- a 10x error that would have
priced every market at a coin flip. Each exchange therefore carries a slowly
adapting offset to the primary source's level, and only joins the composite
once that offset is calibrated.

Order flow
----------
Besides the price, every adapter now emits the top-of-book sizes and each
trade with its aggressor side. That is the raw material for the order-flow
imbalance signal (features/orderflow.py): the one short-horizon predictor
with a consistent literature behind it, and the one input the earlier bot
never looked at.

Reconnects
----------
A DNS outage once made every feed reconnect every two seconds for three
hours, ~5,000 warnings. Reconnects now back off exponentially (2 s -> 60 s),
warn once and then only every tenth attempt, and report their health so the
dashboard can say "feeds down since 00:14" instead of nothing.

Message parsing is kept in pure functions so it is unit-tested; the network
loops only reconnect and dispatch.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

BINANCE_WS = "wss://stream.binance.com:9443/stream?streams="
BYBIT_WS = "wss://stream.bybit.com/v5/public/spot"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"

#: Assets Binance spot does not list (probed 2026-09-17). Others are assumed
#: listed as <asset>USDT; an unlisted symbol simply never ticks.
BINANCE_UNLISTED = {"HYPE"}

RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 60.0


@dataclass(slots=True)
class SpotQuote:
    asset: str
    exchange: str
    bid: float
    ask: float
    price: float            # microprice when sizes are known, else mid
    ts_ms: float            # local receipt time (clock-corrected)
    event_ts_ms: float | None = None   # exchange event time, when supplied
    bid_size: float | None = None      # top-of-book size, base units
    ask_size: float | None = None

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.price * 1e4 if self.price > 0 else float("nan")


@dataclass(slots=True)
class TradeTick:
    asset: str
    exchange: str
    price: float
    qty: float              # base units
    aggressor_buy: bool     # True when the taker bought (lifted the ask)
    ts_ms: float
    event_ts_ms: float | None = None

    @property
    def notional(self) -> float:
        return self.price * self.qty


@dataclass(slots=True)
class CompositeView:
    asset: str
    price: float                   # median of basis-adjusted fresh sources
    weighted_price: float          # inverse-spread weighted
    n_sources: int
    deviation_bps: float           # max |adjusted source - median| / median
    cross_exchange_spread_bps: float   # (max - min) of adjusted prices / median
    age_ms: float                  # age of the freshest source
    sources: dict[str, float]      # RAW prices per exchange
    basis_bps: dict[str, float] = field(default_factory=dict)   # level offset vs the primary


@dataclass
class FeedStatus:
    """Health of one network feed, for the dashboard and the status line."""
    name: str
    connected: bool = False
    since_ms: float = 0.0          # when the current state began
    last_msg_ms: float = 0.0
    messages: int = 0
    reconnects: int = 0
    last_error: str = ""
    backoff_s: float = RECONNECT_MIN_S

    def as_dict(self, now_ms: float) -> dict:
        return {
            "connected": self.connected,
            "state_age_s": round(max(0.0, now_ms - self.since_ms) / 1000.0, 1) if self.since_ms else None,
            "last_msg_age_s": round(max(0.0, now_ms - self.last_msg_ms) / 1000.0, 1) if self.last_msg_ms else None,
            "messages": self.messages,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


def _microprice(bid: float, ask: float, bq: float | None, aq: float | None) -> float:
    if bq and aq and bq + aq > 0:
        return (bid * aq + ask * bq) / (bq + aq)
    return (bid + ask) / 2.0


def _maybe(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ parsers

def parse_binance(msg: dict, by_symbol: dict[str, str], now_ms: float) -> SpotQuote | None:
    """Combined-stream ``bookTicker`` message -> quote."""
    stream, d = msg.get("stream", ""), msg.get("data") or {}
    if not stream.endswith("@bookTicker"):
        return None
    asset = by_symbol.get(stream.split("@")[0])
    if asset is None:
        return None
    try:
        bid, ask = float(d["b"]), float(d["a"])
        bq, aq = float(d.get("B") or 0), float(d.get("A") or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    return SpotQuote(asset, "binance", bid, ask, _microprice(bid, ask, bq, aq), now_ms,
                     bid_size=bq or None, ask_size=aq or None)


def parse_binance_trade(msg: dict, by_symbol: dict[str, str], now_ms: float) -> TradeTick | None:
    """Combined-stream ``aggTrade`` -> trade. ``m`` true means the BUYER was
    the maker, i.e. the aggressor sold."""
    stream, d = msg.get("stream", ""), msg.get("data") or {}
    if not stream.endswith("@aggTrade"):
        return None
    asset = by_symbol.get(stream.split("@")[0])
    if asset is None:
        return None
    try:
        price, qty = float(d["p"]), float(d["q"])
    except (KeyError, TypeError, ValueError):
        return None
    if price <= 0 or qty <= 0:
        return None
    return TradeTick(asset, "binance", price, qty, aggressor_buy=not bool(d.get("m")),
                     ts_ms=now_ms, event_ts_ms=_maybe(d.get("T") or d.get("E")))


def parse_bybit(msg: dict, by_symbol: dict[str, str], now_ms: float,
                state: dict[str, list[float | None]]) -> SpotQuote | None:
    """``orderbook.1.<SYMBOL>`` snapshot/delta -> quote.

    Level-1 deltas carry only the side that changed, so the last seen bid/ask
    per symbol is kept in ``state`` and the quote is emitted once both exist.
    """
    topic = msg.get("topic", "")
    if not topic.startswith("orderbook.1."):
        return None
    sym = topic.split(".")[-1]
    asset = by_symbol.get(sym)
    if asset is None:
        return None
    d = msg.get("data") or {}
    st = state.setdefault(sym, [None, None, None, None])     # bid, ask, bq, aq
    try:
        if d.get("b"):
            st[0], st[2] = float(d["b"][0][0]), float(d["b"][0][1])
        if d.get("a"):
            st[1], st[3] = float(d["a"][0][0]), float(d["a"][0][1])
    except (IndexError, TypeError, ValueError):
        return None
    if st[0] is None or st[1] is None or st[0] <= 0 or st[1] <= 0:
        return None
    ev = msg.get("ts")
    return SpotQuote(asset, "bybit", st[0], st[1], _microprice(st[0], st[1], st[2], st[3]), now_ms,
                     float(ev) if isinstance(ev, (int, float)) else None,
                     bid_size=st[2] or None, ask_size=st[3] or None)


def parse_bybit_trades(msg: dict, by_symbol: dict[str, str], now_ms: float) -> list[TradeTick]:
    """``publicTrade.<SYMBOL>`` -> trades. ``S`` is the taker side."""
    topic = msg.get("topic", "")
    if not topic.startswith("publicTrade."):
        return []
    asset = by_symbol.get(topic.split(".")[-1])
    if asset is None:
        return []
    out: list[TradeTick] = []
    for d in msg.get("data") or []:
        try:
            price, qty = float(d["p"]), float(d["v"])
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0 or qty <= 0:
            continue
        out.append(TradeTick(asset, "bybit", price, qty, aggressor_buy=str(d.get("S", "")).lower() == "buy",
                             ts_ms=now_ms, event_ts_ms=_maybe(d.get("T"))))
    return out


def parse_coinbase(msg: dict, by_product: dict[str, str], now_ms: float) -> SpotQuote | None:
    """Exchange feed ``ticker`` message -> quote."""
    if msg.get("type") != "ticker":
        return None
    asset = by_product.get(msg.get("product_id", ""))
    if asset is None:
        return None
    try:
        bid, ask = float(msg["best_bid"]), float(msg["best_ask"])
    except (KeyError, TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    bq = _maybe(msg.get("best_bid_size"))
    aq = _maybe(msg.get("best_ask_size"))
    return SpotQuote(asset, "coinbase", bid, ask, _microprice(bid, ask, bq, aq), now_ms,
                     bid_size=bq, ask_size=aq)


def parse_coinbase_trade(msg: dict, by_product: dict[str, str], now_ms: float) -> TradeTick | None:
    """The same ``ticker`` message carries the last trade: ``price``,
    ``last_size`` and ``side`` (the taker side)."""
    if msg.get("type") != "ticker":
        return None
    asset = by_product.get(msg.get("product_id", ""))
    if asset is None:
        return None
    price, qty = _maybe(msg.get("price")), _maybe(msg.get("last_size"))
    side = str(msg.get("side", "")).lower()
    if not price or not qty or side not in ("buy", "sell"):
        return None
    return TradeTick(asset, "coinbase", price, qty, aggressor_buy=(side == "buy"), ts_ms=now_ms)


# ---------------------------------------------------------------- composite

@dataclass(slots=True)
class _Basis:
    """EWMA of log(price_exchange / price_primary), with a sample count."""
    value: float = 0.0
    n: int = 0
    last_ts: float = 0.0


@dataclass(slots=True)
class CompositeSpot:
    """Latest quote per (asset, exchange) and the composite view across them.

    The first exchange in ``reference_order`` that has ever quoted an asset is
    that asset's primary; every other exchange's level is expressed relative
    to it. When the primary is stale, the others still combine at the
    primary's level, so the composite series has no level jumps.
    """
    max_age_ms: float = 3_000.0
    reference_order: tuple[str, ...] = ("binance", "bybit", "coinbase")
    basis_halflife_s: float = 300.0
    #: pairs of co-fresh quotes needed before an exchange joins the composite
    basis_min_samples: int = 5
    #: quotes further apart than this are not compared for the basis
    pair_window_ms: float = 1_000.0
    #: no view until this long after an asset's first quote, so every exchange
    #: has had a chance to quote and the primary is settled before anything
    #: downstream (the vol estimator in particular) sees a price
    warmup_ms: float = 3_000.0
    quotes: dict[str, dict[str, SpotQuote]] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    primary: dict[str, str] = field(default_factory=dict)
    basis: dict[str, dict[str, _Basis]] = field(default_factory=dict)
    first_seen: dict[str, float] = field(default_factory=dict)

    def _rank(self, exchange: str) -> int:
        return self.reference_order.index(exchange) if exchange in self.reference_order else 99

    def update(self, q: SpotQuote) -> None:
        qs = self.quotes.setdefault(q.asset, {})
        qs[q.exchange] = q
        self.counts[q.exchange] = self.counts.get(q.exchange, 0) + 1
        self.first_seen.setdefault(q.asset, q.ts_ms)
        prim = self.primary.get(q.asset)
        if prim is None or self._rank(q.exchange) < self._rank(prim):
            # a better-ranked exchange takes over as the level reference; the
            # old primary's zero basis is now wrong and must be re-measured
            # (seen live: Coinbase quoted first, kept basis 0, and sat 9.7 bps
            # off Binance's level while counted as calibrated)
            self.basis[q.asset] = {q.exchange: _Basis(0.0, self.basis_min_samples)}
            self.primary[q.asset] = prim = q.exchange
        if q.exchange == prim:
            return
        # compare against the freshest calibrated source, chained to the primary level
        ref = self._reference(q.asset, q.ts_ms, exclude=q.exchange)
        if ref is None or abs(ref.ts_ms - q.ts_ms) > self.pair_window_ms:
            return
        bmap = self.basis.setdefault(q.asset, {})
        ref_basis = bmap[ref.exchange].value
        d = math.log(q.price / ref.price) + ref_basis
        b = bmap.get(q.exchange)
        if b is None:
            bmap[q.exchange] = _Basis(d, 1, q.ts_ms)
            return
        dt_s = max((q.ts_ms - b.last_ts) / 1000.0, 0.01) if b.last_ts else 1.0
        alpha = 1.0 - 0.5 ** (dt_s / self.basis_halflife_s)
        # a young estimate learns fast, a mature one slowly
        alpha = max(alpha, 1.0 / (b.n + 1))
        b.value += alpha * (d - b.value)
        b.n += 1
        b.last_ts = q.ts_ms

    def _calibrated(self, asset: str, exchange: str) -> bool:
        b = self.basis.get(asset, {}).get(exchange)
        return b is not None and b.n >= self.basis_min_samples

    def _reference(self, asset: str, now_ms: float, exclude: str | None = None) -> SpotQuote | None:
        best: SpotQuote | None = None
        for e in sorted(self.quotes.get(asset, {}), key=self._rank):
            q = self.quotes[asset][e]
            if e == exclude or now_ms - q.ts_ms > self.max_age_ms or not self._calibrated(asset, e):
                continue
            if best is None:
                best = q
        return best

    def adjusted(self, q: SpotQuote) -> float:
        b = self.basis.get(q.asset, {}).get(q.exchange)
        return q.price * math.exp(-b.value) if b is not None else q.price

    def fresh_quotes(self, asset: str, now_ms: float) -> list[SpotQuote]:
        return [q for q in self.quotes.get(asset, {}).values()
                if now_ms - q.ts_ms <= self.max_age_ms and self._calibrated(asset, q.exchange)]

    def view(self, asset: str, now_ms: float) -> CompositeView | None:
        if now_ms - self.first_seen.get(asset, now_ms) < self.warmup_ms:
            return None
        qs = self.fresh_quotes(asset, now_ms)
        if not qs:
            return None
        prices = [self.adjusted(q) for q in qs]
        med = statistics.median(prices)
        weights = [1.0 / max(q.ask - q.bid, 1e-9) for q in qs]
        wp = sum(w * p for w, p in zip(weights, prices)) / sum(weights)
        dev = max(abs(p - med) for p in prices) / med * 1e4 if med > 0 else float("nan")
        xs = (max(prices) - min(prices)) / med * 1e4 if med > 0 else float("nan")
        return CompositeView(
            asset=asset, price=med, weighted_price=wp, n_sources=len(qs), deviation_bps=dev,
            cross_exchange_spread_bps=xs, age_ms=min(now_ms - q.ts_ms for q in qs),
            sources={q.exchange: q.price for q in qs},
            basis_bps={e: round(b.value * 1e4, 2) for e, b in self.basis.get(asset, {}).items()},
        )


# ---------------------------------------------------------------- adapters

OnQuote = Callable[[SpotQuote], None]
OnTrade = Callable[[TradeTick], None]
Clock = Callable[[], float]


def _touch(health: dict[str, FeedStatus] | None, name: str, clock: Clock, connected: bool | None = None,
           error: str | None = None, message: bool = False) -> None:
    if health is None:
        return
    st = health.setdefault(name, FeedStatus(name))
    now = clock()
    if message:
        st.messages += 1
        st.last_msg_ms = now
    if connected is not None and connected != st.connected:
        st.connected, st.since_ms = connected, now
        if connected:
            st.backoff_s = RECONNECT_MIN_S
    if error is not None:
        st.last_error = error


async def _loop(name: str, connect: Callable[[], Awaitable[None]], stop: asyncio.Event,
                clock: Clock, health: dict[str, FeedStatus] | None) -> None:
    """Reconnect forever with exponential backoff; quiet after the first warning."""
    backoff = RECONNECT_MIN_S
    while not stop.is_set():
        started = time.monotonic()
        try:
            await connect()
        except Exception as exc:                          # noqa: BLE001
            if stop.is_set():
                return
            st = health.setdefault(name, FeedStatus(name)) if health is not None else None
            # a connection that lived a while earned a fresh, short backoff
            if time.monotonic() - started > 30.0:
                backoff = RECONNECT_MIN_S
            _touch(health, name, clock, connected=False, error=f"{type(exc).__name__}: {exc}"[:120])
            if st is not None:
                st.reconnects += 1
                st.backoff_s = backoff
            n = st.reconnects if st is not None else 1
            msg = "%s feed dropped (%s); retry in %.0fs (attempt %d)"
            (log.warning if n == 1 or n % 10 == 0 else log.debug)(msg, name, type(exc).__name__, backoff, n)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, RECONNECT_MAX_S)
        else:
            if stop.is_set():
                return
            _touch(health, name, clock, connected=False, error="closed")
            await asyncio.sleep(RECONNECT_MIN_S)


async def run_binance(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                      on_latency: Callable[[float], None] | None = None, on_trade: OnTrade | None = None,
                      health: dict[str, FeedStatus] | None = None) -> None:
    import websockets
    syms = {f"{a.lower()}usdt": a for a in assets if a not in BINANCE_UNLISTED}
    if not syms:
        return
    streams = "/".join(f"{s}@bookTicker" for s in syms) + "/" + "/".join(f"{s}@aggTrade" for s in syms)

    async def connect() -> None:
        async with websockets.connect(BINANCE_WS + streams, open_timeout=20, ping_interval=15,
                                      close_timeout=3) as ws:
            log.info("binance feed connected (%s)", ", ".join(syms.values()))
            _touch(health, "binance", clock, connected=True)
            while not stop.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=45)
                msg = json.loads(raw)
                now = clock()
                _touch(health, "binance", clock, message=True)
                q = parse_binance(msg, syms, now)
                if q is not None:
                    on_quote(q)
                    continue
                t = parse_binance_trade(msg, syms, now)
                if t is not None:
                    if on_trade is not None:
                        on_trade(t)
                    if on_latency is not None and t.event_ts_ms:
                        on_latency(max(0.0, now - t.event_ts_ms))
    await _loop("binance", connect, stop, clock, health)


async def run_bybit(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                    on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None) -> None:
    import websockets
    syms = {f"{a}USDT": a for a in assets}

    async def connect() -> None:
        async with websockets.connect(BYBIT_WS, open_timeout=20, ping_interval=None, close_timeout=3) as ws:
            args = [f"orderbook.1.{s}" for s in syms] + [f"publicTrade.{s}" for s in syms]
            # Bybit caps the topics per subscribe request; one oversized
            # request was silently refused and the feed went quiet (3 messages
            # in 75 s). Ten per request is inside the limit.
            for i in range(0, len(args), 10):
                await ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 10]}))
            log.info("bybit feed connected (%s)", ", ".join(syms.values()))
            _touch(health, "bybit", clock, connected=True)
            state: dict[str, list[float | None]] = {}
            last_ping = time.monotonic()
            while not stop.is_set():
                if time.monotonic() - last_ping > 20:
                    await ws.send(json.dumps({"op": "ping"}))
                    last_ping = time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=25)
                except asyncio.TimeoutError:
                    continue
                msg = json.loads(raw)
                now = clock()
                _touch(health, "bybit", clock, message=True)
                q = parse_bybit(msg, syms, now, state)
                if q is not None:
                    on_quote(q)
                    continue
                if on_trade is not None:
                    for t in parse_bybit_trades(msg, syms, now):
                        on_trade(t)
    await _loop("bybit", connect, stop, clock, health)


async def run_coinbase(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                       on_trade: OnTrade | None = None, health: dict[str, FeedStatus] | None = None) -> None:
    import websockets
    prods = {f"{a}-USD": a for a in assets}

    async def connect() -> None:
        async with websockets.connect(COINBASE_WS, open_timeout=20, ping_interval=15, close_timeout=3) as ws:
            await ws.send(json.dumps({"type": "subscribe", "product_ids": list(prods),
                                      "channels": ["ticker", "heartbeat"]}))
            log.info("coinbase feed connected (%s)", ", ".join(prods.values()))
            _touch(health, "coinbase", clock, connected=True)
            while not stop.is_set():
                raw = await asyncio.wait_for(ws.recv(), timeout=45)
                msg = json.loads(raw)
                now = clock()
                _touch(health, "coinbase", clock, message=True)
                q = parse_coinbase(msg, prods, now)
                if q is not None:
                    on_quote(q)
                    if on_trade is not None:
                        t = parse_coinbase_trade(msg, prods, now)
                        if t is not None:
                            on_trade(t)
    await _loop("coinbase", connect, stop, clock, health)


async def run_all(assets: tuple[str, ...], on_quote: OnQuote, clock: Clock, stop: asyncio.Event,
                  exchanges: tuple[str, ...] = ("binance", "bybit", "coinbase"),
                  on_latency: Callable[[float], None] | None = None,
                  on_trade: OnTrade | None = None,
                  health: dict[str, FeedStatus] | None = None) -> None:
    """Run the selected adapters until ``stop`` is set, then close them (bounded)."""
    tasks = []
    if "binance" in exchanges:
        tasks.append(asyncio.create_task(run_binance(assets, on_quote, clock, stop, on_latency, on_trade, health)))
    if "bybit" in exchanges:
        tasks.append(asyncio.create_task(run_bybit(assets, on_quote, clock, stop, on_trade, health)))
    if "coinbase" in exchanges:
        tasks.append(asyncio.create_task(run_coinbase(assets, on_quote, clock, stop, on_trade, health)))
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(t, timeout=5.0)
