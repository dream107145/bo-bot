"""Live paper trading against real Polymarket data, across every listed asset.

Real books, real prices, real measured latency -- **simulated fills**. There is
no order-signing code anywhere in this repo, so nothing here can place a real
order even if it wanted to.

Pipeline
--------
    AssetRegistry (probe Gamma for every candidate asset)      market/discovery
      -> MarketMeta per window, with the venue fee schedule    feeds/markets
      -> CLOB websocket books                                  LiveBook (below)
      -> composite spot from Binance + Bybit + Coinbase        feeds/spot
      -> vol, TWAP state, strike at the window open            pricing/*
      -> StrategyEngine: features, probability, costs, gates   strategy/engine
      -> RiskManager: caps, sizing                             risk/limits
      -> PaperExchange with measured latency                   execution/paper
      -> settlement from the venue, evidence statistics        below

Every market that is *not* traded carries a reason code, counted and shown
on the dashboard. That table is the product: it says why the bot is sitting
out, which on these markets is most of the time.

The Binance/Chainlink caveat, stated plainly
---------------------------------------------
These markets settle on a Chainlink 60s TWAP stream, not on any exchange. The
composite spot is a *proxy*; settlement records what our own TWAP said next to
what the venue resolved, and ``basis_disagreements`` counts the times they
differed (~1% of windows on the archive).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import orjson
import random
import statistics
import time
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path

import websockets

from . import earnings as earnings_log
from .config import BotConfig
from .control import ControlFile, apply_to, current_values
from .execution.latency import HOME_BROADBAND, LatencyModel, LatencyProfile
from .execution.paper import FeeModel, PaperExchange
from .features.engine import FeatureEngine
from .features.orderflow import OrderFlowCalibration, OrderFlowState
from .feeds.markets import MarketMeta, StrikeTracker, parse_market
from .feeds.polymarket import GAMMA_BASE, window_epoch
from .feeds.spot import (
    RECONNECT_MAX_S, RECONNECT_MIN_S, CompositeSpot, FeedStatus, SpotQuote, TradeTick, run_all,
)
from .market.discovery import AssetRegistry, FetchFailed
from .pricing.twap import TwapState
from .pricing.vol import EwmaVol, TwoScaleVol
from .risk.limits import RiskManager
from .signals.costs import FeeSchedule
from .strategy.engine import Evaluation, Reason, StrategyEngine
from .types import BookLevel, Order, OrderBook, Side, TimeInForce

log = logging.getLogger("live")

CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

#: Share counts below this are dust, not a position worth an order and a fee.
EXIT_EPS = 1e-9


# ─────────────────────────────── clock sync ────────────────────────────────


class ClockSync:
    """Offset between this machine's clock and the exchange's.

    The machine this was first run on was ~970ms FAST, and a wrong clock
    corrupts staleness gates, the strike instant and the "can this order land"
    check. Measured NTP-style, keeping the sample with the smallest round trip.
    """

    TIME_URL = "https://api.binance.com/api/v3/time"

    def __init__(self) -> None:
        self.offset_ms = 0.0          # local clock minus true time
        self.rtt_ms = 0.0
        self.synced = False

    def now(self) -> float:
        """Exchange-aligned epoch milliseconds."""
        return time.time() * 1000.0 - self.offset_ms

    async def resync(self, samples: int = 5) -> None:
        best: tuple[float, float] | None = None
        for _ in range(samples):
            try:
                t0 = time.time() * 1000.0
                body, _ = await asyncio.to_thread(self._fetch)
                t1 = time.time() * 1000.0
            except Exception:
                continue
            if not body:
                continue
            srv = _maybe_float(body.get("serverTime"))
            if srv is None:
                continue
            rtt = t1 - t0
            offset = (t0 + t1) / 2.0 - srv
            if best is None or rtt < best[0]:
                best = (rtt, offset)
            await asyncio.sleep(0.2)
        if best is not None:
            self.rtt_ms, self.offset_ms = best
            self.synced = True

    @staticmethod
    def _fetch():
        req = urllib.request.Request(ClockSync.TIME_URL,
                                     headers={"User-Agent": "troll-poly-bot/0.1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r), None


# ────────────────────────────────── books ──────────────────────────────────


class LiveBook:
    """A book maintained from a snapshot plus ``price_change`` deltas."""

    __slots__ = ("token_id", "bids", "asks", "ts", "_best_bid", "_best_ask")

    def __init__(self, token_id: str) -> None:
        self.token_id = token_id
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.ts: float = 0.0
        self._best_bid: float | None = None
        self._best_ask: float | None = None

    def apply_snapshot(self, msg: dict) -> None:
        self.bids = self._levels(msg.get("bids"))
        self.asks = self._levels(msg.get("asks"))
        self.ts = _ts(msg.get("timestamp"))
        self._best_bid = self._best_ask = None

    @staticmethod
    def _levels(raw) -> dict[float, float]:
        out: dict[float, float] = {}
        for lvl in raw or []:
            try:
                price, size = float(lvl["price"]), float(lvl["size"])
            except (KeyError, TypeError, ValueError):
                continue
            if size > 0:
                out[price] = size
        return out

    def apply_change(self, change: dict, ts: float) -> None:
        try:
            price, size = float(change["price"]), float(change["size"])
        except (KeyError, TypeError, ValueError):
            return
        side = change.get("side")
        book = self.bids if side == "BUY" else self.asks if side == "SELL" else None
        if book is None:
            return
        if size <= 0:
            book.pop(price, None)
        else:
            book[price] = size
        self.ts = ts
        self._best_bid = _maybe_float(change.get("best_bid"))
        self._best_ask = _maybe_float(change.get("best_ask"))

    def to_order_book(self) -> OrderBook:
        bids = sorted(self.bids.items(), key=lambda kv: -kv[0])
        asks = sorted(self.asks.items(), key=lambda kv: kv[0])
        book = OrderBook(
            token_id=self.token_id,
            bids=[BookLevel(p, s) for p, s in bids],
            asks=[BookLevel(p, s) for p, s in asks],
            ts=self.ts,
        )
        if self._best_ask is not None and book.asks and book.asks[0].price < self._best_ask - 1e-9:
            book.asks = [l for l in book.asks if l.price >= self._best_ask - 1e-9]
        if self._best_bid is not None and book.bids and book.bids[0].price > self._best_bid + 1e-9:
            book.bids = [l for l in book.bids if l.price <= self._best_bid + 1e-9]
        return book


def _ts(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return time.time() * 1000.0


def _maybe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _depth_near_touch(levels: list[BookLevel], best: float | None, ticks: float = 0.02, ask: bool = True) -> float:
    if best is None:
        return 0.0
    if ask:
        return sum(l.size for l in levels if l.price <= best + ticks + 1e-9)
    return sum(l.size for l in levels if l.price >= best - ticks - 1e-9)


# ───────────────────────────────── latency ─────────────────────────────────


class EmpiricalLatency(LatencyModel):
    """Bootstrap sampler over measured latencies."""

    MIN_SAMPLES = 25
    CAP = 3000

    def __init__(self, seed_profile: LatencyProfile = HOME_BROADBAND, seed: int = 0) -> None:
        super().__init__(seed_profile, seed)
        self.spot: list[float] = []
        self.book: list[float] = []
        self.order: list[float] = []
        self._rand = random.Random(seed)

    def _record(self, bucket: list[float], ms: float) -> None:
        if not math.isfinite(ms) or ms < 0 or ms > 60_000:
            return
        bucket.append(ms)
        if len(bucket) > self.CAP:
            del bucket[: len(bucket) - self.CAP]

    def record_spot(self, ms: float) -> None: self._record(self.spot, ms)
    def record_book(self, ms: float) -> None: self._record(self.book, ms)
    def record_order(self, ms: float) -> None: self._record(self.order, ms)

    def _draw_from(self, bucket: list[float], fallback) -> float:
        if len(bucket) < self.MIN_SAMPLES:
            return fallback()
        return self._rand.choice(bucket)

    def md_spot(self) -> float:
        return self._draw_from(self.spot, super().md_spot)

    def md_book(self) -> float:
        return self._draw_from(self.book, super().md_book)

    def submit(self) -> float:
        return self._draw_from(self.order, super().submit) / 2.0

    def ack(self) -> float:
        return self._draw_from(self.order, super().ack) / 2.0

    def cancel(self) -> float:
        return self.submit()

    @staticmethod
    def _pct(vals: list[float], q: float) -> float:
        if not vals:
            return 0.0
        s = sorted(vals)
        return s[min(len(s) - 1, max(0, int(q * len(s))))]

    def report(self) -> dict[str, float]:
        return {
            "spot_p50": self._pct(self.spot, 0.5), "spot_p95": self._pct(self.spot, 0.95),
            "book_p50": self._pct(self.book, 0.5), "book_p95": self._pct(self.book, 0.95),
            "order_rtt_p50": self._pct(self.order, 0.5), "order_rtt_p95": self._pct(self.order, 0.95),
            "n_spot": len(self.spot), "n_book": len(self.book), "n_order": len(self.order),
        }

    @property
    def reaction_lag_ms(self) -> float:
        return self._pct(self.spot, 0.5) + self._pct(self.order, 0.5) / 2.0


# ────────────────────────────────── state ──────────────────────────────────


@dataclass
class LiveMarket:
    meta: MarketMeta
    books: dict[str, LiveBook] = field(default_factory=dict)
    settled: bool = False
    submitted: int = 0
    inflight: int = 0
    outcome: str | None = None
    pnl: float = 0.0
    #: closed early by the take-profit rule. Keeps us from re-entering the same
    #: window we just de-risked, and stops settlement from reporting it twice.
    exited: bool = False
    settle_attempts: int = 0
    next_settle_ms: float = 0.0

    @property
    def epoch(self) -> int:
        return int(self.meta.market.open_ts // 1000)


@dataclass
class Agreement:
    """Running check of the analytic fair value against the market mid, by
    time-to-expiry bucket, for the dashboard."""
    n: int = 0
    sum_diff: float = 0.0
    sum_abs: float = 0.0
    sum_sq: float = 0.0
    buckets: dict = field(default_factory=dict)
    EDGES = (300.0, 150.0, 90.0, 60.0, 40.0, 25.0, 15.0, 8.0, 0.0)

    @classmethod
    def bucket_of(cls, secs_left: float) -> str:
        for i in range(len(cls.EDGES) - 1):
            hi, lo = cls.EDGES[i], cls.EDGES[i + 1]
            if lo < secs_left <= hi:
                return f"{lo:.0f}-{hi:.0f}s"
        return "0s"

    def add(self, fair_up: float, mid_up: float, secs_left: float | None = None) -> None:
        d = fair_up - mid_up
        self.n += 1
        self.sum_diff += d
        self.sum_abs += abs(d)
        self.sum_sq += d * d
        if secs_left is not None:
            b = self.buckets.setdefault(self.bucket_of(secs_left), [0, 0.0, 0.0])
            b[0] += 1
            b[1] += d
            b[2] += abs(d)

    def report(self) -> dict:
        if not self.n:
            return {}
        return {
            "n": self.n,
            "mean_bias": round(self.sum_diff / self.n, 4),
            "mean_abs": round(self.sum_abs / self.n, 4),
            "rms": round((self.sum_sq / self.n) ** 0.5, 4),
            "by_secs_left": {
                k: {"n": v[0], "bias": round(v[1] / v[0], 4), "abs": round(v[2] / v[0], 4)}
                for k, v in sorted(self.buckets.items(),
                                   key=lambda kv: -float(kv[0].split("-")[0].rstrip("s") or 0))
                if v[0] >= 20
            },
        }


@dataclass
class LiveStats:
    started: float = field(default_factory=time.time)
    windows_seen: int = 0
    windows_traded: int = 0
    settled: int = 0
    wins: int = 0
    losses: int = 0
    basis_disagreements: int = 0
    unresolvable: int = 0
    realised_pnl: float = 0.0


class LiveBot:
    def __init__(
        self,
        assets: tuple[str, ...] = (),
        balance: float = 100.0,
        cfg: BotConfig | None = None,
        state_path: str = "data/live_state.json",
        trade_log: str = "data/live_trades.jsonl",
        exchanges: tuple[str, ...] | None = None,
        reset_history: bool = True,
        exchange=None,
    ) -> None:
        self.cfg = cfg or BotConfig()
        self.cfg.starting_balance = balance
        self.exchanges = tuple(exchanges or self.cfg.feeds.exchanges)
        #: empty = trade every asset the venue lists; otherwise a restriction
        self.registry = AssetRegistry(candidates=self.cfg.feeds.candidate_assets,
                                      reprobe_s=self.cfg.feeds.reprobe_s,
                                      pinned=tuple(a.upper() for a in assets))

        self.latency = EmpiricalLatency(HOME_BROADBAND, seed=self.cfg.latency_seed)
        if exchange is not None:
            # a real venue adapter (execution/polymarket.py); its fee model is
            # the one the per-market schedules are written into
            self.exchange = exchange
            self.fees = exchange.fees
        else:
            self.fees = FeeModel.from_schedule(FeeSchedule())
            self.exchange = PaperExchange(self.latency, self.fees, starting_balance=balance)
        self.is_live = bool(getattr(self.exchange, "is_live", False))
        self.risk = RiskManager(cfg=self.cfg.risk, starting_balance=balance)
        self.engine = StrategyEngine(self.cfg.engine, self.risk, FeatureEngine())
        self.strategy_latency: LatencyProfile = HOME_BROADBAND

        self.clock = ClockSync()
        if self.is_live:
            self.exchange.clock = self.clock.now
        self._base_spot_age = self.cfg.engine.max_spot_age_ms
        self._base_book_age = self.cfg.engine.max_book_age_ms
        #: Tunables the dashboard may change while we run. ``control_scope``
        #: keeps a paper bot and a real-money bot in one directory steerable
        #: apart: each reads the shared "all" section plus its own.
        self.controls = ControlFile()
        self.control_scope = "live" if self.is_live else "paper"
        self.control_applied: list[str] = []
        self.control_errors: list[str] = []
        self.control_at: float | None = None
        self.tracker = StrikeTracker(tolerance_ms=3000.0)
        self.markets: dict[str, LiveMarket] = {}
        self.token_index: dict[str, tuple[str, LiveMarket]] = {}

        self.composite = CompositeSpot(max_age_ms=self.cfg.feeds.max_quote_age_ms)
        self.vol: dict[str, object] = {}
        self.twap: dict[str, TwapState] = {}
        self.spot: dict[str, float] = {}
        self.spot_ts: dict[str, float] = {}
        self._last_model_update: dict[str, float] = {}
        self.orderflow: dict[str, OrderFlowState] = {}
        self._last_calib_ms: dict[str, float] = {}
        self.feed_health: dict[str, FeedStatus] = {}
        self.discovery_errors = 0
        self.last_discovery_error = ""

        self.stats = LiveStats()
        self.agreement: dict[str, Agreement] = {}
        self.pnl_by_asset: dict[str, float] = {}
        self.epoch_pnl: dict[int, float] = {}
        self.exec_rejections: dict[str, int] = {}
        self._last_mark: dict[str, float] = {}
        self.fair_by_order: dict[str, dict] = {}
        self.price_history: dict[str, deque] = {}
        self._last_hist_ms = 0.0
        self.ledger: list[dict] = []

        self.state_path = Path(state_path)
        self.trade_log = Path(trade_log)
        self.chart_dir = Path(self.cfg.chart_dir)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.trade_log.parent.mkdir(parents=True, exist_ok=True)
        if reset_history:
            self._reset_trade_log()

        self._subscribed: set[str] = set()
        self._resub = asyncio.Event()
        self._stop = asyncio.Event()
        self._spot_task: asyncio.Task | None = None
        self._spot_stop: asyncio.Event | None = None

    # ───────────────────────────── per-asset state ─────────────────────────

    def _make_vol(self):
        v = self.cfg.vol
        fast = EwmaVol(grid_ms=v.grid_ms, halflife_s=v.halflife_s,
                       floor_per_sec=v.floor_per_sec, ceil_per_sec=v.ceil_per_sec)
        if not v.two_scale:
            return fast
        return TwoScaleVol(
            fast=fast,
            slow=EwmaVol(grid_ms=v.slow_grid_ms, halflife_s=v.slow_halflife_s,
                         floor_per_sec=v.floor_per_sec, ceil_per_sec=v.ceil_per_sec),
            default_ratio=v.default_ratio,
        )

    def _ensure_asset(self, asset: str) -> None:
        if asset not in self.vol:
            self.vol[asset] = self._make_vol()
            self.twap[asset] = TwapState()
            self._last_model_update[asset] = 0.0
        self.orderflow.setdefault(asset, OrderFlowState())
        self.engine.ofi_calibration.setdefault(asset, OrderFlowCalibration())

    def _feed_state(self, name: str, connected: bool | None = None, error: str | None = None,
                    message: bool = False) -> FeedStatus:
        st = self.feed_health.setdefault(name, FeedStatus(name))
        now = self.clock.now()
        if message:
            st.messages += 1
            st.last_msg_ms = now
        if connected is not None and connected != st.connected:
            st.connected, st.since_ms = connected, now
            if connected:
                st.backoff_s = RECONNECT_MIN_S
        if error is not None:
            st.last_error = error
        return st

    @property
    def assets(self) -> tuple[str, ...]:
        return self.registry.assets

    # ───────────────────────────── discovery ─────────────────────────────

    async def discover(self) -> None:
        """Probe which assets are listed, then construct current + next slugs."""
        while not self._stop.is_set():
            try:
                now_s = self.clock.now() / 1000.0
                if self.registry.due(now_s):
                    found = await self.registry.refresh(self._fetch_market, now_s)
                    for a in found:
                        self._ensure_asset(a)
                epoch_now = window_epoch(now_s)
                for offset in (0, 1):
                    ep = window_epoch(now_s, offset)
                    for asset in self.registry.assets:
                        slug = f"{asset.lower()}-updown-5m-{ep}"
                        if slug in self.markets:
                            continue
                        row = self.registry.active.get(asset) if ep == epoch_now else None
                        if not row or row.get("slug") != slug:
                            try:
                                row = await self._fetch_market(slug)
                            except FetchFailed as exc:
                                self.discovery_errors += 1
                                self.last_discovery_error = str(exc)
                                continue
                        if not row:
                            continue
                        meta = parse_market(row)
                        if meta is None:
                            continue
                        self._ensure_asset(asset)
                        lm = LiveMarket(meta=meta)
                        for tid in (meta.market.yes_token_id, meta.market.no_token_id):
                            lm.books[tid] = LiveBook(tid)
                            self.token_index[tid] = (slug, lm)
                            self.fees.set_token_schedule(tid, meta.fee)
                        self.markets[slug] = lm
                        self.tracker.register(meta)
                        self.stats.windows_seen += 1
                        log.info("discovered %-26s closes %s  fee %.0f%%  liq %.0f",
                                 slug, time.strftime("%H:%M:%S", time.gmtime(meta.market.close_ts / 1000)),
                                 meta.fee.rate * 100, meta.liquidity)
                        self._resub.set()
                self._reap(self.clock.now())
            except Exception:
                log.exception("discovery failed")
            await asyncio.sleep(20)

    def _reap(self, now_ms: float) -> None:
        for slug in [s for s, m in self.markets.items()
                     if now_ms > m.meta.market.close_ts + (30_000 if m.meta.market.strike <= 0 else 240_000)]:
            lm = self.markets.pop(slug)
            # Belt and braces. Whatever the reason a window reaches its reaping
            # still holding risk -- settlement never ran, an exception ate it,
            # a path added later -- letting it go silently costs a position
            # slot permanently. Nothing may leave here still on the risk book.
            if slug in self.risk.open:
                self._release(slug, lm.meta.market, lm, "reaped unsettled")
            for tid in lm.books:
                self.token_index.pop(tid, None)
                self.fees.per_token.pop(tid, None)
            self._subscribed.discard(slug)
            self.engine.features.forget_market(slug)
            self.engine.last.pop(slug, None)
        # price_history was never pruned here. Each market's deque is capped,
        # but the number of MARKETS was not: 244 of them and 154k points had
        # piled up into a 41 MB state file. Writing that takes about a second,
        # which silently capped the dashboard at 1 Hz no matter what
        # state_loop's interval said. The closed window is not lost -- it is
        # already archived to data/charts/ at settlement.
        for slug in [s for s in self.price_history if s not in self.markets]:
            self.price_history.pop(slug, None)

    async def _fetch_market(self, slug: str) -> dict | None:
        """The market row, None if the venue says it does not exist, or
        ``FetchFailed`` if the venue could not be asked. The three answers are
        different: a DNS outage once delisted five assets because the second
        and third were conflated."""
        url = f"{GAMMA_BASE}/markets?slug={urllib.parse.quote(slug)}"
        rows, rtt = await self._get_json(url)
        if rtt is not None:
            self.latency.record_order(rtt)
        if isinstance(rows, list) and rows:
            return rows[0]
        return None

    async def _get_json(self, url: str) -> tuple[object, float | None]:
        def _do():
            req = urllib.request.Request(url, headers={"User-Agent": "troll-poly-bot/0.1"})
            t0 = time.perf_counter()
            with urllib.request.urlopen(req, timeout=12) as r:
                body = json.load(r)
            return body, (time.perf_counter() - t0) * 1000.0
        try:
            body, rtt = await asyncio.to_thread(_do)
        except Exception as exc:
            log.debug("GET %s failed: %s", url, exc)
            self._feed_state("gamma", connected=False, error=f"{type(exc).__name__}: {exc}"[:120])
            raise FetchFailed(f"{type(exc).__name__}: {exc}") from exc
        self._feed_state("gamma", connected=True, message=True)
        return body, rtt

    # ─────────────────────────────── feeds ───────────────────────────────

    def _on_quote(self, q: SpotQuote) -> None:
        self.composite.update(q)
        of = self.orderflow.get(q.asset)
        if of is not None:
            of.on_quote(q.exchange, q.bid_size, q.ask_size, q.ts_ms)
        now = self.clock.now()
        if now - self._last_model_update.get(q.asset, 0.0) < self.cfg.feeds.model_update_interval_ms:
            return
        view = self.composite.view(q.asset, now)
        if view is None:
            return
        self._ensure_asset(q.asset)
        self._last_model_update[q.asset] = now
        self.spot[q.asset] = view.price
        self.spot_ts[q.asset] = now
        self.vol[q.asset].update(view.price, now)
        self.twap[q.asset].update(view.price, now)
        self.engine.features.update_spot(q.asset, view.price, now)
        # order-flow calibration: score now, realised return 10 s / 30 s later
        cal = self.engine.ofi_calibration.get(q.asset)
        if cal is not None:
            lp = math.log(view.price)
            cal.resolve(now, lp)
            if now - self._last_calib_ms.get(q.asset, 0.0) >= 1000.0:
                self._last_calib_ms[q.asset] = now
                cal.record(self.orderflow[q.asset].features(now).get("ofi_score", math.nan), now, lp)

    def _on_trade(self, t: TradeTick) -> None:
        of = self.orderflow.get(t.asset)
        if of is not None:
            of.on_trade(t.notional, t.aggressor_buy, t.ts_ms)

    def _of_point(self, asset: str, now: float) -> dict | None:
        of = self.orderflow.get(asset)
        if of is None:
            return None
        f = of.features(now)
        r3 = lambda v: None if v != v else round(v, 3)  # noqa: E731
        return {"tfi15": r3(f["tfi_15s"]), "obi": r3(f["obi"]), "ofi": r3(f["ofi_score"]),
                "rate": round(f["trade_rate_30s"], 2)}

    async def spot_feed(self) -> None:
        """Run the exchange feeds for the listed assets; restart when the set changes."""
        while not self._stop.is_set():
            assets = self.registry.assets
            if not assets:
                await asyncio.sleep(1)
                continue
            local_stop = asyncio.Event()
            task = asyncio.create_task(run_all(
                assets, self._on_quote, self.clock.now, local_stop, self.exchanges,
                on_latency=self.latency.record_spot, on_trade=self._on_trade,
                health=self.feed_health))
            self._spot_task, self._spot_stop = task, local_stop
            try:
                while not self._stop.is_set() and self.registry.assets == assets:
                    await asyncio.sleep(1)
            finally:
                # reached on a normal stop AND on cancellation: the adapters must
                # never be orphaned, or the process cannot exit
                local_stop.set()
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(asyncio.shield(task), timeout=20.0)

    async def polymarket_feed(self) -> None:
        backoff = RECONNECT_MIN_S
        while not self._stop.is_set():
            tokens = list(self.token_index)
            if not tokens:
                await asyncio.sleep(1)
                continue
            started = time.monotonic()
            try:
                async with websockets.connect(CLOB_WS, open_timeout=20,
                                              ping_interval=10, max_size=8 * 1024 * 1024) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    log.info("polymarket book feed connected (%d tokens)", len(tokens))
                    self._feed_state("polymarket", connected=True)
                    backoff = RECONNECT_MIN_S
                    self._resub.clear()
                    keepalive = asyncio.create_task(self._ping(ws))
                    while not self._stop.is_set():
                        if self._resub.is_set():
                            break
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            continue
                        self._handle_book_msg(raw)
                    keepalive.cancel()
            except Exception as exc:
                if self._stop.is_set():
                    return
                if time.monotonic() - started > 30.0:
                    backoff = RECONNECT_MIN_S
                st = self._feed_state("polymarket", connected=False, error=f"{type(exc).__name__}: {exc}"[:120])
                st.reconnects += 1
                st.backoff_s = backoff
                (log.warning if st.reconnects == 1 or st.reconnects % 10 == 0 else log.debug)(
                    "book feed dropped (%s); retry in %.0fs (attempt %d)", type(exc).__name__, backoff, st.reconnects)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, RECONNECT_MAX_S)

    @staticmethod
    async def _ping(ws) -> None:
        with contextlib.suppress(Exception):
            while True:
                await asyncio.sleep(8)
                await ws.send("PING")

    def _handle_book_msg(self, raw: str) -> None:
        if not raw or raw[0] not in "[{":
            return
        try:
            msgs = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(msgs, dict):
            msgs = [msgs]
        now_ms = self.clock.now()
        self._feed_state("polymarket", message=True)
        for m in msgs:
            ev = m.get("event_type")
            if ev == "book":
                entry = self.token_index.get(str(m.get("asset_id")))
                if entry:
                    entry[1].books[str(m["asset_id"])].apply_snapshot(m)
                    self.latency.record_book(max(0.0, now_ms - _ts(m.get("timestamp"))))
            elif ev == "price_change":
                ts = _ts(m.get("timestamp"))
                self.latency.record_book(max(0.0, now_ms - ts))
                for ch in m.get("price_changes") or []:
                    entry = self.token_index.get(str(ch.get("asset_id")))
                    if entry:
                        entry[1].books[str(ch["asset_id"])].apply_change(ch, ts)

    # ───────────────────────────── strategy ──────────────────────────────

    async def trade_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                log.exception("trade tick failed")
            await asyncio.sleep(0.2)

    def _count_exec(self, code: str) -> None:
        self.exec_rejections[code] = self.exec_rejections.get(code, 0) + 1

    def _tick(self) -> None:
        now = self.clock.now()

        # The venue's strike is the trailing 60s TWAP at the window open. Only
        # offer one once the full window is backed by observations.
        for asset in list(self.twap):
            st = self.twap[asset]
            if not st.fully_covered(now):
                continue
            tw = st.trailing_twap(now)
            if tw is not None:
                self.tracker.on_price(asset, tw, now)
        for meta in self.tracker.poll(now):
            log.info("%s struck at %.6g (60s TWAP at open)", meta.market.slug, meta.market.strike)

        self._sync_latency_profile()

        for res in self.exchange.step(now, self._true_book, self._close_ts):
            self._on_result(res, now)

        self._sample_prices(now)
        self._take_profit(now)

        feed_lag = self.latency.report()["spot_p50"] if self.latency.spot else HOME_BROADBAND.md_spot_base
        for slug, lm in list(self.markets.items()):
            meta = lm.meta
            m = meta.market
            # `exited` markets are done for the window: re-entering the book we
            # just de-risked into would pay the spread twice to undo the exit
            if m.strike <= 0 or lm.settled or lm.exited or now >= m.close_ts:
                continue
            if lm.inflight > 0:
                self._count_exec("ORDER_IN_FLIGHT")
                continue
            asset = m.asset
            sts = self.spot_ts.get(asset)
            view = self.composite.view(asset, now)
            spot_age = None if sts is None else max(0.0, now - sts) + feed_lag

            up = lm.books[m.yes_token_id].to_order_book()
            down = lm.books[m.no_token_id].to_order_book()
            book_age = max(0.0, now - max(up.ts, down.ts)) if (up.ts or down.ts) else None

            ev = self.engine.evaluate(
                meta=meta, book_up=up, book_down=down, spot=view, spot_age_ms=spot_age,
                book_age_ms=book_age, now=now, vol=self.vol.get(asset), twap=self.twap[asset],
                latency=self.strategy_latency, balance=max(self.exchange.balance, 0.0),
                orderflow=self.orderflow[asset].features(now) if asset in self.orderflow else None,
            )
            if ev.p_analytic is not None and ev.p_market is not None:
                self.agreement.setdefault(asset, Agreement()).add(ev.p_analytic, ev.p_market, ev.seconds_left)
            if not ev.tradeable:
                continue

            # Revalidate against the freshest book right before sending; the
            # book can move between evaluation and submission. Never chase.
            fresh = lm.books[ev.token_id].to_order_book()
            why = self.engine.revalidate(ev, fresh)
            if why is not None:
                self._count_exec(f"REVALIDATE_{why.value}")
                continue

            order = self.engine.to_order(ev)
            self.fair_by_order[order.order_id] = {
                "slug": slug, "asset": asset, "epoch": lm.epoch, "token_id": ev.token_id,
                "side": ev.side, "p_used": ev.p_used, "p_analytic": ev.p_analytic,
                "p_market": ev.sides[0].p_market if ev.side == "UP" else ev.sides[1].p_market,
                "net_edge": ev.net_edge, "confidence": ev.confidence,
                "regime": ev.regime.label if ev.regime else "",
                "secs_left": ev.seconds_left, "expected": ev.expected_price,
                "fee_rate": meta.fee.rate,
                "ofi": None if math.isnan(ev.ofi_score) else round(ev.ofi_score, 3),
                "ofi_drift_bps": round(ev.ofi_drift_bps, 3),
            }
            self.exchange.submit(order, now)
            lm.submitted += 1
            lm.inflight += 1
            log.info("SEND  %-26s %-4s %6.2f sh @ %.2f  p %.3f (mkt %.3f)  net edge %+.3f  conf %.2f  "
                     "%s  ofi %+.2f (%+.1f bps)  %5.1fs left",
                     slug, ev.side, ev.size, ev.limit_price, ev.p_used if ev.side == "UP" else 1 - ev.p_used,
                     self.fair_by_order[order.order_id]["p_market"], ev.net_edge, ev.confidence,
                     ev.regime.label if ev.regime else "",
                     0.0 if math.isnan(ev.ofi_score) else ev.ofi_score, ev.ofi_drift_bps, ev.seconds_left)

    def _exit_candidate(self, lm: LiveMarket, token_id: str, now: float) -> dict | None:
        """Should we sell this token now, and at what price?

        Returns the exit's economics, or None with the reason counted. The
        price we test is the best BID -- the only price a taker can actually
        sell into -- and the profit is computed after BOTH fees, entry and
        exit, so a "profitable" exit is one the ledger will agree with.
        """
        cfg = self.cfg.engine
        pos = self.exchange.positions.get(token_id)
        if pos is None or pos.shares <= EXIT_EPS:
            return None
        size = pos.shares
        book = lm.books[token_id].to_order_book() if token_id in lm.books else None
        bid = book.best_bid if book is not None else None
        if bid is None:
            self._count_exec("EXIT_NO_BID")
            return None
        entry = pos.cost_basis / size                 # average price paid
        rise = bid - entry
        if cfg.take_profit_enabled and rise >= cfg.take_profit_delta:
            kind = "TAKE_PROFIT"
        elif cfg.stop_loss_enabled and rise <= -cfg.stop_loss_delta:
            kind = "STOP_LOSS"
        else:
            return None
        # FOK is all-or-nothing: without the depth to clear the whole position
        # at or above the touch the order is rejected, so do not send it.
        depth = sum(lvl.size for lvl in book.bids if lvl.price >= bid - EXIT_EPS)
        if depth + EXIT_EPS < size:
            self._count_exec("EXIT_THIN_BID")
            return None
        fee = self.exchange.fees.charge(bid, size, token_id)
        # full exit, so the realised PnL is everything the position ever cost
        pnl = bid * size - fee - pos.cost_basis - pos.fees_paid
        # A take-profit that does not clear both fees is not a profit. A stop
        # is allowed to realise a loss -- that is the entire point of it.
        if kind == "TAKE_PROFIT" and pnl / size < cfg.take_profit_min_net:
            self._count_exec("EXIT_FEE_EATS_IT")
            return None
        return {"size": size, "bid": bid, "entry": entry, "pnl": pnl,
                "fee": fee, "kind": kind}

    def _take_profit(self, now: float) -> None:
        """Sell anything that has risen far enough to be worth banking.

        Runs before the entry scan so a position is never added to in the same
        tick it is being closed in.
        """
        cfg = self.cfg.engine
        if not (cfg.take_profit_enabled or cfg.stop_loss_enabled):
            return
        for slug, lm in list(self.markets.items()):
            m = lm.meta.market
            if lm.settled or lm.exited or lm.inflight > 0:
                continue
            if (m.close_ts - now) / 1000.0 <= cfg.take_profit_min_secs_left:
                continue
            for token_id, side in ((m.yes_token_id, "UP"), (m.no_token_id, "DOWN")):
                ex = self._exit_candidate(lm, token_id, now)
                if ex is None:
                    continue
                order = Order(
                    token_id=token_id, side=Side.SELL, price=ex["bid"], size=ex["size"],
                    tif=TimeInForce.FOK, expected_price=ex["bid"],
                    tag=f"{ex['kind']} {side} entry={ex['entry']:.3f} bid={ex['bid']:.3f}",
                )
                self.fair_by_order[order.order_id] = {
                    "slug": slug, "asset": m.asset, "epoch": lm.epoch,
                    "token_id": token_id, "side": side, "exit": True,
                    "kind": ex["kind"], "entry": round(ex["entry"], 4),
                    "secs_left": round((m.close_ts - now) / 1000.0, 1),
                }
                self.exchange.submit(order, now)
                lm.inflight += 1
                log.info("%-11s %-26s %-4s %6.2f sh  entry %.3f -> bid %.3f  "
                         "expect %+.2f  %5.1fs left",
                         ex["kind"], slug, side, ex["size"], ex["entry"], ex["bid"],
                         ex["pnl"], (m.close_ts - now) / 1000.0)
                break                       # one exit order per market per tick

    def _book_exit(self, info: dict, fills: list, now: float) -> None:
        """Realise an early exit: PnL, risk release, stats and the ledger.

        Deliberately NOT risk.on_fill: this reduces exposure. on_settle is what
        pops the open position and books the result, which is the same thing
        settlement does -- an exit is simply a settlement we chose the price of.
        """
        slug = info.get("slug", "")
        lm = self.markets.get(slug)
        token_id = info.get("token_id", "")
        pos = self.exchange.positions.get(token_id)
        size = sum(f.size for f in fills)
        proceeds = sum(f.price * f.size for f in fills)
        # everything the position ever cost is still on it; after a full exit
        # what remains is exactly the realised result
        pnl = -(pos.cost_basis + pos.fees_paid) if pos is not None else 0.0
        if pos is not None:
            pos.cost_basis = 0.0
            pos.fees_paid = 0.0
            pos.shares = 0.0
        asset = info.get("asset", "")
        epoch = int(info.get("epoch", 0))
        self.risk.on_settle(slug, pnl, self.clock.now() / 1000.0)
        self.stats.settled += 1
        self.stats.realised_pnl += pnl
        self.pnl_by_asset[asset] = self.pnl_by_asset.get(asset, 0.0) + pnl
        self.epoch_pnl[epoch] = self.epoch_pnl.get(epoch, 0.0) + pnl
        if pnl > 0:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        if lm is not None:
            lm.exited = True
            lm.pnl += pnl
        avg = proceeds / size if size else 0.0
        log.info("SOLD  %-26s %-4s %6.2f sh @ %.3f  pnl %+.2f  balance %.2f",
                 slug, info.get("side", ""), size, avg, pnl, self.exchange.balance)
        self._ledger_append({
            "ts": now, "event": "fill", "action": "SELL", **{
                k: v for k, v in info.items() if k != "exit"},
            "price": round(avg, 4), "size": round(size, 4),
            "fee": round(sum(f.fee for f in fills), 4),
            "cost": round(-proceeds, 4),      # a credit, not a cost
        })
        self._ledger_append({
            "ts": now, "event": "settle", "slug": slug, "asset": asset,
            "epoch": epoch, "exited": True, "pnl": round(pnl, 4),
            "balance": round(self.exchange.balance, 4),
        })

    # 200ms sampling, bounded to the window plus a 60s lead-in.
    HISTORY_INTERVAL_MS = 200.0
    HISTORY_POINTS = 2000

    @staticmethod
    def _token_price(book) -> float | None:
        """A usable price for one token even when its book is one-sided.

        Once a window is decided the venue books go one-sided in a
        complementary way (winner: bids only, loser: asks only). The side that
        IS quoted is the marketable price and the two sum to 1.00.
        """
        if book is None:
            return None
        if book.best_bid is not None and book.best_ask is not None:
            return book.mid
        if book.best_ask is not None:
            return book.best_ask
        if book.best_bid is not None:
            return book.best_bid
        return None

    @staticmethod
    def _recover_mids(up_mid, down_mid):
        """Recover a missing token price from the other token (they sum to 1)."""
        if up_mid is None and down_mid is not None:
            return 1.0 - down_mid, down_mid
        if down_mid is None and up_mid is not None:
            return up_mid, 1.0 - up_mid
        return up_mid, down_mid

    def _sample_prices(self, now: float) -> None:
        """Snapshot every live market's token prices, touch, depth, the
        composite spot and each exchange's price -- the data collection system.
        One time axis for all of it, so nothing can drift apart."""
        if now - self._last_hist_ms < self.HISTORY_INTERVAL_MS:
            return
        self._last_hist_ms = now
        r4 = lambda v: round(v, 4) if v is not None else None  # noqa: E731
        for slug, lm in self.markets.items():
            m = lm.meta.market
            if now < m.open_ts - 60_000 or now > m.close_ts + 30_000:
                continue
            up = lm.books[m.yes_token_id].to_order_book()
            dn = lm.books[m.no_token_id].to_order_book()
            um, dm = self._recover_mids(self._token_price(up), self._token_price(dn))
            if um is None or dm is None:
                continue
            view = self.composite.view(m.asset, now)
            spot = self.spot.get(m.asset)
            self.price_history.setdefault(slug, deque(maxlen=self.HISTORY_POINTS)).append({
                "t": round(now), "up": round(um, 4), "down": round(dm, 4),
                "spot": round(spot, 6) if spot is not None else None,
                "left": round(m.seconds_remaining(now), 1),
                "ub": r4(up.best_bid), "ua": r4(up.best_ask), "db": r4(dn.best_bid), "da": r4(dn.best_ask),
                # depth within 2 ticks of each touch, in shares
                "ubd": round(_depth_near_touch(up.bids, up.best_bid, ask=False), 1),
                "uad": round(_depth_near_touch(up.asks, up.best_ask, ask=True), 1),
                "dbd": round(_depth_near_touch(dn.bids, dn.best_bid, ask=False), 1),
                "dad": round(_depth_near_touch(dn.asks, dn.best_ask, ask=True), 1),
                "srcs": {k: round(v, 6) for k, v in view.sources.items()} if view else None,
                "of": self._of_point(m.asset, now),
            })

    def _sync_latency_profile(self) -> None:
        rep = self.latency.report()
        if rep["n_order"] < EmpiricalLatency.MIN_SAMPLES:
            return
        rtt = rep["order_rtt_p50"]
        if abs(self.strategy_latency.round_trip_ms() - rtt) >= 25.0:
            self.strategy_latency = replace(
                HOME_BROADBAND, name="measured",
                submit_base=rtt / 2.0, submit_jitter=0.0, ack_base=rtt / 2.0, ack_jitter=0.0,
                md_spot_base=rep["spot_p50"], md_spot_jitter=0.0,
                md_book_base=rep["book_p50"], md_book_jitter=0.0,
            )
        # Staleness gates catch a STALLED feed, not a slow one: scale to the
        # measured feed with a floor so a dead feed is still caught.
        if rep["n_spot"] >= EmpiricalLatency.MIN_SAMPLES:
            self.cfg.engine.max_spot_age_ms = max(self._base_spot_age, rep["spot_p95"] * 2.5)
        if rep["n_book"] >= EmpiricalLatency.MIN_SAMPLES:
            self.cfg.engine.max_book_age_ms = max(self._base_book_age, rep["book_p95"] * 2.5)

    def _true_book(self, token_id: str) -> OrderBook | None:
        entry = self.token_index.get(token_id)
        return entry[1].books[token_id].to_order_book() if entry else None

    def _close_ts(self, token_id: str) -> float:
        entry = self.token_index.get(token_id)
        return entry[1].meta.market.close_ts if entry else 0.0

    def _on_result(self, res, now: float) -> None:
        info = self.fair_by_order.get(res.order.order_id, {})
        lm = self.markets.get(info.get("slug", ""))
        if lm is not None:
            lm.inflight = max(0, lm.inflight - 1)
        if res.is_rejected:
            prefix = "EXIT_" if info.get("exit") else ""
            self._count_exec(f"{prefix}{res.reject_reason.value.upper()}")
            log.info("REJECT %-25s %s%s", info.get("slug", "?"), prefix,
                     res.reject_reason.value)
            return
        if info.get("exit") and res.fills:
            # one round trip closed: booked whole, not fill by fill
            self._book_exit(info, list(res.fills), now)
            return
        for f in res.fills:
            self.risk.on_fill(info.get("slug", ""), info.get("asset", ""), int(info.get("epoch", 0)),
                              info.get("side", ""), f.price * f.size, f.size)
            log.info("FILL  %-26s %-4s %6.2f sh @ %.3f  fee %.4f  slip %+.3f  rtt %.0fms",
                     info.get("slug", "?"), info.get("side", ""), f.size, f.price, f.fee,
                     f.slippage, f.round_trip_ms)
            row = {
                "ts": now, "event": "fill", "action": "BUY", **info,
                "price": f.price, "size": f.size, "fee": f.fee,
                "slippage": f.slippage, "round_trip_ms": f.round_trip_ms,
                "cost": round(f.price * f.size, 4),
            }
            self._ledger_append(row)

    # ──────────────────────────── settlement ─────────────────────────────

    #: Resolved rows vanish from Gamma within ~10 minutes, and the outcome
    #: is not always published at close+40s. Retry a few times before
    #: falling back to our own proxy, and only then give up.
    SETTLE_DELAY_MS = 40_000.0
    SETTLE_RETRY_MS = 30_000.0
    SETTLE_MAX_ATTEMPTS = 5

    async def settle_loop(self) -> None:
        while not self._stop.is_set():
            now = self.clock.now()
            for slug, lm in list(self.markets.items()):
                m = lm.meta.market
                if lm.settled or now < m.close_ts + self.SETTLE_DELAY_MS or now < lm.next_settle_ms:
                    continue
                if m.strike <= 0:
                    lm.settled = True                     # never priced: nothing to settle or archive
                    continue
                try:
                    await self._settle(slug, lm)
                except Exception:
                    log.exception("settlement failed for %s", slug)
                    lm.settled = True
            await asyncio.sleep(5)

    async def _settle(self, slug: str, lm: LiveMarket) -> None:
        m = lm.meta.market
        lm.settle_attempts += 1
        try:
            venue_up = await self._venue_outcome(slug)
        except FetchFailed as exc:
            venue_up = None
            self.last_discovery_error = str(exc)
        ours_up = None
        tw = self.twap[m.asset].trailing_twap(m.close_ts) if m.asset in self.twap else None
        if tw is not None and m.strike > 0:
            ours_up = tw >= m.strike                      # ties resolve Up
        # read before settle_market() moves anything, so both the resolved and
        # the unresolvable path below can tell whether we were actually in it
        had_position = any(
            abs(self.exchange.positions[t].shares) > 1e-9
            for t in (m.yes_token_id, m.no_token_id) if t in self.exchange.positions)
        if venue_up is None:
            if lm.settle_attempts < self.SETTLE_MAX_ATTEMPTS:
                lm.next_settle_ms = self.clock.now() + self.SETTLE_RETRY_MS
                log.info("%s: venue outcome not available yet (attempt %d); retrying",
                         slug, lm.settle_attempts)
                return
            if ours_up is None:
                lm.settled = True
                self.stats.unresolvable += 1
                log.warning("%s: no outcome after %d attempts; positions left unsettled",
                            slug, lm.settle_attempts)
                # The window is over and we will never price it. Flatten it at
                # its last mark and hand the risk budget back -- leaving the
                # position open is what wedged the bot for twenty hours -- and
                # write the ledger row, without which the dashboard has no way
                # to learn the market ended and every fill on it reads "open".
                if had_position:
                    self._release(slug, m, lm, "no venue outcome")
                return
        lm.settled = True
        if venue_up is not None and ours_up is not None and venue_up != ours_up:
            self.stats.basis_disagreements += 1
            log.warning("%s: BASIS DISAGREEMENT venue=%s ours=%s", slug,
                        "UP" if venue_up else "DOWN", "UP" if ours_up else "DOWN")
        won_up = venue_up if venue_up is not None else ours_up

        condition_of = getattr(self.exchange, "condition_of", None)
        if condition_of is not None:                       # live: lets the venue adapter redeem
            condition_of[m.yes_token_id] = m.condition_id
            condition_of[m.no_token_id] = m.condition_id
        pnl = self.exchange.settle_market(m.yes_token_id, won=bool(won_up))
        pnl += self.exchange.settle_market(m.no_token_id, won=not won_up)

        self._archive_chart(slug, lm, won_up, pnl, venue_up)
        lm.outcome = "UP" if won_up else "DOWN"
        # += so an early exit's realised PnL survives; for a market held to
        # settlement lm.pnl is still 0.0 here, so this is the old assignment
        lm.pnl += pnl
        if lm.submitted:
            self.stats.windows_traded += 1
        if had_position:
            self.risk.on_settle(slug, pnl, self.clock.now() / 1000.0)
            self.stats.settled += 1
            self.stats.realised_pnl += pnl
            self.pnl_by_asset[m.asset] = self.pnl_by_asset.get(m.asset, 0.0) + pnl
            self.epoch_pnl[lm.epoch] = self.epoch_pnl.get(lm.epoch, 0.0) + pnl
            if pnl > 0:
                self.stats.wins += 1
            else:
                self.stats.losses += 1
            log.info("SETTLE %-25s %-4s  pnl %+.2f  balance %.2f", slug, lm.outcome, pnl, self.exchange.balance)
            self._ledger_append({
                "ts": self.clock.now(), "event": "settle", "slug": slug, "asset": m.asset,
                "epoch": lm.epoch, "strike": m.strike, "venue_up": venue_up, "ours_up": ours_up,
                "pnl": round(pnl, 4), "balance": round(self.exchange.balance, 4),
            })

    def _archive_chart(self, slug: str, lm: LiveMarket, won_up: bool | None, pnl: float,
                       venue_up: bool | None) -> None:
        """Write a closed window's path to data/charts/<slug>.json (the dataset)."""
        pts = self.price_history.get(slug)
        if not pts:
            return
        m = lm.meta.market
        fills = [r for r in self.ledger if r.get("event") == "fill" and r.get("slug") == slug]
        try:
            self.chart_dir.mkdir(parents=True, exist_ok=True)
            (self.chart_dir / f"{slug}.json").write_text(json.dumps({
                "slug": slug, "asset": m.asset, "strike": m.strike,
                "open_ts": m.open_ts, "close_ts": m.close_ts,
                "outcome": "UP" if won_up else "DOWN",
                "outcome_source": "venue" if venue_up is not None else "proxy",
                "pnl": round(pnl, 4), "fills": fills,
                "question": lm.meta.question, "fee": {"rate": lm.meta.fee.rate, "exponent": lm.meta.fee.exponent},
                "exchanges": list(self.exchanges),
                "points": list(pts),
            }, separators=(",", ":")), encoding="utf-8")
        except OSError:
            log.debug("could not archive chart for %s", slug)

    async def _venue_outcome(self, slug: str) -> bool | None:
        row = await self._fetch_market(slug)
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

    # ───────────────────────────── reporting ─────────────────────────────

    def _append_log(self, row: dict) -> None:
        try:
            with self.trade_log.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            log.debug("could not append to trade log")

    def _abandon_position(self, m) -> float:
        """Flatten an unresolvable window at its last mark. Returns the PnL.

        There is no outcome to settle against, so the position is valued at the
        last price the book showed. That is the least invented number available:
        assuming a win overstates equity and assuming a total loss books a loss
        that probably did not happen. It is still an estimate, which is why the
        caller keeps it out of the evidence statistic.
        """
        total = 0.0
        if self.is_live:
            # nothing is sold here; the tokens stay in the account and settle
            # when the venue resolves. Only the risk budget is released.
            log.warning("%s: unresolved on a REAL account; tokens left in place, no PnL booked", m.slug)
            return 0.0
        for tid in (m.yes_token_id, m.no_token_id):
            pos = self.exchange.positions.get(tid)
            if pos is None or abs(pos.shares) <= EXIT_EPS:
                continue
            mark = self._mark(tid)
            mark = 0.0 if mark is None else max(0.0, min(1.0, mark))
            proceeds = pos.shares * mark
            total += proceeds - pos.cost_basis - pos.fees_paid
            self.exchange.balance += proceeds
            pos.shares = 0.0
            pos.cost_basis = 0.0
            pos.fees_paid = 0.0
        return total

    def _release(self, slug: str, m, lm: LiveMarket | None, why: str) -> None:
        """Give up on a window: flatten it, free its risk budget, say so.

        The risk budget is the part that matters. A position that is never
        popped from the risk manager consumes one of `max_concurrent_positions`
        FOREVER -- ten unresolvable windows in one ten-minute venue outage
        silently took the bot to its position cap and it bought nothing for the
        next twenty hours while looking perfectly healthy.
        """
        pnl = self._abandon_position(m)
        self.risk.on_settle(slug, pnl, self.clock.now() / 1000.0)
        self.stats.realised_pnl += pnl
        self.pnl_by_asset[m.asset] = self.pnl_by_asset.get(m.asset, 0.0) + pnl
        if lm is not None:
            lm.pnl += pnl
            lm.settled = True
        # Deliberately NOT counted in wins/losses, stats.settled or epoch_pnl:
        # a marked-to-market guess is not evidence of edge either way.
        log.warning("ABANDON %-24s %s  marked %+.2f  balance %.2f",
                    slug, why, pnl, self.exchange.balance)
        self._ledger_append({
            "ts": self.clock.now(), "event": "settle", "slug": slug,
            "asset": m.asset, "epoch": lm.epoch if lm is not None else 0,
            "unresolved": True, "reason": why, "marked": True,
            "pnl": round(pnl, 4), "balance": round(self.exchange.balance, 4),
        })

    def _ledger_append(self, row: dict) -> None:
        """Record one event to disk and to the in-memory tail the dashboard reads."""
        # Stamp who wrote it. A paper bot and a real-money bot run out of one
        # directory here and share this file, and without the tag their trades
        # read as one interleaved history.
        row.setdefault("mode", self.mode_label)
        row.setdefault("live", self.is_live)
        self._append_log(row)
        self.ledger.append(row)
        del self.ledger[:-200]
        if row.get("event") == "settle":
            # the run ledger is cleared on restart; the earnings record is not,
            # and it is tagged so paper money never lands in a real-money total
            earnings_log.append({
                "ts": row.get("ts"), "pnl": row.get("pnl"), "slug": row.get("slug", ""),
                "asset": row.get("asset", ""), "exited": bool(row.get("exited")),
                "mode": self.mode_label, "live": self.is_live,
            })

    def _reset_trade_log(self) -> None:
        """Start each run with an empty trade log.

        A restart resets the balance to ``starting_balance``, so carrying the
        previous run's fills forward makes the dashboard's history describe PnL
        that the current run's equity does not account for. The per-window
        recordings in ``data/charts/`` are the durable research record and are
        left alone. Pass ``reset_history=False`` (``--keep-history``) to append
        across restarts instead.
        """
        try:
            self.trade_log.write_text("", encoding="utf-8")
        except OSError:
            log.debug("could not reset trade log")

    def evidence(self) -> dict:
        """Is there edge yet? Per-EPOCH realised PnL, because every asset in a
        window is the same bet. Nothing below |t| ~ 2 should be believed."""
        vals = list(self.epoch_pnl.values())
        n = len(vals)
        if n == 0:
            return {"epochs": 0}
        mean = statistics.fmean(vals)
        se = statistics.stdev(vals) / math.sqrt(n) if n > 1 else float("nan")
        return {
            "epochs": n, "mean_per_epoch": round(mean, 4),
            "se": None if n < 2 else round(se, 4),
            "t": None if n < 2 or se == 0 else round(mean / se, 2),
            "positive_epochs": sum(1 for v in vals if v > 0),
            "total": round(sum(vals), 4),
        }

    @property
    def mode_label(self) -> str:
        if not self.is_live:
            return "live-paper"
        return "LIVE-ARMED" if getattr(self.exchange, "armed", False) else "live-dry-run"

    def snapshot(self) -> dict:
        lr = self.exchange.latency_report()
        equity = self.exchange.equity(self._mark)
        now = self.clock.now()
        eng = self.engine.snapshot()
        skips = dict(eng["rejections"])
        for k, v in self.exec_rejections.items():
            skips[k] = skips.get(k, 0) + v
        return {
            "mode": self.mode_label,
            "live": self.exchange.snapshot() if self.is_live else None,
            "started": self.stats.started,
            "uptime_s": round(time.time() - self.stats.started, 1),
            "assets": list(self.assets),
            "exchanges": list(self.exchanges),
            "balance": round(self.exchange.balance, 4),
            "equity": round(equity, 4),
            "starting_balance": self.cfg.starting_balance,
            "pnl": round(equity - self.cfg.starting_balance, 4),
            "realised_pnl": round(self.stats.realised_pnl, 4),
            "unrealised_pnl": round(equity - self.exchange.balance, 4),
            "pnl_by_asset": {a: round(v, 4) for a, v in sorted(self.pnl_by_asset.items())},
            "open_positions": {
                t: round(p.shares, 2)
                for t, p in self.exchange.positions.items() if abs(p.shares) > 1e-9
            },
            "live_markets": [
                {
                    "slug": s, "asset": lm.meta.market.asset, "strike": lm.meta.market.strike,
                    "secs_left": round(lm.meta.market.seconds_remaining(now), 1),
                    "submitted": lm.submitted, "fee_rate": lm.meta.fee.rate,
                    "status": self._market_status(lm, now),
                    "liquidity": round(lm.meta.liquidity, 0),
                    "up_shares": round(self.exchange.positions[lm.meta.market.yes_token_id].shares, 2)
                    if lm.meta.market.yes_token_id in self.exchange.positions else 0.0,
                    "down_shares": round(self.exchange.positions[lm.meta.market.no_token_id].shares, 2)
                    if lm.meta.market.no_token_id in self.exchange.positions else 0.0,
                    "decision": (eng["last"].get(s) or {}),
                }
                for s, lm in sorted(self.markets.items()) if not lm.settled
            ],
            "closed_markets": [
                {
                    "slug": s, "asset": lm.meta.market.asset, "strike": lm.meta.market.strike,
                    "outcome": lm.outcome, "pnl": round(lm.pnl, 4), "traded": lm.submitted,
                }
                for s, lm in sorted(self.markets.items()) if lm.outcome is not None
            ],
            "price_history": {slug: list(pts) for slug, pts in self.price_history.items() if len(pts) >= 2},
            "ledger": self.ledger[-60:],
            "stats": {
                "windows_seen": self.stats.windows_seen, "windows_traded": self.stats.windows_traded,
                "settled": self.stats.settled, "wins": self.stats.wins, "losses": self.stats.losses,
                "basis_disagreements": self.stats.basis_disagreements,
                "unresolvable": self.stats.unresolvable, "realised_pnl": round(self.stats.realised_pnl, 4),
            },
            "evidence": self.evidence(),
            "feeds": {n: st.as_dict(now) for n, st in sorted(self.feed_health.items())},
            "discovery": {
                "errors": self.discovery_errors, "last_error": self.last_discovery_error,
                "unresolved": sorted(self.registry.unresolved),
                "probe_failures": self.registry.probe_failures,
            },
            "orderflow": eng.get("orderflow", {}),
            "orderflow_now": {a: self._of_point(a, now) for a in self.assets},
            "risk": self.risk.snapshot(),
            "orders": {k: round(v, 4) for k, v in lr.items()},
            "measured_latency_ms": {k: round(v, 1) for k, v in self.latency.report().items()},
            "reaction_lag_ms": round(self.latency.reaction_lag_ms, 1),
            "clock_offset_ms": round(self.clock.offset_ms, 1),
            "model_vs_market": {a: g.report() for a, g in self.agreement.items()},
            "regime": eng["regime"],
            "rejections": eng["rejections"],
            "exec_rejections": dict(self.exec_rejections),
            "sigma_bps_per_sec": {a: round(v.sigma_per_sec * 1e4, 3) for a, v in self.vol.items()},
            "variance_ratio": {a: round(getattr(v, "variance_ratio", 1.0), 2) for a, v in self.vol.items()},
            "vol_annualised_pct": {a: round(v.annualised() * 100, 1) for a, v in self.vol.items()},
            "skips": skips,
            "gates": {
                "max_spot_age_ms": round(self.cfg.engine.max_spot_age_ms, 1),
                "max_book_age_ms": round(self.cfg.engine.max_book_age_ms, 1),
                "min_net_edge": self.cfg.engine.min_net_edge,
                "market_blend": self.cfg.engine.market_blend,
                "take_profit": (self.cfg.engine.take_profit_delta
                                if self.cfg.engine.take_profit_enabled else 0.0),
                "stop_loss": (self.cfg.engine.stop_loss_delta
                              if self.cfg.engine.stop_loss_enabled else 0.0),
                "trade_window_s": [self.cfg.engine.trade_window_start_s, self.cfg.engine.trade_window_end_s],
            },
            "controls": {
                "scope": self.control_scope,
                "values": current_values(self.cfg, self.exchange),
                "applied": self.control_applied[-8:],
                "applied_at": self.control_at,
                "errors": self.control_errors[:8],
            },
            "spot": {a: round(p, 6) for a, p in self.spot.items()},
            "spot_sources": {
                a: (lambda v: None if v is None else {
                    "n": v.n_sources, "deviation_bps": round(v.deviation_bps, 2),
                    "sources": {k: round(p, 6) for k, p in v.sources.items()}})(self.composite.view(a, now))
                for a in self.assets
            },
            "exchange_counts": dict(self.composite.counts),
            "twap_coverage_s": {a: round(st.coverage_s(now), 1) for a, st in self.twap.items()},
            "twap60": {a: (lambda v: round(v, 6) if v else None)(st.trailing_twap(now)) for a, st in self.twap.items()},
        }

    @staticmethod
    def _market_status(lm: LiveMarket, now: float) -> str:
        m = lm.meta.market
        if m.strike > 0:
            return "closing" if m.seconds_remaining(now) < 60 else "live"
        if now < m.open_ts:
            return "pending"
        return "missed open"

    def _mark(self, token_id: str) -> float | None:
        book = self._true_book(token_id)
        if book is not None and book.mid is not None:
            self._last_mark[token_id] = book.mid
            return book.mid
        if token_id in self._last_mark:
            return self._last_mark[token_id]
        pos = self.exchange.positions.get(token_id)
        if pos is not None and abs(pos.shares) > 1e-9:
            return max(0.0, min(1.0, pos.cost_basis / pos.shares))
        return None

    async def clock_loop(self, every: float = 300.0) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(every)
            await self.clock.resync(samples=3)

    def _write_state(self, tmp, snap) -> None:
        """Serialise and swap in. Atomic, so a reader never sees half a file.

        orjson because this runs ten times a second and is ~8x faster than the
        stdlib on this payload; OPT_NON_STR_KEYS because epoch_pnl is keyed by
        int epoch, which json coerces to a string and orjson otherwise refuses.
        Falls back rather than losing a write if anything unexpected appears.
        """
        try:
            blob = orjson.dumps(snap, option=orjson.OPT_NON_STR_KEYS)
        except TypeError:
            blob = json.dumps(snap, separators=(",", ":")).encode()
        tmp.write_bytes(blob)
        tmp.replace(self.state_path)

    async def control_loop(self, every: float = 2.0) -> None:
        """Re-read the dashboard's tunables whenever the file changes.

        Polling a small file beats a socket here: the dashboard is not
        necessarily our parent, either process may restart independently, and
        a file that is a second stale costs nothing at a 5-minute cadence.
        """
        while not self._stop.is_set():
            try:
                if self.controls.changed():
                    self._apply_controls()
            except Exception as exc:                      # noqa: BLE001
                log.warning("control file could not be applied: %s", exc)
            await asyncio.sleep(every)

    def _apply_controls(self) -> None:
        values, errors = self.controls.merged(self.control_scope)
        changed = apply_to(values, self.cfg, self.exchange)
        # the staleness gates are auto-widened from measured feed latency with
        # a floor; an operator setting them means setting that floor, or the
        # next latency report would immediately overwrite the new value
        if "engine.max_spot_age_ms" in values:
            self._base_spot_age = values["engine.max_spot_age_ms"]
        if "engine.max_book_age_ms" in values:
            self._base_book_age = values["engine.max_book_age_ms"]
        self.control_errors = errors
        if changed:
            self.control_applied = changed
            self.control_at = time.time()
            for line in changed:
                log.warning("CONTROL  %s", line)
        for problem in errors:
            log.warning("control file: %s", problem)

    async def state_loop(self, every: float = 0.1) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        while not self._stop.is_set():
            await asyncio.sleep(every)
            snap = self.snapshot()
            try:
                await asyncio.to_thread(self._write_state, tmp, snap)
            except OSError:
                pass

    async def report_loop(self, every: float = 30.0) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(every)
            snap = self.snapshot()
            lat = snap["measured_latency_ms"]
            ev = snap["evidence"]
            log.info(
                "STATUS equity %.2f (%+.2f) | assets %s | live %d | settled %d (%dW/%dL) | "
                "orders %d filled %d | spot lag %.0fms book %.0fms rtt %.0fms | evidence n=%s t=%s",
                snap["equity"], snap["pnl"], ",".join(snap["assets"]), len(snap["live_markets"]),
                snap["stats"]["settled"], snap["stats"]["wins"], snap["stats"]["losses"],
                int(snap["orders"].get("orders_submitted", 0)), int(snap["orders"].get("orders_filled", 0)),
                lat["spot_p50"], lat["book_p50"], lat["order_rtt_p50"], ev.get("epochs"), ev.get("t"),
            )
            top = sorted(snap["skips"].items(), key=lambda kv: -kv[1])[:6]
            log.info("  why not trading: %s", ", ".join(f"{k} {v}" for k, v in top) or "no evaluations yet")
            down = [f"{n} ({v['last_error'] or 'no messages'})" for n, v in snap["feeds"].items() if not v["connected"]]
            if down:
                log.warning("  FEEDS DOWN: %s", "; ".join(down))
            if snap["discovery"]["unresolved"]:
                log.warning("  assets unanswered by the venue: %s", ", ".join(snap["discovery"]["unresolved"]))
            for asset, rep in snap["model_vs_market"].items():
                if rep:
                    log.info("  MODEL vs MARKET %-5s bias %+.3f  mean|diff| %.3f  n=%d  sigma %.2f bps/s  regime %s",
                             asset, rep["mean_bias"], rep["mean_abs"], rep["n"],
                             snap["sigma_bps_per_sec"].get(asset, 0.0), snap["regime"].get(asset, "?"))

    # ─────────────────────────────── driver ──────────────────────────────

    async def run(self) -> None:
        if self.is_live:
            log.warning("%s | assets %s | bankroll $%.2f | %s",
                        self.mode_label, ", ".join(self.registry.pinned) or "ALL LISTED",
                        self.cfg.starting_balance,
                        "REAL ORDERS WILL BE POSTED" if self.mode_label == "LIVE-ARMED"
                        else "dry run: orders signed, never posted")
        else:
            log.info("live paper trading | assets %s | exchanges %s | balance $%.2f | PAPER FILLS ONLY",
                     ", ".join(self.registry.pinned) or "ALL LISTED", ", ".join(self.exchanges),
                     self.cfg.starting_balance)
        log.info("resolution is a Chainlink 60s TWAP; the composite spot is a PROXY -- watch basis_disagreements")
        await self.clock.resync()
        log.info("clock offset %+.0fms vs exchange (rtt %.0fms)%s",
                 self.clock.offset_ms, self.clock.rtt_ms,
                 "  <-- your system clock is off; consider w32tm /resync"
                 if abs(self.clock.offset_ms) > 250 else "")
        tasks = [
            asyncio.create_task(self.clock_loop()),
            asyncio.create_task(self.discover()),
            asyncio.create_task(self.spot_feed()),
            asyncio.create_task(self.polymarket_feed()),
            asyncio.create_task(self.trade_loop()),
            asyncio.create_task(self.settle_loop()),
            asyncio.create_task(self.state_loop()),
            asyncio.create_task(self.report_loop()),
            asyncio.create_task(self.control_loop()),
        ]
        if hasattr(self.exchange, "run"):
            tasks.append(asyncio.create_task(self.exchange.run(self._stop)))
        try:
            await self._stop.wait()
        finally:
            if self._spot_stop is not None:
                self._spot_stop.set()
            if self._spot_task is not None:
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(self._spot_task, timeout=20.0)
            for t in tasks:
                t.cancel()
            for t in tasks:
                with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                    await asyncio.wait_for(t, timeout=15.0)

    def stop(self) -> None:
        self._stop.set()
