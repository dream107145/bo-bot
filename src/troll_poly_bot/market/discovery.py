"""Which assets have 5-minute up/down markets right now?

Slugs are deterministic (``<asset>-updown-5m-<window start epoch>``), so
discovery is a probe: ask Gamma for the current window's slug for every
candidate asset and keep the ones that exist. Probed 2026-09-17 this found
btc, eth, sol, xrp, doge, bnb and hype; the candidate list is deliberately
wider so a newly listed asset is picked up without a code change, and it is
re-probed periodically so a delisted one drops out.

"Not listed" and "could not ask" are different answers
-----------------------------------------------------
The first version treated both as absence. During a DNS outage a probe that
could only reach two of seven assets became the asset list for the next
half hour, and the bot quietly stopped trading the other five. A fetch now
raises ``FetchFailed`` on a network error; the registry keeps an asset whose
probe failed, drops one only when the venue positively returns nothing, and
re-probes every minute while any answer is missing.

Nothing here hard-codes BTC.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from ..feeds.polymarket import WINDOW_S, window_epoch

log = logging.getLogger(__name__)

CANDIDATE_ASSETS: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE",
    "ADA", "AVAX", "LINK", "LTC", "DOT", "SUI", "TRX", "TON", "PEPE", "SHIB",
    "BCH", "XLM", "UNI", "AAVE", "NEAR", "APT", "ARB", "OP", "POL", "WLD", "ENA",
)


class FetchFailed(Exception):
    """The request itself failed (network, DNS, timeout, 5xx). Not "absent"."""


Fetch = Callable[[str], Awaitable[dict | None]]


def slug_for_epoch(asset: str, epoch: int) -> str:
    return f"{asset.lower()}-updown-5m-{epoch}"


def slugs_for_epoch(assets: tuple[str, ...], epoch: int) -> list[tuple[str, str]]:
    return [(a, slug_for_epoch(a, epoch)) for a in assets]


async def probe_assets(candidates: tuple[str, ...], epoch: int, fetch: Fetch,
                       concurrency: int = 6) -> tuple[dict[str, dict], set[str]]:
    """Return ({asset: gamma_row} for every listed candidate, {assets whose
    probe FAILED and therefore said nothing})."""
    sem = asyncio.Semaphore(concurrency)
    found: dict[str, dict] = {}
    failed: set[str] = set()

    async def one(asset: str) -> None:
        async with sem:
            try:
                row = await fetch(slug_for_epoch(asset, epoch))
            except FetchFailed:
                failed.add(asset)
                return
            except Exception as exc:                        # noqa: BLE001
                log.debug("probe %s raised %s", asset, exc)
                failed.add(asset)
                return
        if row:
            found[asset] = row

    await asyncio.gather(*(one(a) for a in candidates))
    return found, failed


@dataclass
class AssetRegistry:
    """Assets currently listed, re-probed every ``reprobe_s`` seconds, or
    every ``retry_s`` while the last probe left any candidate unanswered."""
    candidates: tuple[str, ...] = CANDIDATE_ASSETS
    reprobe_s: float = 1800.0
    retry_s: float = 60.0
    active: dict[str, dict] = field(default_factory=dict)     # asset -> last gamma row
    last_probe: float = 0.0
    pinned: tuple[str, ...] = ()        # assets the operator restricted to, if any
    unresolved: set[str] = field(default_factory=set)         # probe failed last time
    probe_failures: int = 0

    @property
    def assets(self) -> tuple[str, ...]:
        found = tuple(sorted(self.active))
        if self.pinned:
            return tuple(a for a in found if a in self.pinned)
        return found

    def due(self, now_s: float | None = None) -> bool:
        now_s = time.time() if now_s is None else now_s
        if not self.active:
            return True
        wait = self.retry_s if self.unresolved else self.reprobe_s
        return now_s - self.last_probe >= wait

    async def refresh(self, fetch: Fetch, now_s: float | None = None) -> tuple[str, ...]:
        now_s = time.time() if now_s is None else now_s
        epoch = window_epoch(now_s)
        cands = self.pinned or self.candidates
        found, failed = await probe_assets(cands, epoch, fetch)
        self.last_probe = now_s
        self.unresolved = set(failed)
        if failed:
            self.probe_failures += 1
        if not found and failed:
            # the venue could not be asked at all; keep what we had
            log.warning("asset probe failed for every candidate (%d); keeping %s and retrying in %.0fs",
                        len(failed), ", ".join(sorted(self.active)) or "nothing", self.retry_s)
            return self.assets
        # an asset whose probe failed is unknown, not delisted: carry it over
        for a in failed:
            if a in self.active:
                found[a] = self.active[a]
        gone = set(self.active) - set(found)
        new = set(found) - set(self.active)
        self.active = found
        if new:
            log.info("assets listed: %s", ", ".join(sorted(new)))
        if gone:
            log.info("assets no longer listed: %s", ", ".join(sorted(gone)))
        if failed:
            log.info("asset probe incomplete (%s unanswered); retrying in %.0fs",
                     ", ".join(sorted(failed)), self.retry_s)
        if not self.active:
            log.warning("no 5m markets found for any candidate at epoch %d", epoch)
        return self.assets


def next_epochs(now_s: float, ahead: int = 1) -> list[int]:
    """Epochs of the current window and ``ahead`` following ones."""
    return [window_epoch(now_s, k) for k in range(ahead + 1)]


__all__ = ["CANDIDATE_ASSETS", "AssetRegistry", "FetchFailed", "probe_assets", "slugs_for_epoch",
           "slug_for_epoch", "next_epochs", "WINDOW_S"]
