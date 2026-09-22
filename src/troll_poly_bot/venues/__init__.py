"""Where the up/down markets live: Polymarket or Limitless.

Everything above this package is venue-agnostic already -- the composite spot,
the TWAP strike, the pricer, the fee curve shape, the risk book, the console.
What differs between venues is the plumbing: how a window's slug is spelled,
how its row is fetched and read, how its book arrives, how its outcome is
read. That is what a ``Venue`` provides. ``TPB_VENUE`` in .env picks one.
"""
from __future__ import annotations

from .base import Venue
from .limitless import LimitlessVenue
from .polymarket import PolymarketVenue

VENUES = {"polymarket": PolymarketVenue, "limitless": LimitlessVenue}


def make_venue(name: str, **kw) -> Venue:
    key = (name or "polymarket").strip().lower()
    if key not in VENUES:
        raise ValueError(f"unknown venue {name!r}; choose from {', '.join(sorted(VENUES))}")
    return VENUES[key](**kw)


__all__ = ["Venue", "PolymarketVenue", "LimitlessVenue", "VENUES", "make_venue"]
