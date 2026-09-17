"""Probe the venue for every candidate asset and print what is listed, with fees.

    python scripts/smoke_discovery.py
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from troll_poly_bot.feeds.markets import parse_market                  # noqa: E402
from troll_poly_bot.market.discovery import AssetRegistry              # noqa: E402

GAMMA = "https://gamma-api.polymarket.com"


async def fetch(slug: str) -> dict | None:
    def _do():
        req = urllib.request.Request(f"{GAMMA}/markets?slug={urllib.parse.quote(slug)}",
                                     headers={"User-Agent": "troll-poly-bot/0.1"})
        with urllib.request.urlopen(req, timeout=12) as r:
            rows = json.load(r)
        return rows[0] if isinstance(rows, list) and rows else None
    try:
        return await asyncio.to_thread(_do)
    except Exception:
        return None


async def main() -> None:
    reg = AssetRegistry()
    t0 = time.perf_counter()
    assets = await reg.refresh(fetch)
    print(f"probed {len(reg.candidates)} candidates in {time.perf_counter() - t0:.1f}s -> listed: {assets}")
    for a in assets:
        meta = parse_market(reg.active[a])
        if meta is None:
            print(f"  {a}: row did not parse")
            continue
        print(f"  {a:5} {meta.market.slug:26} fee rate {meta.fee.rate} exp {meta.fee.exponent} takerOnly {meta.fee.taker_only} "
              f"liq {meta.liquidity:.0f} vol {meta.volume:.0f} twap {meta.twap_lookback_s:.0f}s  {meta.question[:40]}")


if __name__ == "__main__":
    asyncio.run(main())
