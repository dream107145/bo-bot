"""Pull a month of 15-minute up/down history for the parameter study.

    python scripts/fetch_15m_history.py spot     [--days 30]
    python scripts/fetch_15m_history.py markets  [--days 30] [--stride-minor 3]

spot     1-minute candles from Coinbase for every asset the venue lists
         (Bybit and Binance REST are blocked from this host; Coinbase lists
         all seven including BNB and HYPE). -> data/research/15m/spot_<ASSET>.parquet
markets  every 15m window (BTC, ETH) or every ``stride-minor``-th window (the
         rest) over the period: the Gamma row (outcome, volume) and the CLOB
         price history of the UP token at 1-minute fidelity.
         -> data/research/15m/markets.parquet (one row per window)
            data/research/15m/points.parquet  (one row per price sample)

Everything is public and unauthenticated. Requests are retried with backoff on
429/5xx and the pull is resumable: windows already on disk are skipped.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import pathlib
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

OUT = pathlib.Path("data/research/15m")
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
COINBASE = "https://api.exchange.coinbase.com"
ASSETS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE")
MAJOR = ("BTC", "ETH")
WINDOW_S = 900

log = logging.getLogger("fetch15m")


def get(url: str, timeout: float = 20.0, tries: int = 6):
    delay = 0.5
    for i in range(tries):
        req = urllib.request.Request(url, headers={"User-Agent": "tpb-research/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as f:
                return json.loads(f.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(delay); delay = min(delay * 2, 8.0); continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            if i < tries - 1:
                time.sleep(delay); delay = min(delay * 2, 8.0); continue
            raise
    return None


# ───────────────────────────────── spot ──────────────────────────────────

def fetch_spot(asset: str, days: int) -> pd.DataFrame:
    """Coinbase 1m candles, newest first, 300 per call; walk backwards."""
    end = int(time.time()) // 60 * 60
    start = end - days * 86400
    rows: list[list] = []
    cur_end = end
    while cur_end > start:
        cur_start = max(start, cur_end - 300 * 60)
        url = (f"{COINBASE}/products/{asset}-USD/candles?granularity=60"
               f"&start={dt.datetime.fromtimestamp(cur_start, dt.timezone.utc).isoformat()}"
               f"&end={dt.datetime.fromtimestamp(cur_end, dt.timezone.utc).isoformat()}")
        body = get(url)
        if body:
            rows.extend(body)
        cur_end = cur_start
        time.sleep(0.12)                     # ~8 req/s, under the public limit
    df = pd.DataFrame(rows, columns=["t", "low", "high", "open", "close", "volume"])
    df = df.drop_duplicates("t").sort_values("t").reset_index(drop=True)
    df["asset"] = asset
    return df


def cmd_spot(args) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for a in ASSETS:
        path = OUT / f"spot_{a}.parquet"
        df = fetch_spot(a, args.days)
        df.to_parquet(path, index=False)
        gaps = int((df["t"].diff().dropna() != 60).sum())
        log.info("%s: %d candles  %s .. %s  gaps=%d", a, len(df),
                 dt.datetime.fromtimestamp(df.t.min(), dt.timezone.utc).strftime("%m-%d %H:%M"),
                 dt.datetime.fromtimestamp(df.t.max(), dt.timezone.utc).strftime("%m-%d %H:%M"), gaps)


# ──────────────────────────────── markets ────────────────────────────────

def fetch_window(asset: str, epoch: int) -> tuple[dict | None, list[dict]]:
    slug = f"{asset.lower()}-updown-15m-{epoch}"
    row = get(f"{GAMMA}/markets/slug/{slug}")
    if not row:
        return None, []
    try:
        toks = json.loads(row["clobTokenIds"]); outs = [o.lower() for o in json.loads(row["outcomes"])]
        up_tok = toks[outs.index("up")]
        prices = json.loads(row.get("outcomePrices") or "[]")
        up_price = float(prices[outs.index("up")]) if prices else None
    except (KeyError, ValueError, TypeError, IndexError):
        return None, []
    meta = {
        "slug": slug, "asset": asset, "epoch": epoch, "up_token": up_tok,
        "outcome_up": None if up_price is None else int(up_price > 0.5),
        "resolved": bool(prices) and up_price in (0.0, 1.0),
        "volume": float(row.get("volumeNum") or 0.0),
        "closed": bool(row.get("closed")),
        "fee_rate": float((row.get("feeSchedule") or {}).get("rate") or 0.0),
    }
    hist = get(f"{CLOB}/prices-history?market={up_tok}&startTs={epoch - 180}"
               f"&endTs={epoch + WINDOW_S + 120}&fidelity=1") or {}
    pts = [{"slug": slug, "asset": asset, "epoch": epoch, "t": int(p["t"]), "p": float(p["p"])}
           for p in hist.get("history", [])]
    return meta, pts


def cmd_markets(args) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    mpath, ppath = OUT / "markets.parquet", OUT / "points.parquet"
    done: set[str] = set()
    metas: list[dict] = []
    points: list[dict] = []
    if mpath.exists():
        prev = pd.read_parquet(mpath); metas = prev.to_dict("records"); done = set(prev.slug)
        points = pd.read_parquet(ppath).to_dict("records") if ppath.exists() else []
        log.info("resuming: %d windows on disk", len(done))

    now = int(time.time())
    last_closed = now // WINDOW_S * WINDOW_S - WINDOW_S        # fully closed and settled
    first = last_closed - args.days * 86400
    jobs: list[tuple[str, int]] = []
    for a in ASSETS:
        stride = 1 if a in MAJOR else args.stride_minor
        k = 0
        for ep in range(first, last_closed, WINDOW_S):
            k += 1
            if (k - 1) % stride:
                continue
            if f"{a.lower()}-updown-15m-{ep}" not in done:
                jobs.append((a, ep))
    log.info("%d windows to fetch (%d assets, %d days)", len(jobs), len(ASSETS), args.days)

    n_ok = n_missing = 0
    t0 = time.time()
    with ThreadPoolExecutor(args.concurrency) as ex:
        futs = {ex.submit(fetch_window, a, ep): (a, ep) for a, ep in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                meta, pts = fut.result()
            except Exception as exc:                               # noqa: BLE001
                log.warning("%s: %s", futs[fut], exc); continue
            if meta is None:
                n_missing += 1
            else:
                n_ok += 1; metas.append(meta); points.extend(pts)
            if i % 250 == 0 or i == len(jobs):
                el = time.time() - t0
                log.info("%d/%d  ok=%d missing=%d  %.0fs elapsed  eta %.0fs",
                         i, len(jobs), n_ok, n_missing, el, el / i * (len(jobs) - i))
                pd.DataFrame(metas).to_parquet(mpath, index=False)
                pd.DataFrame(points).to_parquet(ppath, index=False)
    pd.DataFrame(metas).to_parquet(mpath, index=False)
    pd.DataFrame(points).to_parquet(ppath, index=False)
    log.info("done: %d windows, %d price points", len(metas), len(points))


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("spot"); s.add_argument("--days", type=int, default=30)
    m = sub.add_parser("markets"); m.add_argument("--days", type=int, default=30)
    m.add_argument("--stride-minor", type=int, default=3)
    m.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    {"spot": cmd_spot, "markets": cmd_markets}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
