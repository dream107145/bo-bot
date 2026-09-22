"""The durable record of what was won and lost, and how to bucket it by date.

Why this is a separate file from the trade log
----------------------------------------------
``data/live_trades.jsonl`` is the *run* ledger: a paper restart clears it,
because a restart also resets the balance and carrying old fills forward would
describe PnL the new run's equity does not account for. That is right for the
run view and useless for "what did I earn last month", which needs a record
that nothing truncates.

``data/earnings.jsonl`` is therefore append-only and never reset. One line per
settled window, tagged with the mode that produced it, so paper results and
real money can be read apart rather than silently summed.

Buckets are calendar buckets in the machine's own timezone: an operator asking
for "daily" means their day, not a rolling 24 hours from process start. Empty
periods inside the range are emitted as zeros, because a week with no trading
is information and a chart that closes the gap would hide it.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

EARNINGS_LOG = Path("data/earnings.jsonl")

PERIODS = ("day", "week", "month")
#: How many buckets each period shows by default, newest last.
DEFAULT_SPAN = {"day": 30, "week": 26, "month": 12}
MAX_SPAN = 400
SCOPES = ("all", "paper", "live")


@dataclass(slots=True)
class Settlement:
    ts: float                 # epoch milliseconds
    pnl: float
    mode: str
    live: bool
    asset: str = ""
    slug: str = ""
    exited: bool = False

    @property
    def when(self) -> datetime:
        return datetime.fromtimestamp(self.ts / 1000.0)


def append(row: dict[str, Any], path: Path | str | None = None) -> None:
    """Record one settled window. Never raises: a full disk must not stop trading.

    The path is resolved per call, not bound at import, so the destination can
    be redirected (tests, or a run pointed at a different data directory).
    """
    p = Path(path or EARNINGS_LOG)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except OSError:
        pass


def load(path: Path | str | None = None) -> list[Settlement]:
    """Every settlement on disk, oldest first. Bad lines are skipped, not fatal."""
    out: list[Settlement] = []
    try:
        text = Path(path or EARNINGS_LOG).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            ts = float(row["ts"])
            pnl = float(row.get("pnl") or 0.0)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if ts != ts or pnl != pnl:                       # NaN
            continue
        out.append(Settlement(
            ts=ts, pnl=pnl, mode=str(row.get("mode") or "paper"),
            live=bool(row.get("live")), asset=str(row.get("asset") or ""),
            slug=str(row.get("slug") or ""), exited=bool(row.get("exited")),
        ))
    out.sort(key=lambda s: s.ts)
    return out


# ------------------------------------------------------------------ buckets


def _bucket_start(d: date, period: str) -> date:
    if period == "week":
        return d - timedelta(days=d.weekday())          # ISO week, Monday
    if period == "month":
        return d.replace(day=1)
    return d


def _next_bucket(d: date, period: str) -> date:
    if period == "week":
        return d + timedelta(days=7)
    if period == "month":
        return date(d.year + 1, 1, 1) if d.month == 12 else date(d.year, d.month + 1, 1)
    return d + timedelta(days=1)


def _label(d: date, period: str) -> str:
    if period == "week":
        return f"{d:%d %b}"
    if period == "month":
        return f"{d:%b %Y}"
    return f"{d:%d %b}"


def _ms(d: date) -> float:
    return datetime(d.year, d.month, d.day).timestamp() * 1000.0


def aggregate(rows: Iterable[Settlement], period: str = "day", scope: str = "all",
              span: int | None = None, now: float | None = None) -> dict[str, Any]:
    """Calendar buckets of realised PnL, newest last, gaps filled with zeros."""
    period = period if period in PERIODS else "day"
    scope = scope if scope in SCOPES else "all"
    span = DEFAULT_SPAN[period] if span is None else min(max(int(span), 1), MAX_SPAN)

    rows = [r for r in rows
            if scope == "all" or (r.live if scope == "live" else not r.live)]

    totals: dict[date, dict[str, float]] = {}
    for r in rows:
        key = _bucket_start(r.when.date(), period)
        b = totals.setdefault(key, {"pnl": 0.0, "trades": 0.0, "wins": 0.0, "losses": 0.0})
        b["pnl"] += r.pnl
        b["trades"] += 1
        b["wins" if r.pnl > 0 else "losses"] += 1

    today = _bucket_start(datetime.fromtimestamp((now or time.time() * 1000.0) / 1000.0).date(), period)
    # walk back `span` buckets from today so the axis ends on the current period
    # even when nothing has settled in it yet
    edges: list[date] = [today]
    while len(edges) < span:
        d = edges[0]
        prev = (d - timedelta(days=1)) if period == "day" else \
               (d - timedelta(days=7)) if period == "week" else \
               _bucket_start((d - timedelta(days=1)), "month")
        edges.insert(0, prev)
    if totals:
        oldest = min(totals)
        while edges[0] > oldest and len(edges) < MAX_SPAN:
            d = edges[0]
            prev = (d - timedelta(days=1)) if period == "day" else \
                   (d - timedelta(days=7)) if period == "week" else \
                   _bucket_start((d - timedelta(days=1)), "month")
            edges.insert(0, prev)
        edges = edges[-max(span, 1):] if len(edges) > span else edges

    buckets = []
    cum = 0.0
    # the cumulative line must account for everything before the visible window
    first = edges[0] if edges else today
    cum = sum(v["pnl"] for k, v in totals.items() if k < first)
    opening = cum
    for d in edges:
        b = totals.get(d, {"pnl": 0.0, "trades": 0.0, "wins": 0.0, "losses": 0.0})
        cum += b["pnl"]
        buckets.append({
            "key": d.isoformat(),
            "label": _label(d, period),
            "start_ts": _ms(d),
            "end_ts": _ms(_next_bucket(d, period)),
            "pnl": round(b["pnl"], 4),
            "cum": round(cum, 4),
            "trades": int(b["trades"]),
            "wins": int(b["wins"]),
            "losses": int(b["losses"]),
        })

    traded = [b for b in buckets if b["trades"]]
    best = max(traded, key=lambda b: b["pnl"]) if traded else None
    worst = min(traded, key=lambda b: b["pnl"]) if traded else None
    window_pnl = sum(b["pnl"] for b in buckets)
    window_trades = sum(b["trades"] for b in buckets)
    window_wins = sum(b["wins"] for b in buckets)
    return {
        "period": period,
        "scope": scope,
        "buckets": buckets,
        "opening_cum": round(opening, 4),
        "totals": {
            "pnl": round(window_pnl, 4),
            "trades": window_trades,
            "wins": window_wins,
            "losses": window_trades - window_wins,
            "win_rate": round(window_wins / window_trades, 4) if window_trades else None,
            "per_trade": round(window_pnl / window_trades, 4) if window_trades else None,
            "periods_traded": len(traded),
            "all_time_pnl": round(sum(r.pnl for r in rows), 4),
            "all_time_trades": len(rows),
        },
        "best": best,
        "worst": worst,
        "modes": sorted({r.mode for r in rows}),
    }
