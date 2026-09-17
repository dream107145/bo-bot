"""Delayed market-data views.

The exchange sees truth; the strategy sees the past. This module is what makes
the strategy see the past.

Each published message is stamped with its OWN transit time rather than the feed
being uniformly shifted. That matters: a latency spike on one message means a
later message can overtake it, which is exactly what happens on a real socket.
``view()`` therefore returns the freshest message *by publish time* among those
that have actually arrived -- modelling a sane client that discards stale
updates -- rather than simply the last thing off the wire.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

T = TypeVar("T")


@dataclass(slots=True)
class _Msg(Generic[T]):
    publish_ts: float    # when the exchange sent it
    visible_ts: float    # when it lands in your process
    value: T


class DelayedFeed(Generic[T]):
    """A feed the strategy may only read through a network link."""

    def __init__(
        self,
        latency_sampler: Callable[[], float],
        maxlen: int = 4096,
    ) -> None:
        self._sampler = latency_sampler
        self._buf: deque[_Msg[T]] = deque(maxlen=maxlen)
        self._truth: T | None = None
        self._truth_ts: float = 0.0

    def publish(self, value: T, now: float) -> None:
        """Called by the feed handler with the real exchange timestamp."""
        self._truth = value
        self._truth_ts = now
        self._buf.append(_Msg(now, now + self._sampler(), value))

    def view(self, now: float) -> T | None:
        """What the strategy is allowed to know at ``now``."""
        best: _Msg[T] | None = None
        for msg in reversed(self._buf):
            if msg.visible_ts > now:
                continue
            if best is None or msg.publish_ts > best.publish_ts:
                best = msg
            # buffer is append-ordered by publish_ts, so once we are looking at
            # messages older than the best we already found, nothing can beat it
            if best is not None and msg.publish_ts < best.publish_ts:
                break
        return best.value if best is not None else None

    def view_age_ms(self, now: float) -> float | None:
        """How stale the strategy's view currently is. Log this."""
        best: _Msg[T] | None = None
        for msg in reversed(self._buf):
            if msg.visible_ts <= now and (best is None or msg.publish_ts > best.publish_ts):
                best = msg
                break
        return None if best is None else now - best.publish_ts

    def truth(self) -> T | None:
        """The real current value. ONLY the exchange simulator may call this.

        If a strategy calls this it is look-ahead cheating and every number the
        paper run produces is worthless.
        """
        return self._truth
