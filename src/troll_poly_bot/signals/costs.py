"""Every cost between a model probability and a realised payout.

Fees, verified 2026-09-17 against the venue
--------------------------------------------
Gamma returns ``feeSchedule: {rate: 0.07, exponent: 1, takerOnly: true,
rebateRate: 0.2}`` with ``feeType: crypto_fees_v2`` on every 5m crypto
market, and the docs give the formula:

    fee = shares * rate * (p * (1 - p)) ** exponent

Takers pay; makers pay nothing and receive 20% of collected fees. At p = 0.50
that is 1.75c per share, at 0.80 it is 1.12c, at 0.95 it is 0.33c. The old
code assumed ``0.02 * min(p, 1-p)`` -- 1.0c at 0.50 and 0.40c at 0.80 -- so
it under-counted the hurdle by 1.75x at the middle and ~2.8x at 0.80.

The schedule is read from the market row at discovery time, never assumed.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    rate: float = 0.07
    exponent: float = 1.0
    taker_only: bool = True
    maker_rebate: float = 0.2
    enabled: bool = True

    def taker_fee_per_share(self, price: float) -> float:
        if not self.enabled or price <= 0.0 or price >= 1.0:
            return 0.0
        return self.rate * (price * (1.0 - price)) ** self.exponent

    def maker_fee_per_share(self, price: float) -> float:
        return 0.0 if self.taker_only else self.taker_fee_per_share(price)

    @classmethod
    def from_gamma(cls, row: dict) -> "FeeSchedule":
        """Parse a Gamma market row. Falls back to the verified crypto schedule."""
        fs = row.get("feeSchedule") or {}
        try:
            return cls(
                rate=float(fs.get("rate", 0.07)),
                exponent=float(fs.get("exponent", 1.0)),
                taker_only=bool(fs.get("takerOnly", True)),
                maker_rebate=float(fs.get("rebateRate", 0.2)),
                enabled=bool(row.get("feesEnabled", True)),
            )
        except (TypeError, ValueError):
            return cls()


@dataclass(slots=True)
class CostBreakdown:
    """Per share. ``net_edge`` is what is left of the model edge after costs."""
    model_p: float
    market_p: float          # the mid, for reference
    ask: float
    gross_edge: float        # model_p - ask
    fee: float
    half_spread: float       # ask - mid: what crossing costs relative to fair-ish
    slippage: float          # expected adverse move of the ask while in flight
    latency_cost: float      # model edge lost to a stale view (uncertainty band)
    net_edge: float

    def as_dict(self) -> dict[str, float]:
        return {k: round(getattr(self, k), 5) for k in (
            "model_p", "market_p", "ask", "gross_edge", "fee", "half_spread",
            "slippage", "latency_cost", "net_edge")}


@dataclass(slots=True)
class CostModel:
    fee: FeeSchedule = FeeSchedule()
    #: Expected adverse move of the ask between decision and arrival, per
    #: share. Measured by the replay on recorded books (see docs/strategy.md);
    #: a parameter here so it is never silently zero.
    expected_slippage: float = 0.004
    #: Fraction of the model-uncertainty band charged as a latency/staleness
    #: cost. The pricer already widens its horizon by the information lag; this
    #: is the residual "we may be wrong about sigma" term.
    uncertainty_charge: float = 1.0

    def evaluate(self, model_p: float, ask: float, bid: float | None,
                 model_uncertainty: float = 0.0) -> CostBreakdown:
        mid = (ask + bid) / 2.0 if bid is not None else ask
        fee = self.fee.taker_fee_per_share(ask)
        half_spread = max(0.0, ask - mid)
        latency = self.uncertainty_charge * max(0.0, model_uncertainty)
        gross = model_p - ask
        net = gross - fee - self.expected_slippage - latency
        return CostBreakdown(
            model_p=model_p, market_p=mid, ask=ask, gross_edge=gross, fee=fee,
            half_spread=half_spread, slippage=self.expected_slippage,
            latency_cost=latency, net_edge=net,
        )
