"""Latency-aware paper matching engine.

Model
-----
Three clocks matter and they are all different:

    decision_ts   the strategy looked at a book that was already
                  ``md_book`` ms old and a spot price ``md_spot`` ms old
    exchange_ts   = decision_ts + submit_latency
                  the order lands and matches against the book AS IT IS THEN,
                  not as the strategy saw it
    ack_ts        = exchange_ts + ack_latency
                  the strategy finally learns what happened

The strategy is handed delayed views (see feeds.delayed). This engine is handed
the truth. Everything the simulator teaches you lives in that gap.

Deliberate pessimism
--------------------
Where a modelling choice is ambiguous this engine picks the unfavourable branch,
because an optimistic paper engine is worse than no paper engine at all -- it
manufactures confidence and charges you real money for it later.

  * top-of-book ``contention`` is removed before you fill (faster players got there)
  * resting (maker) orders fill only when the market trades STRICTLY THROUGH
    them, i.e. only when you were wrong -- see ``_match_resting``
  * orders arriving after the market closes are rejected, not filled
"""
from __future__ import annotations

import heapq
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Iterable

from ..types import (
    Fill,
    Order,
    OrderBook,
    OrderState,
    Position,
    Side,
    TimeInForce,
)
from ..signals.costs import FeeSchedule
from .latency import LatencyModel

EPS = 1e-9


class RejectReason(str, Enum):
    NONE = ""
    NO_LIQUIDITY = "no_liquidity"          # book moved away entirely in flight
    FOK_UNFILLABLE = "fok_unfillable"      # could not fill full size at arrival
    MARKET_CLOSED = "market_closed"        # landed after resolution -- real at 5m
    TICK_SIZE = "tick_size"
    MIN_SIZE = "min_size"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    # real-money adapter (execution/polymarket.py)
    DRY_RUN = "dry_run"                    # signed locally, deliberately not posted
    KILL_SWITCH = "kill_switch"            # the kill file exists
    HALTED = "halted"                      # daily loss or repeated API errors
    LIVE_CAPS = "live_caps"                # per-order / open / rate cap
    API_ERROR = "api_error"                # the venue could not be reached


@dataclass(slots=True)
class OrderResult:
    """What the strategy learns, when it learns it."""
    order: Order
    fills: list[Fill]
    reject_reason: RejectReason
    ack_ts: float

    @property
    def is_rejected(self) -> bool:
        return self.reject_reason is not RejectReason.NONE


@dataclass(slots=True)
class FeeModel:
    """Taker fee per fill.

    With a ``schedule`` (the venue's own ``feeSchedule``, verified 2026-09-17
    as ``rate * p * (1 - p)`` per share with rate 0.07 on crypto markets) the
    fee is charged exactly as the venue does, optionally per token so a
    market with a different schedule is charged its own. Without one the
    LEGACY ``rate * min(p, 1-p)`` shape is used -- that is what the original
    bot assumed, and it under-counted the real hurdle by 1.75x at 0.50 and
    ~2.8x at 0.80. Keep it only for the synthetic sim and old tests.
    """
    rate: float = 0.02
    schedule: FeeSchedule | None = None
    per_token: dict[str, FeeSchedule] = field(default_factory=dict)

    @classmethod
    def from_schedule(cls, schedule: FeeSchedule) -> "FeeModel":
        return cls(rate=0.0, schedule=schedule)

    def set_token_schedule(self, token_id: str, schedule: FeeSchedule) -> None:
        self.per_token[token_id] = schedule

    def charge(self, price: float, size: float, token_id: str | None = None) -> float:
        sched = self.per_token.get(token_id) if token_id else None
        sched = sched or self.schedule
        if sched is not None:
            return sched.taker_fee_per_share(price) * size
        return self.rate * min(price, 1.0 - price) * size


@dataclass(order=True, slots=True)
class _Scheduled:
    ts: float
    seq: int
    payload: object = field(compare=False)


