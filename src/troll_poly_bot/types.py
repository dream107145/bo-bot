"""Core domain types.

All timestamps are epoch milliseconds as float. All prices are USDC per share
in [0, 1]. All sizes are shares.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum

_order_seq = itertools.count(1)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class TimeInForce(str, Enum):
    #: Fill what you can at arrival, kill the rest. The only honest choice for a
    #: latency-sensitive taker: a resting remainder is a free option for others.
    FOK = "FOK"
    IOC = "IOC"
    GTC = "GTC"


class OrderState(str, Enum):
    IN_FLIGHT = "IN_FLIGHT"      # travelling to the matching engine
    RESTING = "RESTING"          # live on the book
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass(slots=True)
class BookLevel:
    price: float
    size: float


@dataclass(slots=True)
class OrderBook:
    """One side of a binary market's book, for a single token_id.

    bids are sorted descending by price, asks ascending.
    """
    token_id: str
    bids: list[BookLevel] = field(default_factory=list)
    asks: list[BookLevel] = field(default_factory=list)
    ts: float = 0.0

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        return (b + a) / 2 if b is not None and a is not None else None

    def microprice(self) -> float | None:
        """Size-weighted mid. A better estimate of the next print than mid."""
        if not self.bids or not self.asks:
            return None
        b, a = self.bids[0], self.asks[0]
        denom = b.size + a.size
        if denom <= 0:
            return self.mid
        # weight each side by the size resting on the OPPOSITE side
        return (b.price * a.size + a.price * b.size) / denom

    def copy(self) -> OrderBook:
        return OrderBook(
            token_id=self.token_id,
            bids=[BookLevel(l.price, l.size) for l in self.bids],
            asks=[BookLevel(l.price, l.size) for l in self.asks],
            ts=self.ts,
        )


@dataclass(slots=True)
class Order:
    token_id: str
    side: Side
    price: float                      # limit price
    size: float                       # shares
    tif: TimeInForce = TimeInForce.FOK
    order_id: str = ""
    #: wall-clock at which the strategy DECIDED to send this
    decision_ts: float = 0.0
    #: book state the strategy based the decision on (for slippage attribution)
    expected_price: float | None = None
    #: fair value at decision time (for edge-capture attribution)
    fair_at_decision: float | None = None
    state: OrderState = OrderState.IN_FLIGHT
    filled_size: float = 0.0
    filled_notional: float = 0.0
    tag: str = ""

    def __post_init__(self) -> None:
        if not self.order_id:
            self.order_id = f"ord-{next(_order_seq):06d}"

    @property
    def remaining(self) -> float:
        return max(0.0, self.size - self.filled_size)

    @property
    def avg_fill_price(self) -> float | None:
        if self.filled_size <= 0:
            return None
        return self.filled_notional / self.filled_size


@dataclass(slots=True)
class Fill:
    order_id: str
    token_id: str
    side: Side
    price: float
    size: float
    fee: float
    #: when the match actually happened at the exchange
    exchange_ts: float
    #: when the strategy LEARNS about it (exchange_ts + ack latency)
    ack_ts: float
    decision_ts: float
    expected_price: float | None = None
    tag: str = ""

    @property
    def slippage(self) -> float:
        """Signed cost vs. the price the strategy expected, in USDC per share.

        Positive = worse than expected. This is the number that tells you what
        latency is costing you.
        """
        if self.expected_price is None:
            return 0.0
        if self.side is Side.BUY:
            return self.price - self.expected_price
        return self.expected_price - self.price

    @property
    def round_trip_ms(self) -> float:
        return self.ack_ts - self.decision_ts


@dataclass(slots=True)
class Position:
    token_id: str
    shares: float = 0.0
    cost_basis: float = 0.0     # net USDC paid (negative if net received)
    fees_paid: float = 0.0

    def apply(self, fill: Fill) -> None:
        notional = fill.price * fill.size
        if fill.side is Side.BUY:
            self.shares += fill.size
            self.cost_basis += notional
        else:
            self.shares -= fill.size
            self.cost_basis -= notional
        self.fees_paid += fill.fee

    def settle(self, outcome_is_yes: bool) -> float:
        """Realised PnL when the market resolves. This token pays 1.0 if it won."""
        payout = self.shares * (1.0 if outcome_is_yes else 0.0)
        return payout - self.cost_basis - self.fees_paid


@dataclass(slots=True)
class Market:
    """A single 5-minute crypto up/down market."""
    condition_id: str
    asset: str                   # "BTC", "ETH", ...
    yes_token_id: str            # the "Up" token
    no_token_id: str             # the "Down" token
    strike: float                # reference/open price the outcome compares against
    open_ts: float               # window start (epoch ms)
    close_ts: float              # resolution timestamp (epoch ms)
    tick_size: float = 0.01
    min_size: float = 5.0
    slug: str = ""

    def seconds_remaining(self, now: float) -> float:
        return max(0.0, (self.close_ts - now) / 1000.0)
