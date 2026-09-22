"""Tunables a running bot re-reads from disk, so the dashboard can steer it.

Why a file
----------
The dashboard runs in its own process and may not even be the parent of the
bot (you can start one from a terminal and still see it on the page). A small
JSON file in ``data/`` is the one channel both ends always share, it survives
a dashboard restart, and it is inspectable and editable by hand when the page
is not running.

What can change while the bot runs
---------------------------------
Only the values in :data:`TUNABLES`. Each one is clamped to a range that
cannot wedge the bot, and anything unknown or unparseable is ignored with a
message rather than applied. Things that decide what the process *is* --
paper versus real money, the bankroll, which assets, which spot exchanges --
are start-time arguments, not tunables, and are marked ``restart=True`` so
the page can say so instead of pretending a change took effect.

Scoping
-------
Running a paper bot and a real-money bot side by side out of one directory is
normal here, and they must be steerable apart. The file therefore holds three
sections::

    {"all": {...}, "paper": {...}, "live": {...}}

A bot applies ``all`` first, then its own section, so ``live`` overrides
``all`` for the real-money process only.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONTROL_PATH = Path("data/controls.json")
KILL_PATH = Path("data/KILL")
SCOPES = ("all", "paper", "live")


@dataclass(frozen=True, slots=True)
class Tunable:
    """One settable value: where it lives, what it may be, how to say it."""
    key: str                 # "engine.min_net_edge" -- group.attribute
    kind: str                # float | int | bool
    lo: float
    hi: float
    label: str
    group: str               # UI grouping
    unit: str = ""
    help: str = ""
    #: ``live`` means the value only exists on a real-money exchange.
    applies_to: str = "any"
    step: float = 0.01

    @property
    def target(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def attr(self) -> str:
        return self.key.split(".", 1)[1]

    def coerce(self, value: Any) -> tuple[Any | None, str | None]:
        """(clean value, error). Clamps silently; rejects nonsense loudly."""
        if self.kind == "bool":
            if isinstance(value, bool):
                return value, None
            if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "on", "off"):
                return value.lower() in ("true", "1", "on"), None
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value), None
            return None, f"{self.key}: expected true or false, got {value!r}"
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None, f"{self.key}: expected a number, got {value!r}"
        if num != num or num in (float("inf"), float("-inf")):
            return None, f"{self.key}: not a finite number"
        num = min(max(num, self.lo), self.hi)
        return (int(round(num)), None) if self.kind == "int" else (num, None)


def _t(key, kind, lo, hi, label, group, **kw) -> Tunable:
    return Tunable(key=key, kind=kind, lo=lo, hi=hi, label=label, group=group, **kw)


#: Everything the dashboard may change on a running bot.
TUNABLES: dict[str, Tunable] = {t.key: t for t in (
    # ---- what counts as an edge -----------------------------------------
    _t("engine.min_net_edge", "float", 0.0, 0.5, "Minimum net edge", "Edge",
       unit="prob", step=0.005,
       help="Required edge per share AFTER fee and slippage. Lower trades more and thinner."),
    _t("engine.market_blend", "float", 0.0, 1.0, "Market blend", "Edge",
       unit="weight", step=0.05,
       help="Weight on the venue mid in the blended probability. 1.0 trusts the market entirely."),
    _t("engine.expected_slippage", "float", 0.0, 0.1, "Expected slippage", "Edge", step=0.001,
       help="Charged against the edge before the gate. Raise it if fills come in worse than quoted."),
    _t("engine.confidence_gate_mult", "float", 0.0, 5.0, "Confidence gate", "Edge", step=0.1,
       help="Edge after costs must beat this many model error bands, or the trade is noise."),
    _t("engine.uncertainty_charge", "float", 0.0, 5.0, "Uncertainty charge", "Edge", step=0.1,
       help="Fraction of the model's error band subtracted from the edge."),

    # ---- which books are worth taking -----------------------------------
    _t("engine.min_trade_price", "float", 0.01, 0.5, "Minimum price", "Book filters", step=0.01,
       help="Never pay less than this per share. Below it the fee curve and the tick grid dominate."),
    _t("engine.max_trade_price", "float", 0.5, 0.99, "Maximum price", "Book filters", step=0.01,
       help="Never pay more than this per share."),
    _t("engine.max_spread", "float", 0.005, 0.5, "Maximum spread", "Book filters", step=0.005,
       help="Skip a book wider than this. A wide book means the exit costs more than the edge."),
    _t("engine.min_depth_shares", "float", 0.0, 1000.0, "Minimum depth", "Book filters",
       unit="shares", step=1,
       help="Resting shares required at our limit before we size anything."),
    _t("engine.cross_ticks", "int", 0, 5, "Cross ticks", "Book filters", unit="ticks", step=1,
       help="How many ticks through the ask the limit is placed, to make a fill-or-kill land."),

    # ---- when in the window ---------------------------------------------
    # Bounds here are validation only (any window the venue could list); the
    # dashboard gets them scaled to the window actually being traded (spec()).
    _t("engine.trade_window_start_s", "float", 10.0, 3600.0, "Window opens at", "Timing",
       unit="s left", step=5,
       help="Seconds remaining when trading may start."),
    _t("engine.trade_window_end_s", "float", 0.0, 3590.0, "Window closes at", "Timing",
       unit="s left", step=5,
       help="Stop this many seconds before resolution. Books go one-sided near the end."),
    _t("engine.max_spot_age_ms", "float", 100.0, 20000.0, "Max spot age", "Timing", unit="ms", step=100,
       help="Refuse to trade on a spot price older than this."),
    _t("engine.max_book_age_ms", "float", 100.0, 20000.0, "Max book age", "Timing", unit="ms", step=100,
       help="Refuse to trade on an order book older than this."),

    # ---- getting out ------------------------------------------------------
    _t("engine.take_profit_enabled", "bool", 0, 1, "Take profit", "Exits",
       help="Sell into a risen bid instead of holding to the oracle print."),
    _t("engine.take_profit_delta", "float", 0.005, 0.9, "Take profit at", "Exits", unit="prob", step=0.005,
       help="Bid must rise this far above our average entry before we bank it."),
    _t("engine.stop_loss_enabled", "bool", 0, 1, "Stop loss", "Exits",
       help="Cut a position whose window has decided against us."),
    _t("engine.stop_loss_delta", "float", 0.01, 0.99, "Stop loss at", "Exits", unit="prob", step=0.01,
       help="Sell when the bid falls this far below our average entry."),

    # ---- how big ----------------------------------------------------------
    _t("risk.kelly_fraction", "float", 0.0, 1.0, "Kelly fraction", "Sizing", step=0.01,
       help="Fraction of the full Kelly stake. Smaller bets, more of them."),
    _t("risk.min_order_usdc", "float", 0.5, 100.0, "Minimum order", "Sizing", unit="USDC", step=0.5,
       help="Below this a trade is not worth the fee. Note the venue also demands 5 shares."),
    _t("risk.max_position_usdc", "float", 0.5, 1000.0, "Max per market", "Sizing", unit="USDC", step=0.5,
       help="Cap on one window's stake. Divided by the price, it must still buy 5 shares."),
    _t("risk.max_asset_exposure_usdc", "float", 1.0, 5000.0, "Max per asset", "Sizing",
       unit="USDC", step=1,
       help="Cap across every open window of one asset."),
    _t("risk.max_epoch_exposure_usdc", "float", 1.0, 5000.0, "Max per epoch", "Sizing",
       unit="USDC", step=1,
       help="Cap across all assets sharing a window. They are one correlated bet -- and with "
            "5m and 15m both running, so are the 5m windows inside a 15m one."),
    _t("risk.max_total_exposure_usdc", "float", 1.0, 10000.0, "Max total open", "Sizing",
       unit="USDC", step=1,
       help="Cap on everything open at once."),
    _t("risk.max_concurrent_positions", "int", 1, 50, "Max open positions", "Sizing", step=1,
       help="How many windows we may be in simultaneously."),
    _t("risk.max_entries_per_market", "int", 1, 10, "Entries per window", "Sizing", step=1,
       help="How many times one market may be bought in one window. 1 = once. More lets the bot "
            "add on the SAME side when every gate passes again, up to the per-market cap."),
    _t("risk.reentry_cooldown_s", "float", 0.0, 600.0, "Re-entry cooldown", "Sizing", unit="s", step=5,
       help="Minimum seconds after a fill before the same market may be bought again."),

    # ---- when to stop -----------------------------------------------------
    _t("risk.max_daily_loss_usdc", "float", 1.0, 5000.0, "Daily loss limit", "Circuit breakers",
       unit="USDC", step=1,
       help="Strategy-level halt once the day's realised losses reach this."),
    _t("risk.max_drawdown_usdc", "float", 1.0, 5000.0, "Drawdown limit", "Circuit breakers",
       unit="USDC", step=1,
       help="Halt once equity falls this far from its peak."),

    # ---- the real-money envelope -----------------------------------------
    _t("caps.max_order_usdc", "float", 0.5, 500.0, "Cap per order", "Real-money caps",
       unit="USDC", step=0.5, applies_to="live",
       help="Hard cap checked at the venue adapter. Must exceed 5 shares at your price, "
            "or every order is refused."),
    _t("caps.max_open_usdc", "float", 1.0, 2000.0, "Cap on open notional", "Real-money caps",
       unit="USDC", step=1, applies_to="live",
       help="Hard cap on everything open at the venue."),
    _t("caps.max_daily_loss_usdc", "float", 1.0, 2000.0, "Cap on daily loss", "Real-money caps",
       unit="USDC", step=1, applies_to="live",
       help="Halt the venue adapter for the rest of the run at this realised loss."),
    _t("caps.max_orders_per_hour", "int", 1, 1000, "Cap on orders per hour", "Real-money caps",
       unit="orders", step=1, applies_to="live",
       help="Rate limit enforced before anything is signed."),
)}

#: Start-time only. Listed so the page can show them and say a restart is due.
START_ARGS: dict[str, dict[str, Any]] = {
    "balance": {"label": "Bankroll", "kind": "float", "unit": "USDC",
                "help": "What the risk caps scale to. In live mode it is capped to the account balance."},
    "assets": {"label": "Assets", "kind": "text",
               "help": "Comma separated, e.g. BTC,ETH. Empty means every asset the venue lists."},
    "exchanges": {"label": "Spot exchanges", "kind": "text",
                  "help": "Sources for the composite spot price."},
}

GROUP_ORDER = ("Edge", "Book filters", "Timing", "Exits", "Sizing",
               "Circuit breakers", "Real-money caps")


#: How the timing knobs are presented for a window of ``window_s`` seconds:
#: (upper bound, help). The stored value is always in that window's own
#: seconds-left, so what the page shows is what the bot compares the clock to.
def _timing_presentation(window_s: float) -> dict[str, tuple[float, str]]:
    w = float(window_s)
    mins = f"{w / 60:g}-minute"
    return {
        "engine.trade_window_start_s": (
            max(w - 5.0, 10.0),
            f"Seconds remaining when trading may start. {w - 5:g} is the whole {mins} window "
            f"(5 s after the open); {w / 2:g} would skip its first half."),
        "engine.trade_window_end_s": (
            max(w - 10.0, 0.0),
            f"Stop this many seconds before the {mins} window resolves. Books go one-sided "
            f"near the end: at 15m over half are already decided with 60-180 s left."),
    }


def spec(window_s: float = 300.0) -> list[dict[str, Any]]:
    """The tunable catalogue, as the dashboard renders it, for a bot trading
    windows of ``window_s`` seconds (the timing rows scale with it)."""
    timing = _timing_presentation(window_s)
    out = []
    for group in GROUP_ORDER:
        for t in TUNABLES.values():
            if t.group != group:
                continue
            hi, help_ = t.hi, t.help
            if t.key in timing:
                hi, help_ = min(timing[t.key][0], t.hi), timing[t.key][1]
            out.append({"key": t.key, "kind": t.kind, "lo": t.lo, "hi": hi, "step": t.step,
                        "label": t.label, "group": t.group, "unit": t.unit, "help": help_,
                        "applies_to": t.applies_to, "window_s": w if (w := window_s) and t.key in timing else None})
    return out


def validate(patch: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """(values that will be applied, complaints about the rest)."""
    clean: dict[str, Any] = {}
    errors: list[str] = []
    if not isinstance(patch, dict):
        return {}, ["expected an object of key -> value"]
    for key, raw in patch.items():
        tunable = TUNABLES.get(key)
        if tunable is None:
            errors.append(f"{key}: not a tunable")
            continue
        value, err = tunable.coerce(raw)
        if err:
            errors.append(err)
        else:
            clean[key] = value
    return clean, errors


def _coherent(values: dict[str, Any]) -> list[str]:
    """Pairs that would contradict each other. Reported, not silently fixed."""
    warn = []
    lo, hi = values.get("engine.min_trade_price"), values.get("engine.max_trade_price")
    if lo is not None and hi is not None and lo >= hi:
        warn.append("minimum price is not below maximum price")
    start, end = values.get("engine.trade_window_start_s"), values.get("engine.trade_window_end_s")
    if start is not None and end is not None and start <= end:
        warn.append("the trade window opens at or before it closes, so nothing can trade")
    floor, cap = values.get("risk.min_order_usdc"), values.get("risk.max_position_usdc")
    if floor is not None and cap is not None and floor > cap:
        warn.append("the minimum order is larger than the per-market cap, so nothing can size")
    order_cap = values.get("caps.max_order_usdc")
    if order_cap is not None and floor is not None and order_cap < floor:
        warn.append(f"the real-money cap of {order_cap:g} USDC is below the {floor:g} USDC "
                    "minimum order, so every order will be refused")
    return warn


class ControlFile:
    """Read and write ``data/controls.json``. Safe to call from either process."""

    def __init__(self, path: Path | str = CONTROL_PATH) -> None:
        self.path = Path(path)
        self._mtime: float | None = None

    # ------------------------------------------------------------- reading

    def read(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"updated_ts": None, **{s: {} for s in SCOPES}}
        if not isinstance(raw, dict):
            return {"updated_ts": None, **{s: {} for s in SCOPES}}
        out: dict[str, Any] = {"updated_ts": raw.get("updated_ts")}
        for scope in SCOPES:
            section = raw.get(scope)
            out[scope] = section if isinstance(section, dict) else {}
        return out

    def changed(self) -> bool:
        """Has the file been written since we last looked? Cheap enough to poll."""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._mtime:
            self._mtime = mtime
            return True
        return False

    def merged(self, scope: str) -> tuple[dict[str, Any], list[str]]:
        """``all`` overlaid with one scope's own section, validated."""
        raw = self.read()
        combined = dict(raw.get("all") or {})
        if scope in SCOPES and scope != "all":
            combined.update(raw.get(scope) or {})
        return validate(combined)

    # ------------------------------------------------------------- writing

    def write_scope(self, scope: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Merge ``patch`` into one scope. Returns what was stored plus notes."""
        if scope not in SCOPES:
            return {"ok": False, "errors": [f"unknown scope {scope!r}"], "values": {}, "warnings": []}
        clean, errors = validate(patch)
        raw = self.read()
        section = dict(raw.get(scope) or {})
        section.update(clean)
        payload = {"updated_ts": time.time(),
                   **{s: (section if s == scope else (raw.get(s) or {})) for s in SCOPES}}
        self._atomic_write(payload)
        merged, _ = self.merged("live" if scope == "live" else "paper" if scope == "paper" else "all")
        return {"ok": not errors, "errors": errors, "values": clean,
                "warnings": _coherent(merged or clean), "scope": scope}

    def replace_scope(self, scope: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Set one scope to exactly ``patch``, so emptied fields are removed.

        ``write_scope`` merges, which can only ever add overrides. The page
        needs to be able to take one away again, and does that by sending the
        whole section it is showing.
        """
        if scope not in SCOPES:
            return {"ok": False, "errors": [f"unknown scope {scope!r}"], "values": {}, "warnings": []}
        clean, errors = validate(patch)
        raw = self.read()
        payload = {"updated_ts": time.time(),
                   **{s: (clean if s == scope else (raw.get(s) or {})) for s in SCOPES}}
        self._atomic_write(payload)
        merged, _ = self.merged(scope)
        return {"ok": not errors, "errors": errors, "values": clean,
                "warnings": _coherent(merged), "scope": scope}

    def clear_scope(self, scope: str) -> dict[str, Any]:
        """Drop one scope's overrides so the bot falls back to its defaults."""
        if scope not in SCOPES:
            return {"ok": False, "errors": [f"unknown scope {scope!r}"]}
        raw = self.read()
        payload = {"updated_ts": time.time(),
                   **{s: ({} if s == scope else (raw.get(s) or {})) for s in SCOPES}}
        self._atomic_write(payload)
        return {"ok": True, "errors": [], "scope": scope,
                "note": "the bot keeps the values already applied until it restarts"}

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def current_values(cfg, exchange=None) -> dict[str, Any]:
    """What the bot is running with right now, keyed like the tunables."""
    out: dict[str, Any] = {}
    for key, t in TUNABLES.items():
        holder = {"engine": getattr(cfg, "engine", None),
                  "risk": getattr(cfg, "risk", None),
                  "caps": getattr(exchange, "caps", None)}.get(t.target)
        if holder is None or not hasattr(holder, t.attr):
            continue
        value = getattr(holder, t.attr)
        out[key] = value if t.kind == "bool" else (int(value) if t.kind == "int" else float(value))
    return out


def apply_to(values: dict[str, Any], cfg, exchange=None) -> list[str]:
    """Set the validated values on the live objects. Returns what changed."""
    changed: list[str] = []
    for key, value in values.items():
        t = TUNABLES.get(key)
        if t is None:
            continue
        holder = {"engine": getattr(cfg, "engine", None),
                  "risk": getattr(cfg, "risk", None),
                  "caps": getattr(exchange, "caps", None)}.get(t.target)
        if holder is None or not hasattr(holder, t.attr):
            continue
        before = getattr(holder, t.attr)
        if isinstance(before, bool) or t.kind == "bool":
            same = bool(before) == bool(value)
        else:
            same = abs(float(before) - float(value)) < 1e-12
        if same:
            continue
        try:
            setattr(holder, t.attr, value)
        except (AttributeError, TypeError, ValueError):
            continue
        fmt = (lambda v: str(bool(v)).lower()) if t.kind == "bool" else (lambda v: f"{v:g}")
        changed.append(f"{t.label}: {fmt(before)} -> {fmt(value)}")
    return changed


# ------------------------------------------------------------- kill switch


def kill_active(path: Path | str = KILL_PATH) -> bool:
    return Path(path).exists()


def set_kill(active: bool, path: Path | str = KILL_PATH) -> bool:
    """Create or remove the kill file. Returns the state afterwards."""
    p = Path(path)
    try:
        if active:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"halted from the dashboard at {time.strftime('%Y-%m-%d %H:%M:%S')}\n",
                         encoding="utf-8")
        else:
            p.unlink(missing_ok=True)
    except OSError:
        pass
    return p.exists()
