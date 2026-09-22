"""The venue contract. See the package docstring."""
from __future__ import annotations

from typing import Awaitable, Callable

from ..feeds.markets import MarketMeta

#: ``await get_json(url)`` -> (body, round_trip_ms). Raises
#: ``market.discovery.FetchFailed`` when the venue could not be asked; returns
#: ``(None, rtt)`` when it answered "no such thing".
GetJson = Callable[[str], Awaitable[tuple[object, float | None]]]


class Venue:
    #: shown in logs, feed health and the console
    name: str = "venue"
    #: "websocket" -- live.py runs its own subscription loop for this venue;
    #: "poll" -- live.py asks ``book_snapshots`` on a timer
    book_transport: str = "poll"
    #: seconds between book polls when ``book_transport == "poll"``
    poll_s: float = 1.0
    #: the venue holds taker orders this long before matching them; it is
    #: real latency the paper engine must charge
    taker_delay_ms: float = 0.0
    #: whether ``strike_hints`` can ever return anything
    publishes_strike: bool = False

    def slug(self, asset: str, epoch: int, duration_min: int) -> str:
        raise NotImplementedError

    def parse_slug(self, slug: str) -> tuple[str, int, int] | None:
        """(ASSET, duration_min, epoch) or None if this is not one of ours."""
        raise NotImplementedError

    async def fetch_market(self, get_json: GetJson, slug: str) -> dict | None:
        raise NotImplementedError

    def parse_market(self, row: dict) -> MarketMeta | None:
        raise NotImplementedError

    async def outcome(self, get_json: GetJson, slug: str) -> bool | None:
        """True = Up, False = Down, None = not (yet) decided."""
        raise NotImplementedError

    async def book_snapshots(self, get_json: GetJson, meta: MarketMeta
                             ) -> list[tuple[str, dict]]:
        """For poll venues: ``[(token_id, {"bids": [...], "asks": [...],
        "timestamp": ms}), ...]`` in the shape ``LiveBook.apply_snapshot`` reads."""
        return []

    async def strike_hints(self, get_json: GetJson) -> dict[str, float]:
        """slug -> the venue's own strike, for venues that publish it."""
        return {}