class PaperExchange:
    """Simulates the Polymarket CLOB from the far side of a network link."""

    def __init__(
        self,
        latency: LatencyModel,
        fees: FeeModel | None = None,
        starting_balance: float = 1000.0,
    ) -> None:
        self.latency = latency
        self.fees = fees or FeeModel()
        self.balance = starting_balance
        self.starting_balance = starting_balance

        self.positions: dict[str, Position] = defaultdict(lambda: Position(""))
        self.fills: list[Fill] = []
        self.results: list[OrderResult] = []

        self._inflight: list[_Scheduled] = []      # orders -> exchange
        self._pending_acks: list[_Scheduled] = []  # results -> strategy
        self._resting: dict[str, Order] = {}
        self._seq = 0

        # telemetry
        self.n_submitted = 0
        self.n_rejected: dict[RejectReason, int] = defaultdict(int)
        self._submit_latencies: list[float] = []

    # ------------------------------------------------------------------ submit

    def submit(self, order: Order, now: float) -> str:
        """Strategy calls this. The order does NOT exist at the exchange yet."""
        order.decision_ts = now
        order.state = OrderState.IN_FLIGHT
        lat = self.latency.submit()
        self._submit_latencies.append(lat)
        self.n_submitted += 1
        self._push(self._inflight, now + lat, order)
        return order.order_id

    def cancel(self, order_id: str, now: float) -> None:
        """Cancels are not instant either. Between now and arrival you are still
        exposed -- which is exactly how makers get picked off."""
        self._push(self._pending_acks, now + self.latency.cancel(), ("cancel", order_id))

    def _push(self, q: list[_Scheduled], ts: float, payload: object) -> None:
        self._seq += 1
        heapq.heappush(q, _Scheduled(ts, self._seq, payload))

    # -------------------------------------------------------------------- step

    def step(
        self,
        now: float,
        book_of: Callable[[str], OrderBook | None],
        close_ts_of: Callable[[str], float] | None = None,
    ) -> list[OrderResult]:
        """Advance the exchange to ``now``.

        ``book_of`` must return the TRUE book (not the strategy's delayed view).
        Returns the results whose ack has landed, i.e. what the strategy may
        legitimately know about at ``now``.
        """
        # 1. orders that have arrived at the matching engine
        while self._inflight and self._inflight[0].ts <= now:
            sched = heapq.heappop(self._inflight)
            order: Order = sched.payload  # type: ignore[assignment]
            self._execute_on_arrival(order, sched.ts, book_of, close_ts_of)

        # 2. resting orders may fill as the book moves
        for order in list(self._resting.values()):
            book = book_of(order.token_id)
            if book is None:
                continue
            fills = self._match_resting(order, book, now)
            if fills:
                self._settle_fills(order, fills, now)
                if order.remaining <= EPS:
                    order.state = OrderState.FILLED
                    self._resting.pop(order.order_id, None)
                    self._push(
                        self._pending_acks,
                        now + self.latency.ack(),
                        OrderResult(order, fills, RejectReason.NONE, 0.0),
                    )

        # 3. results whose ack has arrived back at the strategy
        ready: list[OrderResult] = []
        while self._pending_acks and self._pending_acks[0].ts <= now:
            sched = heapq.heappop(self._pending_acks)
            payload = sched.payload
            if isinstance(payload, tuple) and payload[0] == "cancel":
                order = self._resting.pop(payload[1], None)
                if order is not None:
                    order.state = OrderState.CANCELLED
                continue
            result: OrderResult = payload  # type: ignore[assignment]
            result.ack_ts = sched.ts
            ready.append(result)
            self.results.append(result)
        return ready

    # ---------------------------------------------------------------- matching

    def _execute_on_arrival(
        self,
        order: Order,
        arrival_ts: float,
        book_of: Callable[[str], OrderBook | None],
        close_ts_of: Callable[[str], float] | None,
    ) -> None:
        def finish(fills: list[Fill], reason: RejectReason) -> None:
            if reason is not RejectReason.NONE:
                order.state = OrderState.REJECTED
                self.n_rejected[reason] += 1
            self._push(
                self._pending_acks,
                arrival_ts + self.latency.ack(),
                OrderResult(order, fills, reason, 0.0),
            )

        # A 5m market with a 200ms round trip means an order decided at T-0.15s
        # simply does not arrive in time. This is not an edge case here.
        if close_ts_of is not None:
            close_ts = close_ts_of(order.token_id)
            if close_ts and arrival_ts >= close_ts:
                finish([], RejectReason.MARKET_CLOSED)
                return

        book = book_of(order.token_id)
        if book is None:
            finish([], RejectReason.NO_LIQUIDITY)
            return

        matched = self._match_taker(order, book, arrival_ts)
        filled = sum(sz for _, sz in matched)

        if order.tif is TimeInForce.FOK and filled + EPS < order.size:
            # The book the strategy saw was good enough; the book that was
            # actually there when we landed was not. That delta is latency.
            finish([], RejectReason.FOK_UNFILLABLE)
            return

        if not matched:
            if order.tif is TimeInForce.GTC:
                order.state = OrderState.RESTING
                self._resting[order.order_id] = order
                # the strategy still has to be told it rested, and it learns
                # that an ack-latency later like everything else
                finish([], RejectReason.NONE)
                return
            finish([], RejectReason.NO_LIQUIDITY)
            return

        fills = self._settle_fills(order, matched, arrival_ts)

        if order.remaining > EPS and order.tif is TimeInForce.GTC:
            order.state = OrderState.PARTIAL
            self._resting[order.order_id] = order
        else:
            order.state = OrderState.FILLED if order.remaining <= EPS else OrderState.PARTIAL

        finish(fills, RejectReason.NONE)

    def _match_taker(
        self, order: Order, book: OrderBook, ts: float
    ) -> list[tuple[float, float]]:
        """Walk the book at arrival time. Returns [(price, size), ...]."""
        levels = book.asks if order.side is Side.BUY else book.bids
        contention = self.latency.profile.contention
        out: list[tuple[float, float]] = []
        remaining = order.size

        for i, lvl in enumerate(levels):
            if order.side is Side.BUY and lvl.price > order.price + EPS:
                break
            if order.side is Side.SELL and lvl.price < order.price - EPS:
                break
            avail = lvl.size
            if i == 0 and contention > 0.0:
                # someone faster than you ate part of the touch while you were
                # on the wire
                avail *= 1.0 - contention
            take = min(remaining, avail)
            if take <= EPS:
                continue
            out.append((lvl.price, take))
            remaining -= take
            if remaining <= EPS:
                break
        return out

    def _match_resting(
        self, order: Order, book: OrderBook, ts: float
    ) -> list[tuple[float, float]]:
        """Pessimistic maker fill model.

        A resting order is only filled when the opposite touch trades STRICTLY
        THROUGH its price -- that is, only once the market has already decided
        you were on the wrong side. Real queue position depends on trade-by-trade
        volume this engine does not have, and erring the other way would invent
        a market-making edge that does not exist.

        Treat any maker PnL from this engine as an upper bound on how bad it is,
        not an estimate of how good it is.
        """
        if order.side is Side.BUY:
            best_ask = book.best_ask
            if best_ask is None or best_ask >= order.price:
                return []
        else:
            best_bid = book.best_bid
            if best_bid is None or best_bid <= order.price:
                return []
        return [(order.price, order.remaining)]

    # ------------------------------------------------------------- bookkeeping

    def _settle_fills(
        self, order: Order, matched: Iterable[tuple[float, float]], exchange_ts: float
    ) -> list[Fill]:
        out: list[Fill] = []
        for price, size in matched:
            fee = self.fees.charge(price, size, order.token_id)
            fill = Fill(
                order_id=order.order_id,
                token_id=order.token_id,
                side=order.side,
                price=price,
                size=size,
                fee=fee,
                exchange_ts=exchange_ts,
                ack_ts=exchange_ts + self.latency.ack(),
                decision_ts=order.decision_ts,
                expected_price=order.expected_price,
                tag=order.tag,
            )
            order.filled_size += size
            order.filled_notional += price * size
            pos = self.positions[order.token_id]
            if not pos.token_id:
                pos.token_id = order.token_id
            pos.apply(fill)
            self.balance -= (price * size if order.side is Side.BUY else -price * size)
            self.balance -= fee
            self.fills.append(fill)
            out.append(fill)
        return out

    def settle_market(self, token_id: str, won: bool) -> float:
        """Resolve a token to 1.0 or 0.0 and realise the PnL."""
        pos = self.positions.get(token_id)
        if pos is None or abs(pos.shares) <= EPS:
            return 0.0
        payout = pos.shares * (1.0 if won else 0.0)
        self.balance += payout
        pnl = pos.settle(won)
        pos.shares = 0.0
        pos.cost_basis = 0.0
        pos.fees_paid = 0.0
        return pnl

    # -------------------------------------------------------------- telemetry

    def latency_report(self) -> dict[str, float]:
        """The numbers that tell you whether latency is eating the strategy."""
        slips = [f.slippage for f in self.fills if f.expected_price is not None]
        rts = [f.round_trip_ms for f in self.fills]
        n_fok = self.n_rejected[RejectReason.FOK_UNFILLABLE]
        n_closed = self.n_rejected[RejectReason.MARKET_CLOSED]
        filled_size = sum(f.size for f in self.fills)
        return {
            "orders_submitted": float(self.n_submitted),
            "orders_filled": float(len({f.order_id for f in self.fills})),
            "fill_rate": (
                len({f.order_id for f in self.fills}) / self.n_submitted
                if self.n_submitted else 0.0
            ),
            "rejected_fok_unfillable": float(n_fok),
            "rejected_market_closed": float(n_closed),
            "rejected_no_liquidity": float(self.n_rejected[RejectReason.NO_LIQUIDITY]),
            "mean_slippage_per_share": statistics.fmean(slips) if slips else 0.0,
            # the headline: USDC handed to the network, not to the market
            "total_slippage_cost": sum(
                f.slippage * f.size for f in self.fills if f.expected_price is not None
            ),
            "total_fees": sum(f.fee for f in self.fills),
            "mean_round_trip_ms": statistics.fmean(rts) if rts else 0.0,
            "p95_round_trip_ms": (
                sorted(rts)[int(len(rts) * 0.95)] if len(rts) >= 20 else 0.0
            ),
            "filled_shares": filled_size,
        }

    def equity(self, mark: Callable[[str], float | None] | None = None) -> float:
        """Balance plus mark-to-market of open positions."""
        eq = self.balance
        if mark is not None:
            for token_id, pos in self.positions.items():
                if abs(pos.shares) <= EPS:
                    continue
                m = mark(token_id)
                if m is not None:
                    eq += pos.shares * m
        return eq
