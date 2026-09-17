"""Loader for the windows the live bot archives to ``data/charts/<slug>.json``.

Each archive is one 5-minute market: the strike the bot struck (60s TWAP of
the Binance proxy at the window open), the outcome it recorded, and a price
path sampled every ~200ms carrying the Up/Down token prices, the touch on each
side (``ub/ua/db/da``, each None when that side of the book was empty) and
the Binance spot the bot held at that instant.

Two things this loader does that a naive ``json.load`` would not:

1. **Outcomes are re-graded from the closing book.** The bot recorded
   ``venue_up`` when Gamma still had the row and fell back to its *own*
   Binance-proxy TWAP when it did not. That fallback grades the trade with the
   same signal that produced it. The venue's own book at the bell (0.99/0.01)
   is the market's settled consensus and is used here instead; a window whose
   closing book is not decisive is flagged, not guessed.

2. **Every point is resampled onto a 1-second grid**, last-observation
   carried forward, so features, models and the replay all see exactly what
   the bot would have seen at that second -- never a later sample.
"""
from __future__ import annotations

import glob
import json
import math
import os
from dataclasses import dataclass, field

import numpy as np

TICK = 0.01
#: Time-left buckets used by every analysis, so tables line up.
TIME_BUCKETS: tuple[tuple[float, float], ...] = (
    (300.0, 240.0), (240.0, 180.0), (180.0, 120.0), (120.0, 60.0),
    (60.0, 30.0), (30.0, 10.0), (10.0, 0.0),
)


def time_bucket(left: float) -> str:
    for hi, lo in TIME_BUCKETS:
        if lo < left <= hi:
            return f"{int(lo)}-{int(hi)}s"
    return "0s" if left <= 0 else ">300s"


@dataclass(slots=True)
class Grid:
    """One window on a 1-second grid. Arrays are aligned; NaN where unknown."""
    t: np.ndarray            # epoch ms at each grid second
    left: np.ndarray         # seconds to close
    spot: np.ndarray         # Binance proxy spot the bot held
    up: np.ndarray           # Up token price (mid, or the quoted side once one-sided)
    ub: np.ndarray           # Up best bid  (NaN when absent)
    ua: np.ndarray           # Up best ask
    db: np.ndarray           # Down best bid
    da: np.ndarray           # Down best ask
    has_touch: np.ndarray    # bool: the sample carried the real touch


@dataclass(slots=True)
class ArchivedWindow:
    slug: str
    asset: str
    strike: float
    open_ts: float
    close_ts: float
    outcome_recorded: str            # what the bot wrote
    outcome: str | None              # bell-verified ("UP"/"DOWN") or None if undecidable
    outcome_source: str              # "bell" | "recorded" | "ambiguous"
    n_points: int
    first_left: float
    last_left: float
    grid: Grid = field(repr=False)

    @property
    def up_won(self) -> bool | None:
        return None if self.outcome is None else self.outcome == "UP"

    @property
    def reaches_bell(self) -> bool:
        return self.last_left <= 1.0

    @property
    def covers_open(self) -> bool:
        return self.first_left >= 250.0


def _grade_from_bell(points: list[dict]) -> str | None:
    tail = [p for p in points if p.get("left") is not None and p["left"] <= 0.5]
    if not tail:
        return None
    up = tail[-1].get("up")
    if up is None:
        return None
    if up >= 0.9:
        return "UP"
    if up <= 0.1:
        return "DOWN"
    return "AMBIGUOUS"


def _to_grid(points: list[dict], open_ts: float, close_ts: float) -> Grid:
    # grid seconds from 60s before the open to 5s after the close
    t0 = math.floor((open_ts - 60_000.0) / 1000.0) * 1000.0
    t1 = close_ts + 5_000.0
    n = int((t1 - t0) / 1000.0) + 1
    t = t0 + 1000.0 * np.arange(n)
    cols = {k: np.full(n, np.nan) for k in ("spot", "up", "ub", "ua", "db", "da")}
    touch = np.zeros(n, dtype=bool)
    pts = sorted(points, key=lambda p: p["t"])
    pt_t = np.array([p["t"] for p in pts], dtype=float)
    # index of the last point at or before each grid second (LOCF, causal)
    idx = np.searchsorted(pt_t, t, side="right") - 1
    for gi in range(n):
        j = idx[gi]
        if j < 0:
            continue
        p = pts[j]
        if t[gi] - p["t"] > 5_000.0:          # feed gap: nothing fresh, leave NaN
            continue
        for k in cols:
            v = p.get(k)
            if v is not None:
                cols[k][gi] = float(v)
        touch[gi] = "ua" in p or "ub" in p
    return Grid(
        t=t, left=(close_ts - t) / 1000.0, spot=cols["spot"], up=cols["up"],
        ub=cols["ub"], ua=cols["ua"], db=cols["db"], da=cols["da"], has_touch=touch,
    )


