"""Network latency modelling.

The single most important thing this simulator does. A paper engine that fills
at the touch instantly will report a profitable strategy that loses money live,
because it hides the two costs that actually matter:

  1. You decide on a book that is already stale (market-data latency).
  2. By the time your order lands, the book has moved (submit latency) -- and it
     has moved AGAINST you more often than not, because whatever moved it is
     frequently the same information that made you want to trade.

Latencies are modelled as ``base + Gamma(2, jitter/2)`` -- a right-skewed hump
rather than a constant -- plus occasional spikes. Real network latency is never
a constant, and a constant-latency sim systematically flatters the strategy: it
removes exactly the tail events during which you get run over.

Every profile is seeded so two paper runs are comparable. If you change a
threshold and PnL moves, that must be the threshold, not a different draw of
network noise.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np


@dataclass(frozen=True, slots=True)
class LatencyProfile:
    """One-way latencies in milliseconds.

    ``*_base`` is the floor (physics + processing). ``*_jitter`` is the MEAN of
    the right-skewed excess on top of it, so mean latency ~= base + jitter.
    """

    name: str

    # market data: exchange -> your process
    md_spot_base: float          # Binance (or whatever the resolution feed is)
    md_spot_jitter: float
    md_book_base: float          # Polymarket CLOB book
    md_book_jitter: float

    # order path
    submit_base: float           # your process -> matching engine
    submit_jitter: float
    ack_base: float              # matching engine -> your process
    ack_jitter: float
    cancel_base: float
    cancel_jitter: float

    # pathological events
    spike_prob: float = 0.0      # per-message probability of a latency spike
    spike_ms: float = 0.0        # MEAN magnitude of a spike (exponential tail)

    #: Fraction of top-of-book size assumed consumed by faster participants
    #: before your order lands. The crudest possible adverse-selection proxy,
    #: but omitting it entirely is worse -- see PaperExchange.
    contention: float = 0.0

    def reaction_lag_ms(self) -> float:
        """Mean time between the world changing and your order landing.

        The world moves for this long without you. Every strategy decision is
        really a bet on where price will be ``reaction_lag_ms`` from the tick
        you reacted to -- not on where it is now.
        """
        return (
            self.md_spot_base + self.md_spot_jitter
            + self.submit_base + self.submit_jitter
        )

    def round_trip_ms(self) -> float:
        return (
            self.submit_base + self.submit_jitter
            + self.ack_base + self.ack_jitter
        )


# --------------------------------------------------------------------------
# Presets. Measure your own before trusting any of these (scripts/measure_latency.py).
# --------------------------------------------------------------------------

COLOCATED = LatencyProfile(
    name="colocated",
    md_spot_base=1.5, md_spot_jitter=0.8,
    md_book_base=1.5, md_book_jitter=0.8,
    submit_base=2.0, submit_jitter=1.0,
    ack_base=2.0, ack_jitter=1.0,
    cancel_base=2.0, cancel_jitter=1.0,
    spike_prob=0.001, spike_ms=25.0,
    contention=0.35,
)

VPS_NEARBY = LatencyProfile(
    name="vps_nearby",
    md_spot_base=25.0, md_spot_jitter=12.0,   # still cross-region to Binance
    md_book_base=8.0, md_book_jitter=5.0,
    submit_base=10.0, submit_jitter=6.0,
    ack_base=10.0, ack_jitter=6.0,
    cancel_base=10.0, cancel_jitter=6.0,
    spike_prob=0.005, spike_ms=80.0,
    contention=0.55,
)

HOME_BROADBAND = LatencyProfile(
    name="home_broadband",
    md_spot_base=120.0, md_spot_jitter=60.0,  # US home -> Binance is far
    md_book_base=40.0, md_book_jitter=25.0,
    submit_base=50.0, submit_jitter=30.0,
    ack_base=50.0, ack_jitter=30.0,
    cancel_base=50.0, cancel_jitter=30.0,
    spike_prob=0.02, spike_ms=300.0,
    contention=0.80,
)

HOME_WIFI_POOR = LatencyProfile(
    name="home_wifi_poor",
    md_spot_base=180.0, md_spot_jitter=140.0,
    md_book_base=80.0, md_book_jitter=70.0,
    submit_base=100.0, submit_jitter=90.0,
    ack_base=100.0, ack_jitter=90.0,
    cancel_base=100.0, cancel_jitter=90.0,
    spike_prob=0.05, spike_ms=900.0,
    contention=0.90,
)

#: Zero latency. NOT for evaluating a strategy -- only for unit tests and for
#: measuring how much your edge depends on being fast (run both, compare).
INSTANT = LatencyProfile(
    name="instant",
    md_spot_base=0.0, md_spot_jitter=0.0,
    md_book_base=0.0, md_book_jitter=0.0,
    submit_base=0.0, submit_jitter=0.0,
    ack_base=0.0, ack_jitter=0.0,
    cancel_base=0.0, cancel_jitter=0.0,
    spike_prob=0.0, spike_ms=0.0,
    contention=0.0,
)

PROFILES: dict[str, LatencyProfile] = {
    p.name: p for p in (COLOCATED, VPS_NEARBY, HOME_BROADBAND, HOME_WIFI_POOR, INSTANT)
}


class LatencyModel:
    """Samples one-way latencies from a profile. Seeded for reproducibility.

    Random draws are buffered in blocks. A per-call ``rng.gamma()`` is dominated
    by numpy's scalar-call overhead, and this model is sampled several times per
    tick for every feed and every order -- it showed up as the single largest
    cost in a simulation profile. Drawing standard variates in blocks and
    scaling them is distributionally identical and far cheaper.
    """

    _BLOCK = 8192

    def __init__(self, profile: LatencyProfile, seed: int = 0) -> None:
        self.profile = profile
        self._rng = np.random.default_rng(seed)
        self._gamma: np.ndarray = np.empty(0)
        self._unif: np.ndarray = np.empty(0)
        self._expo: np.ndarray = np.empty(0)
        self._gi = self._ui = self._ei = 0

    def _next_gamma(self) -> float:
        if self._gi >= len(self._gamma):
            # Gamma(shape=2, scale=1) -> mean 2; scaled by jitter/2 below so the
            # excess has mean == jitter. Right-skewed, and unlike an exponential
            # it does not pile up at the floor.
            self._gamma = self._rng.gamma(2.0, 1.0, self._BLOCK)
            self._gi = 0
        v = self._gamma[self._gi]
        self._gi += 1
        return float(v)

    def _next_unif(self) -> float:
        if self._ui >= len(self._unif):
            self._unif = self._rng.random(self._BLOCK)
            self._ui = 0
        v = self._unif[self._ui]
        self._ui += 1
        return float(v)

    def _next_expo(self) -> float:
        if self._ei >= len(self._expo):
            self._expo = self._rng.exponential(1.0, self._BLOCK)
            self._ei = 0
        v = self._expo[self._ei]
        self._ei += 1
        return float(v)

    def _draw(self, base: float, jitter: float) -> float:
        if base <= 0.0 and jitter <= 0.0:
            return 0.0
        excess = self._next_gamma() * (jitter / 2.0) if jitter > 0.0 else 0.0
        spike = 0.0
        p = self.profile
        if p.spike_prob > 0.0 and self._next_unif() < p.spike_prob:
            spike = self._next_expo() * p.spike_ms
        return base + excess + spike

    def md_spot(self) -> float:
        return self._draw(self.profile.md_spot_base, self.profile.md_spot_jitter)

    def md_book(self) -> float:
        return self._draw(self.profile.md_book_base, self.profile.md_book_jitter)

    def submit(self) -> float:
        return self._draw(self.profile.submit_base, self.profile.submit_jitter)

    def ack(self) -> float:
        return self._draw(self.profile.ack_base, self.profile.ack_jitter)

    def cancel(self) -> float:
        return self._draw(self.profile.cancel_base, self.profile.cancel_jitter)

    def describe(self) -> str:
        p = self.profile
        return (
            f"latency profile {p.name!r}: "
            f"spot md ~{p.md_spot_base + p.md_spot_jitter:.0f}ms, "
            f"book md ~{p.md_book_base + p.md_book_jitter:.0f}ms, "
            f"order round trip ~{p.round_trip_ms():.0f}ms, "
            f"REACTION LAG ~{p.reaction_lag_ms():.0f}ms, "
            f"contention {p.contention:.0%}"
        )


def degraded(profile: LatencyProfile, factor: float) -> LatencyProfile:
    """Scale a profile's latencies.

    Use it to stress-test: if the edge evaporates at 1.5x, you do not have an
    edge, you have a latency lottery.
    """
    return replace(
        profile,
        name=f"{profile.name}x{factor:g}",
        md_spot_base=profile.md_spot_base * factor,
        md_spot_jitter=profile.md_spot_jitter * factor,
        md_book_base=profile.md_book_base * factor,
        md_book_jitter=profile.md_book_jitter * factor,
        submit_base=profile.submit_base * factor,
        submit_jitter=profile.submit_jitter * factor,
        ack_base=profile.ack_base * factor,
        ack_jitter=profile.ack_jitter * factor,
        cancel_base=profile.cancel_base * factor,
        cancel_jitter=profile.cancel_jitter * factor,
    )
