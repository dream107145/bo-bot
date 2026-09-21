"""Read-only view of a real Polymarket account.

Positions on Polymarket are on-chain, so the Data API serves them to anyone who
asks: **no key, no signature, no credentials of any kind**. All this needs is a
wallet address, and the worst a bug in here can do is show a wrong number.

That is the entire point of keeping it separate. The trading side of this repo
has no order-signing code and this module does not add any: it cannot place,
cancel or modify an order, and it never touches the bot's strategy, risk config
or paper exchange. It runs in the dashboard process, not the trading process,
so a hung HTTP call cannot stall a trading tick.

The address wanted here is the **proxy wallet** -- the Gnosis-Safe-style wallet
Polymarket trades from -- not the EOA that controls it. They are different
addresses and the API knows nothing about the latter.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

DATA_API = "https://data-api.polymarket.com"
#: A 20-byte hex address. Anything else is never sent to the API.
WALLET_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
TIMEOUT_S = 10.0
#: Enough rows to be useful on a dashboard, few enough to stay a small payload.
MAX_ROWS = 100


class AccountError(Exception):
    """The account could not be read. Never fatal -- the panel shows the reason."""


def normalise_wallet(wallet: str | None) -> str:
    """Validate and canonicalise, or raise. Guards the URL we are about to build."""
    w = (wallet or "").strip()
    if not WALLET_RE.match(w):
        raise AccountError(
            "not a wallet address: expected 0x followed by 40 hex characters")
    return w.lower()


def _rows(payload) -> list:
    """The API returns a bare list on some routes and {"data": [...]} on others."""
    if isinstance(payload, dict):
        payload = payload.get("data")
    return payload if isinstance(payload, list) else []


def _get(path: str, params: dict) -> object:
    url = f"{DATA_API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "troll-poly-bot/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        raise AccountError(f"{path} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AccountError(f"{path} unreachable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AccountError(f"{path} returned malformed JSON") from exc


def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def fetch_value(wallet: str) -> float | None:
    """Portfolio value in USDC -- open positions marked to market."""
    payload = _get("/v2/value", {"user": wallet})
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):                       # some routes wrap in a list
        data = data[0] if data else None
    if isinstance(data, dict):
        return _num(data.get("value"))
    return None


def fetch_positions(wallet: str, limit: int = MAX_ROWS) -> list[dict]:
    rows = _rows(_get("/v2/positions",
                      {"user": wallet, "limit": min(limit, MAX_ROWS)}))
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append({
            "title": r.get("title") or r.get("slug") or "",
            "slug": r.get("slug") or "",
            "outcome": r.get("outcome") or "",
            "size": _num(r.get("current_size"), 0.0),
            "avg_price": _num(r.get("avg_price")),
            "current_price": _num(r.get("current_price")),
            "value": _num(r.get("current_value"), 0.0),
            "cost": _num(r.get("total_cost_usdc")),
            "unrealized_pnl": _num(r.get("unrealized_pnl"), 0.0),
            "realized_pnl": _num(r.get("realized_pnl"), 0.0),
            "percent_pnl": _num(r.get("percent_pnl")),
            "redeemable": bool(r.get("redeemable")),
            "status": r.get("status") or "",
        })
    return out


def fetch_trades(wallet: str, limit: int = MAX_ROWS) -> list[dict]:
    rows = _rows(_get("/v2/trades",
                      {"user": wallet, "limit": min(limit, MAX_ROWS)}))
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        size = _num(r.get("size"), 0.0)
        price = _num(r.get("price"), 0.0)
        out.append({
            "ts": _num(r.get("timestamp"), 0.0),
            "side": (r.get("side") or "").upper(),
            "title": r.get("title") or r.get("slug") or "",
            "slug": r.get("slug") or "",
            "outcome": r.get("outcome") or "",
            "size": size,
            "price": price,
            "usdc": round(size * price, 4),
        })
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out


def snapshot(wallet: str, limit: int = MAX_ROWS) -> dict:
    """Everything the account panel shows, in one call.

    Each section carries its own error rather than failing the whole snapshot:
    a rate-limited positions call should not blank the balance that did load.
    """
    address = normalise_wallet(wallet)
    out: dict = {"wallet": address, "read_only": True}
    try:
        out["value"] = fetch_value(address)
    except AccountError as exc:
        out["value"], out["value_error"] = None, str(exc)
    try:
        positions = fetch_positions(address, limit)
        out["positions"] = positions
        out["positions_value"] = round(sum(p["value"] for p in positions), 4)
        out["unrealized_pnl"] = round(sum(p["unrealized_pnl"] for p in positions), 4)
    except AccountError as exc:
        out["positions"], out["positions_error"] = [], str(exc)
    try:
        out["trades"] = fetch_trades(address, limit)
    except AccountError as exc:
        out["trades"], out["trades_error"] = [], str(exc)
    return out
