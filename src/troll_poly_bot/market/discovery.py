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

from ..feeds.polymarket import (
    DEFAULT_DURATION_MIN, SUPPORTED_DURATIONS_MIN, WINDOW_S, duration_tag,
    window_epoch, window_seconds,
)

log = logging.getLogger(__name__)

CANDIDATE_ASSETS: tuple[str, ...] = (
    "BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE",
    "ADA", "AVAX", "LINK", "LTC", "DOT", "SUI", "TRX", "TON", "PEPE", "SHIB",
    "BCH", "XLM", "UNI", "AAVE", "NEAR", "APT", "ARB", "OP", "POL", "WLD", "ENA",
)


class FetchFailed(Exception):
    """The request itself failed (network, DNS, timeout, 5xx). Not "absent"."""


Fetch = Callable[[str], Awaitable[dict | None]]


def slug_for_epoch(asset: str, epoch: int,
                   duration_min: int = DEFAULT_DURATION_MIN) -> str:
    return f"{asset.lower()}-updown-{duration_tag(duration_min)}-{epoch}"


def slugs_for_epoch(assets: tuple[str, ...], epoch: int,
                    duration_min: int = DEFAULT_DURATION_MIN) -> list[tuple[str, str]]:
    return [(a, slug_for_epoch(a, epoch, duration_min)) for a in assets]


