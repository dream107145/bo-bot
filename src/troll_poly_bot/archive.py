"""The saved-market archive: every closed window the bot kept, indexed.

What is in there
----------------
``data/charts/<slug>.json`` is the durable research record of one 5-minute
window: the full 200 ms sample path, the strike, both token prices, our fills,
and how the venue actually resolved it. ``<slug>.png`` is the chart the
dashboard rendered for it. Roughly 175 MB accumulates over a few days.

Why an index
------------
Listing them means answering "which windows, in what order, matching what" --
and parsing 175 MB of sample paths to do that would make the page unusable.
So each window is summarised once, and the summary is cached on disk keyed by
the file's size and mtime. A rescan then costs one ``stat`` per file, and only
genuinely new windows are parsed. The cache is disposable: delete it and the
next listing rebuilds it.

The summary deliberately keeps no sample points. Opening one window fetches
that file whole; the list view never needs it.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

CHART_DIR = Path("data/charts")
INDEX_PATH = Path("data/charts_index.json")
#: A market slug and nothing else -- these values become filenames.
SLUG_RE = re.compile(r"^[a-z0-9]+-updown-\d+m-\d+$")
INDEX_VERSION = 1
DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 120


def summarise(doc: dict[str, Any], slug: str, has_png: bool = False) -> dict[str, Any]:
    """One archived window, reduced to what a list view needs."""
    points = doc.get("points") or []
    fills = doc.get("fills") or []
    strike = float(doc.get("strike") or 0.0)

    last_spot = None
    for p in reversed(points):
        v = p.get("spot")
        if isinstance(v, (int, float)) and v:
            last_spot = float(v)
            break
    move_bps = None
    if last_spot is not None and strike:
        move_bps = (last_spot - strike) / strike * 10_000.0

    def _first_last(key: str) -> tuple[float | None, float | None]:
        first = last = None
        for p in points:
            v = p.get(key)
            if isinstance(v, (int, float)):
                first = float(v)
                break
        for p in reversed(points):
            v = p.get(key)
            if isinstance(v, (int, float)):
                last = float(v)
                break
        return first, last

    up_open, up_close = _first_last("up")
    shares = sum(abs(float(f.get("size") or 0.0)) for f in fills)
    pnl = float(doc.get("pnl") or 0.0)
    return {
        "slug": slug,
        "asset": str(doc.get("asset") or "").upper(),
        "question": str(doc.get("question") or ""),
        "strike": strike,
        "open_ts": float(doc.get("open_ts") or 0.0),
        "close_ts": float(doc.get("close_ts") or 0.0),
        "outcome": (str(doc.get("outcome")) if doc.get("outcome") else None),
        "outcome_source": str(doc.get("outcome_source") or ""),
        "pnl": round(pnl, 4),
        "fills": len(fills),
        "shares": round(shares, 2),
        "traded": bool(fills),
        "points": len(points),
        "fee_rate": float((doc.get("fee") or {}).get("rate") or 0.0),
        "up_open": None if up_open is None else round(up_open, 4),
        "up_close": None if up_close is None else round(up_close, 4),
        "move_bps": None if move_bps is None else round(move_bps, 2),
        "last_spot": last_spot,
        "has_png": has_png,
    }


class ArchiveIndex:
    """Summaries of ``data/charts``, cached against file size and mtime."""

    def __init__(self, chart_dir: Path | str = CHART_DIR,
                 index_path: Path | str = INDEX_PATH) -> None:
        self.chart_dir = Path(chart_dir)
        self.index_path = Path(index_path)
        self._rows: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------ building

    def _load_cache(self) -> None:
        try:
            raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        if not isinstance(raw, dict) or raw.get("version") != INDEX_VERSION:
            raw = {}
        rows = raw.get("rows")
        self._rows = rows if isinstance(rows, dict) else {}
        self._loaded = True

    def _save_cache(self) -> None:
        try:
            self.index_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": INDEX_VERSION, "built": time.time(),
                                       "rows": self._rows}, separators=(",", ":")),
                           encoding="utf-8")
            tmp.replace(self.index_path)
        except OSError:
            pass                                    # a read-only disk must not break listing

    def refresh(self) -> dict[str, int]:
        """Bring the index in line with the directory. Returns what changed."""
        with self._lock:
            if not self._loaded:
                self._load_cache()
            try:
                files = {p.stem: p for p in self.chart_dir.glob("*.json")
                         if SLUG_RE.match(p.stem)}
            except OSError:
                files = {}
            pngs = set()
            try:
                pngs = {p.stem for p in self.chart_dir.glob("*.png")}
            except OSError:
                pass

            added = updated = 0
            for slug, path in files.items():
                try:
                    st = path.stat()
                except OSError:
                    continue
                stamp = [st.st_mtime, st.st_size]
                have = self._rows.get(slug)
                if have is not None and have.get("_stamp") == stamp:
                    if have.get("has_png") != (slug in pngs):
                        have["has_png"] = slug in pngs
                        updated += 1
                    continue
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(doc, dict):
                    continue
                row = summarise(doc, slug, has_png=slug in pngs)
                row["_stamp"] = stamp
                self._rows[slug] = row
                added += 1 if have is None else 0
                updated += 0 if have is None else 1

            gone = [s for s in self._rows if s not in files]
            for s in gone:
                del self._rows[s]

            if added or updated or gone:
                self._save_cache()
            return {"total": len(self._rows), "added": added,
                    "updated": updated, "removed": len(gone)}

    # ------------------------------------------------------------- reading

    def all_rows(self) -> list[dict[str, Any]]:
        self.refresh()
        with self._lock:
            return [{k: v for k, v in r.items() if k != "_stamp"} for r in self._rows.values()]

    def one(self, slug: str) -> Path | None:
        """The path of one archived window, or None if the slug is not ours."""
        if not SLUG_RE.match(slug or ""):
            return None
        p = self.chart_dir / f"{slug}.json"
        return p if p.is_file() else None

    def png(self, slug: str) -> Path | None:
        if not SLUG_RE.match(slug or ""):
            return None
        p = self.chart_dir / f"{slug}.png"
        return p if p.is_file() else None


# ------------------------------------------------------------------ queries


def filter_rows(rows: Iterable[dict[str, Any]], asset: str = "", outcome: str = "",
                traded: str = "", search: str = "") -> list[dict[str, Any]]:
    out = []
    asset = (asset or "").strip().upper()
    outcome = (outcome or "").strip().upper()
    search = (search or "").strip().lower()
    for r in rows:
        if asset and r.get("asset") != asset:
            continue
        if outcome and (r.get("outcome") or "").upper() != outcome:
            continue
        if traded == "yes" and not r.get("traded"):
            continue
        if traded == "no" and r.get("traded"):
            continue
        if search and search not in (r.get("slug", "") + " " + r.get("question", "")).lower():
            continue
        out.append(r)
    return out


def stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    traded = [r for r in rows if r.get("traded")]
    settled = [r for r in traded if r.get("outcome")]
    wins = sum(1 for r in settled if float(r.get("pnl") or 0.0) > 0)
    pnl = sum(float(r.get("pnl") or 0.0) for r in traded)
    ups = sum(1 for r in rows if (r.get("outcome") or "").upper() == "UP")
    decided = sum(1 for r in rows if r.get("outcome"))
    return {
        "windows": len(rows),
        "traded": len(traded),
        "pnl": round(pnl, 4),
        "wins": wins,
        "losses": len(settled) - wins,
        "win_rate": round(wins / len(settled), 4) if settled else None,
        "up_share": round(ups / decided, 4) if decided else None,
        "assets": sorted({r["asset"] for r in rows if r.get("asset")}),
    }


def page(rows: list[dict[str, Any]], page_no: int = 1,
         page_size: int = DEFAULT_PAGE_SIZE, sort: str = "recent") -> dict[str, Any]:
    """Newest first by default. Sorting is over the whole filtered set."""
    key = {
        "recent": lambda r: -float(r.get("close_ts") or 0.0),
        "oldest": lambda r: float(r.get("close_ts") or 0.0),
        "best": lambda r: -float(r.get("pnl") or 0.0),
        "worst": lambda r: float(r.get("pnl") or 0.0),
        "move": lambda r: -abs(float(r.get("move_bps") or 0.0)),
    }.get(sort, lambda r: -float(r.get("close_ts") or 0.0))
    ordered = sorted(rows, key=key)
    page_size = min(max(int(page_size), 1), MAX_PAGE_SIZE)
    total_pages = max(1, -(-len(ordered) // page_size))
    page_no = min(max(int(page_no), 1), total_pages)
    start = (page_no - 1) * page_size
    return {
        "rows": ordered[start:start + page_size],
        "page": page_no,
        "page_size": page_size,
        "total_rows": len(ordered),
        "total_pages": total_pages,
        "sort": sort,
    }
