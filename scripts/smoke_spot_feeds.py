"""Connect to every spot exchange for a few seconds and report what arrived.

    python scripts/smoke_spot_feeds.py --seconds 20 --assets BTC,ETH,HYPE
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from troll_poly_bot.feeds.spot import CompositeSpot, run_all   # noqa: E402


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--assets", default="BTC,ETH,SOL,XRP,DOGE,BNB,HYPE")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    assets = tuple(a.strip().upper() for a in args.assets.split(",") if a.strip())
    comp = CompositeSpot()
    stop = asyncio.Event()
    clock = lambda: time.time() * 1000.0                      # noqa: E731
    task = asyncio.create_task(run_all(assets, comp.update, clock, stop))
    await asyncio.sleep(args.seconds)
    # snapshot BEFORE stopping: closing three sockets can take seconds and
    # would age every quote past the freshness limit
    now = clock()
    views = {a: comp.view(a, now) for a in assets}
    counts = dict(comp.counts)
    stop.set()
    await task
    print(f"\nmessages per exchange: {counts}")
    print(f"{'asset':6}{'n_src':>6}{'median':>14}{'dev bps':>9}{'x-spread bps':>13}  sources")
    for a in assets:
        v = views[a]
        if v is None:
            print(f"{a:6}   none")
            continue
        print(f"{a:6}{v.n_sources:>6}{v.price:>14.6f}{v.deviation_bps:>9.2f}{v.cross_exchange_spread_bps:>13.2f}  "
              + ", ".join(f"{k}={p:.6f}" for k, p in sorted(v.sources.items())))


if __name__ == "__main__":
    asyncio.run(main())