async def probe_assets(candidates: tuple[str, ...], epoch: int, fetch: Fetch,
                       concurrency: int = 6,
                       duration_min: int = DEFAULT_DURATION_MIN,
                       ) -> tuple[dict[str, dict], set[str]]:
    """Return ({asset: gamma_row} for every listed candidate, {assets whose
    probe FAILED and therefore said nothing})."""
    sem = asyncio.Semaphore(concurrency)
    found: dict[str, dict] = {}
    failed: set[str] = set()

    async def one(asset: str) -> None:
        async with sem:
            try:
                row = await fetch(slug_for_epoch(asset, epoch, duration_min))
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
    every ``retry_s`` while the last candidate is unanswered.

    ``durations`` is the set of window lengths to probe, in minutes. Each is
    probed at its own UTC alignment, because a 15m window opens on a 900s
    boundary and a 5m window on a 300s one -- probing 15m at a 5m epoch asks
    for a slug that never exists. An asset counts as listed if ANY configured
    duration lists it; ``assets_for`` says which of them actually do, so a
    caller never constructs a slug the venue does not have.
    """
    candidates: tuple[str, ...] = CANDIDATE_ASSETS
    reprobe_s: float = 1800.0
    retry_s: float = 60.0
    active: dict[str, dict] = field(default_factory=dict)     # asset -> last gamma row
    last_probe: float = 0.0
    pinned: tuple[str, ...] = ()        # assets the operator restricted to, if any
    unresolved: set[str] = field(default_factory=set)         # probe failed last time
    probe_failures: int = 0
    durations: tuple[int, ...] = (DEFAULT_DURATION_MIN,)
    #: duration (minutes) -> {asset: last gamma row}
    active_by_duration: dict[int, dict[str, dict]] = field(default_factory=dict)

    @property
    def assets(self) -> tuple[str, ...]:
        found = tuple(sorted(self.active))
        if self.pinned:
            return tuple(a for a in found if a in self.pinned)
        return found

    def assets_for(self, duration_min: int) -> tuple[str, ...]:
        """Assets listed at this duration, honouring ``pinned``.

        Before the first probe of a duration this falls back to ``assets`` so a
        caller is never starved by a registry that has not run yet; a slug that
        does not exist simply fetches nothing.
        """
        per = self.active_by_duration.get(int(duration_min))
        if per is None:
            return self.assets
        found = tuple(sorted(per))
        if self.pinned:
            return tuple(a for a in found if a in self.pinned)
        return found

    def cached_row(self, slug: str) -> dict | None:
        """The gamma row for ``slug`` if the last probe happened to fetch it.

        Saves one request per window per asset. Matched on the slug itself, so
        a stale row from a previous window can never be returned.
        """
        for per in self.active_by_duration.values():
            for row in per.values():
                if row.get("slug") == slug:
                    return row
        return None

    def due(self, now_s: float | None = None) -> bool:
        now_s = time.time() if now_s is None else now_s
        if not self.active:
            return True
        wait = self.retry_s if self.unresolved else self.reprobe_s
        return now_s - self.last_probe >= wait

    async def refresh(self, fetch: Fetch, now_s: float | None = None) -> tuple[str, ...]:
        now_s = time.time() if now_s is None else now_s
        cands = self.pinned or self.candidates
        durations = self.durations or (DEFAULT_DURATION_MIN,)

        # One probe per duration, each at its OWN alignment. The union is what
        # "listed" means; a failure anywhere leaves that asset unanswered.
        epoch = window_epoch(now_s, duration_min=durations[0])
        found: dict[str, dict] = {}
        failed: set[str] = set()
        per_duration: dict[int, dict[str, dict]] = {}
        for d in durations:
            d_epoch = window_epoch(now_s, duration_min=d)
            d_found, d_failed = await probe_assets(cands, d_epoch, fetch, duration_min=d)
            per_duration[int(d)] = d_found
            failed |= d_failed
            for asset, row in d_found.items():
                found.setdefault(asset, row)
        self.last_probe = now_s
        self.unresolved = set(failed)
        if failed:
            self.probe_failures += 1
        if not found and failed:
            # the venue could not be asked at all; keep what we had
            log.warning("asset probe failed for every candidate (%d); keeping %s and retrying in %.0fs",
                        len(failed), ", ".join(sorted(self.active)) or "nothing", self.retry_s)
            return self.assets
        # an asset whose probe failed is unknown, not delisted: carry it over.
        # Read the PREVIOUS per-duration state before replacing it.
        for a in failed:
            if a in self.active:
                found.setdefault(a, self.active[a])
            for d, per in per_duration.items():
                prev = self.active_by_duration.get(d, {})
                if a not in per and a in prev:
                    per[a] = prev[a]
        self.active_by_duration = per_duration
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
            log.warning("no %s markets found for any candidate at epoch %d",
                        "/".join(duration_tag(d) for d in durations), epoch)
        return self.assets


def next_epochs(now_s: float, ahead: int = 1,
                duration_min: int = DEFAULT_DURATION_MIN) -> list[int]:
    """Epochs of the current window and ``ahead`` following ones."""
    return [window_epoch(now_s, k, duration_min) for k in range(ahead + 1)]


def parse_durations(spec: str | None, default: tuple[int, ...] = (DEFAULT_DURATION_MIN,),
                    ) -> tuple[int, ...]:
    """Parse ``"15"`` or ``"5,15"`` (also ``"5m,15m"``) into ``(5, 15)``.

    Order is preserved after de-duplication so the first entry is the primary
    duration. An unsupported value is a hard error rather than a silent skip:
    a bot that quietly trades nothing is worse than one that refuses to start.
    """
    if spec is None or not str(spec).strip():
        return default
    out: list[int] = []
    for part in str(spec).replace(";", ",").split(","):
        tok = part.strip().lower().rstrip("m").strip()
        if not tok:
            continue
        try:
            val = int(tok)
        except ValueError:
            raise ValueError(f"not a window length in minutes: {part!r}") from None
        if val not in SUPPORTED_DURATIONS_MIN:
            raise ValueError(
                f"unsupported window {val}m; the venue lists "
                f"{', '.join(duration_tag(d) for d in SUPPORTED_DURATIONS_MIN)}"
            )
        if val not in out:
            out.append(val)
    return tuple(out) or default


__all__ = ["CANDIDATE_ASSETS", "AssetRegistry", "FetchFailed", "probe_assets", "slugs_for_epoch",
           "slug_for_epoch", "next_epochs", "parse_durations", "WINDOW_S",
           "SUPPORTED_DURATIONS_MIN", "DEFAULT_DURATION_MIN", "window_seconds"]