def load_window(path: str) -> ArchivedWindow | None:
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    pts = [p for p in (d.get("points") or []) if p.get("spot") is not None and p.get("t") is not None]
    strike = float(d.get("strike") or 0.0)
    if strike <= 0.0 or len(pts) < 50 or not d.get("outcome"):
        return None
    pts.sort(key=lambda p: p["t"])
    lefts = [p["left"] for p in pts if p.get("left") is not None]
    bell = _grade_from_bell(pts)
    if bell in ("UP", "DOWN"):
        outcome, source = bell, "bell"
    elif bell == "AMBIGUOUS":
        outcome, source = None, "ambiguous"
    else:
        outcome, source = d["outcome"], "recorded"
    return ArchivedWindow(
        slug=d["slug"], asset=d["asset"], strike=strike,
        open_ts=float(d["open_ts"]), close_ts=float(d["close_ts"]),
        outcome_recorded=d["outcome"], outcome=outcome, outcome_source=source,
        n_points=len(pts), first_left=max(lefts) if lefts else 0.0,
        last_left=min(lefts) if lefts else 999.0,
        grid=_to_grid(pts, float(d["open_ts"]), float(d["close_ts"])),
    )


def load_archives(directory: str = "data/charts", assets: tuple[str, ...] | None = None,
                  require_outcome: bool = True) -> list[ArchivedWindow]:
    out: list[ArchivedWindow] = []
    for f in sorted(glob.glob(os.path.join(directory, "*.json"))):
        try:
            w = load_window(f)
        except (OSError, ValueError, KeyError):
            continue
        if w is None:
            continue
        if assets and w.asset not in assets:
            continue
        if require_outcome and w.outcome is None:
            continue
        out.append(w)
    out.sort(key=lambda w: (w.open_ts, w.asset))
    return out


def marketable_ask(g: Grid, i: int, side: str) -> float | None:
    """What a taker would pay for one share of ``side`` at grid index ``i``.

    Uses the recorded touch when the sample carried it (so a one-sided book
    returns None: nobody was offering). Older samples without the touch
    rebuild a 1-tick market around the price -- faithful until a window is
    decided, flattering after, which is why callers should prefer windows
    with ``has_touch``.
    """
    ask = g.ua[i] if side == "UP" else g.da[i]
    if g.has_touch[i]:
        return None if np.isnan(ask) else float(ask)
    p = g.up[i] if side == "UP" else (1.0 - g.up[i] if not np.isnan(g.up[i]) else np.nan)
    if np.isnan(p) or p <= 0.0 or p >= 1.0:
        return None
    bid = max(TICK, math.floor(p / TICK + 1e-9) * TICK)
    return min(1.0 - TICK, round(bid + TICK, 2))


def summary(windows: list[ArchivedWindow]) -> str:
    by: dict[str, list[ArchivedWindow]] = {}
    for w in windows:
        by.setdefault(w.asset, []).append(w)
    lines = [f"{'asset':6}{'n':>5}{'bell':>6}{'recorded':>9}{'ambig':>6}{'open':>6}{'touch':>7}{'up%':>5}"]
    for a, ws in sorted(by.items()):
        n = len(ws)
        lines.append(
            f"{a:6}{n:>5}{sum(w.outcome_source == 'bell' for w in ws):>6}"
            f"{sum(w.outcome_source == 'recorded' for w in ws):>9}"
            f"{sum(w.outcome_source == 'ambiguous' for w in ws):>6}"
            f"{sum(w.covers_open for w in ws):>6}"
            f"{np.mean([w.grid.has_touch.mean() for w in ws]):>7.2f}"
            f"{100 * np.mean([w.outcome == 'UP' for w in ws if w.outcome]):>5.0f}"
        )
    return "\n".join(lines)
