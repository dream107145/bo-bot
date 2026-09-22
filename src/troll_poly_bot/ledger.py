"""The run ledger, folded from events into the trades a person recognises.

The problem this solves
-----------------------
``data/live_trades.jsonl`` is an event stream: one line per fill, one per
settlement. A single position routinely produces three or four of them --
a window is often entered in two partial fills, then sold, then settled --
and in a flat table those lines look like the same trade written out several
times. They are not duplicates; they are the parts of one round trip.

So the ledger view is built from round trips instead: one row per position,
carrying when it was bought, when it was sold or settled, at what average
prices, and what it made. The raw events stay available underneath, because
the event stream is the record and this is a reading of it.

A round trip closes when the position returns to flat. Buying the same window
again after an exit starts a new one rather than reopening the old.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

TRADE_LOG = Path("data/live_trades.jsonl")
EPS = 1e-9


def load(path: Path | str | None = None) -> list[dict[str, Any]]:
    """Every event on disk, oldest first. Unreadable lines are skipped."""
    rows: list[dict[str, Any]] = []
    try:
        text = Path(path or TRADE_LOG).read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("event"):
            rows.append(row)
    return rows


def _num(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if f != f else f                    # NaN -> default


def is_sell(row: dict[str, Any]) -> bool:
    """A sell is marked; anything else is an entry.

    Older rows carry no ``action`` at all, which is how the writer used to
    record a buy, so the absence of the key has to keep meaning BUY.
    """
    return str(row.get("action") or "").upper() == "SELL"


@dataclass
class RoundTrip:
    slug: str = ""
    asset: str = ""
    side: str = ""
    mode: str = ""
    epoch: int = 0
    bought_ts: float | None = None
    last_buy_ts: float | None = None
    sold_ts: float | None = None
    settled_ts: float | None = None
    shares: float = 0.0
    sold_shares: float = 0.0
    cost: float = 0.0                 # what the entries cost, before fees
    proceeds: float = 0.0             # what the exits returned, before fees
    fees: float = 0.0
    pnl: float | None = None
    balance_after: float | None = None
    exited: bool = False              # closed early rather than held to resolution
    buys: int = 0
    sells: int = 0
    expected: list[float] = field(default_factory=list)

    @property
    def entry_price(self) -> float | None:
        return self.cost / self.shares if self.shares > EPS else None

    @property
    def exit_price(self) -> float | None:
        return self.proceeds / self.sold_shares if self.sold_shares > EPS else None

    @property
    def closed_ts(self) -> float | None:
        return self.settled_ts if self.settled_ts is not None else self.sold_ts

    @property
    def hold_s(self) -> float | None:
        if self.bought_ts is None or self.closed_ts is None:
            return None
        return max(0.0, (self.closed_ts - self.bought_ts) / 1000.0)

    @property
    def status(self) -> str:
        if self.pnl is None and self.sold_ts is None:
            return "open"
        if self.exited or (self.sold_ts is not None and self.pnl is not None):
            return "sold"
        return "settled"

    @property
    def slippage(self) -> float | None:
        """Average paid minus average expected, over the entries."""
        if not self.expected or self.entry_price is None:
            return None
        return self.entry_price - sum(self.expected) / len(self.expected)

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug, "asset": self.asset, "side": self.side,
            "mode": self.mode, "epoch": self.epoch,
            "bought_ts": self.bought_ts, "last_buy_ts": self.last_buy_ts,
            "sold_ts": self.sold_ts, "settled_ts": self.settled_ts,
            "closed_ts": self.closed_ts,
            "shares": round(self.shares, 4), "sold_shares": round(self.sold_shares, 4),
            "entry_price": None if self.entry_price is None else round(self.entry_price, 4),
            "exit_price": None if self.exit_price is None else round(self.exit_price, 4),
            "cost": round(self.cost, 4), "proceeds": round(self.proceeds, 4),
            "fees": round(self.fees, 4),
            "pnl": None if self.pnl is None else round(self.pnl, 4),
            "balance_after": self.balance_after,
            "hold_s": None if self.hold_s is None else round(self.hold_s, 1),
            "status": self.status, "exited": self.exited,
            "buys": self.buys, "sells": self.sells,
            "slippage": None if self.slippage is None else round(self.slippage, 4),
        }


def round_trips(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fold fills and settlements into one row per position, newest last."""
    events = sorted((r for r in rows if r.get("ts") is not None),
                    key=lambda r: _num(r.get("ts")))
    open_trip: dict[tuple[str, str], RoundTrip] = {}
    by_slug: dict[str, list[RoundTrip]] = {}
    done: list[RoundTrip] = []

    def close(key: tuple[str, str]) -> None:
        trip = open_trip.pop(key, None)
        if trip is not None:
            done.append(trip)

    for row in events:
        slug = str(row.get("slug") or "")
        ts = _num(row.get("ts"))
        if row.get("event") == "fill":
            side = str(row.get("side") or "")
            key = (slug, side)
            trip = open_trip.get(key)
            if trip is None:
                trip = RoundTrip(slug=slug, asset=str(row.get("asset") or ""), side=side,
                                 mode=str(row.get("mode") or ""), epoch=int(_num(row.get("epoch"))))
                open_trip[key] = trip
                by_slug.setdefault(slug, []).append(trip)
            size, cost = _num(row.get("size")), _num(row.get("cost"))
            trip.fees += _num(row.get("fee"))
            if is_sell(row):
                trip.sold_shares += size
                trip.proceeds += -cost          # a sell is recorded as a credit
                trip.sold_ts = ts
                trip.sells += 1
                if trip.sold_shares >= trip.shares - 1e-6:
                    close(key)                  # flat again: this trip is done
            else:
                if trip.bought_ts is None:
                    trip.bought_ts = ts
                trip.last_buy_ts = ts
                trip.shares += size
                trip.cost += cost
                trip.buys += 1
                if row.get("expected") is not None:
                    trip.expected.append(_num(row.get("expected")))
        elif row.get("event") == "settle":
            # a settlement belongs to the window, so it lands on that window's
            # trips; the PnL is reported once, on the last of them
            trips = by_slug.get(slug) or []
            for t in trips:
                t.settled_ts = ts
                t.exited = t.exited or bool(row.get("exited"))
            if trips:
                last = trips[-1]
                last.pnl = _num(row.get("pnl"))
                last.balance_after = _num(row.get("balance")) or None
            for key in [k for k in open_trip if k[0] == slug]:
                close(key)

    done.extend(open_trip.values())
    done.sort(key=lambda t: (t.bought_ts if t.bought_ts is not None else 0.0))
    return [t.as_dict() for t in done]


def summary(trips: list[dict[str, Any]]) -> dict[str, Any]:
    """Headline numbers over a set of round trips."""
    closed = [t for t in trips if t["pnl"] is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    sold = [t for t in closed if t["status"] == "sold"]
    held = [t for t in closed if t["status"] == "settled"]
    holds = [t["hold_s"] for t in closed if t["hold_s"] is not None]
    return {
        "trips": len(trips),
        "open": sum(1 for t in trips if t["status"] == "open"),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(closed) - len(wins),
        "win_rate": round(len(wins) / len(closed), 4) if closed else None,
        "pnl": round(sum(t["pnl"] for t in closed), 4),
        "fees": round(sum(t["fees"] for t in trips), 4),
        "shares": round(sum(t["shares"] for t in trips), 2),
        "sold_early": len(sold),
        "held_to_resolution": len(held),
        "median_hold_s": round(sorted(holds)[len(holds) // 2], 1) if holds else None,
        "modes": sorted({t["mode"] for t in trips if t["mode"]}),
    }
