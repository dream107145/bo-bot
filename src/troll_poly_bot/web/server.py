"""Local dashboard server.

Stdlib only, bound to loopback. This drives the *real* Python model -- the same
pricer, paper engine and latency model the tests cover -- rather than a second
implementation in JavaScript that would quietly drift out of agreement with it.

    python -m troll_poly_bot.web

Runs are executed on a worker thread so the UI stays responsive, and can be
cancelled: the sim takes a progress callback that aborts when it returns False.
"""
from __future__ import annotations

import contextlib
import json
import os
import logging
import mimetypes
import re
import threading
import urllib.parse
import time
import traceback
import uuid
import subprocess
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .. import archive, control, earnings, ledger
from ..execution.latency import PROFILES
from ..feeds import account
from ..sim import SimResult, run_sim
from . import ws
from .schema import apply_params, build_schema

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
LIVE_STATE = Path("data/live_state.json")
TRADE_LOG = Path("data/live_trades.jsonl")


def _filter_live(payload: dict, chart: str | None, since: int | None = None) -> dict:
    """Trim the state to what one page needs.

    The bot writes every live market's full 5-minute history ten times a
    second; the page only ever draws ONE of them. Sending all of them at that
    rate is most of the bytes for nothing -- the full state is ~9 MB, one
    market ~375 KB. ``history_meta`` keeps the point count per market so the
    picker and the archive exporter still know what exists without receiving
    it.

    ``since`` trims further to the points the client does not have yet, which
    is what makes a 100 ms poll affordable: a steady-state tick is a handful
    of points rather than the whole window. ``history_partial`` tells the
    client whether to append or replace. When the client is further behind
    than the buffer reaches, every point is new, so a full replace is both
    what it gets and what it wants.
    """
    hist = payload.get("price_history") or {}
    payload["history_meta"] = {slug: len(pts) for slug, pts in hist.items()}
    if chart is None:
        # nothing selected: the page draws no chart, so it needs no history
        payload["price_history"] = {}
        return payload
    if chart not in hist:
        payload["price_history"] = {}
        return payload
    pts = hist[chart]
    if since is not None:
        fresh = [p for p in pts if p.get("t", 0) > since]
        # an empty delta for a market we DO have is still {slug: []}, not {}:
        # it means "nothing new, keep what you hold", not "this market is gone"
        payload["history_partial"] = len(fresh) < len(pts)
        pts = fresh
    payload["price_history"] = {chart: pts}
    return payload


DEFAULT_HISTORY_PAGE_SIZE = 25
MAX_HISTORY_PAGE_SIZE = 200


def _qs_int(qs: dict[str, list[str]], key: str, default: int) -> int:
    """One query param, coerced to int. Garbage or absent falls back quietly --
    a malformed ``?page=`` should not 500 a dashboard poll."""
    try:
        return int((qs.get(key) or [default])[0])
    except (TypeError, ValueError):
        return default


def _history(page: int = 1, page_size: int = DEFAULT_HISTORY_PAGE_SIZE,
             view: str = "trips", mode: str = "all") -> dict:
    """Every fill and settlement the live bot has ever written, across restarts.

    The dashboard state is per-process: a restart starts a fresh exchange at
    its --balance with an empty ledger, and stop() removes the state file. The
    trade log is append-only and survives all of that, so it is the record --
    and it only grows, across days or weeks of paper trading. The table is
    therefore paginated rather than hard-capped: KPIs and the equity curve
    still summarise every row on disk, but ``rows`` returns one page, newest
    first, so the operator can page back through the whole history instead
    of only ever seeing the most recent slice of it.
    """
    rows = ledger.load(TRADE_LOG)
    if mode in ("paper", "live"):
        want_live = mode == "live"
        rows = [r for r in rows if bool(r.get("live")) == want_live]
    settles = [r for r in rows if r.get("event") == "settle"]
    fills = [r for r in rows if r.get("event") == "fill"]
    realised, curve = 0.0, []
    for r in settles:
        pnl = float(r.get("pnl") or 0.0)
        realised += pnl
        curve.append({"ts": r.get("ts"), "slug": r.get("slug"),
                      "pnl": round(pnl, 4), "cum": round(realised, 4)})

    # One position is several events -- often two partial entries, a sell and
    # a settlement -- which in a flat list reads as the same trade repeated.
    # The default view folds them into round trips, each with its own buy and
    # sell time; the raw event stream stays available underneath.
    trips = ledger.round_trips(rows)
    view = view if view in ("trips", "events") else "trips"
    listed = (trips[::-1] if view == "trips" else rows[::-1])   # newest first

    page_size = min(max(page_size, 1), MAX_HISTORY_PAGE_SIZE)
    total_rows = len(listed)
    total_pages = max(1, -(-total_rows // page_size))          # ceil div
    page = min(max(page, 1), total_pages)
    start = (page - 1) * page_size
    return {
        "fills": len(fills),
        "settled": len(settles),
        "wins": sum(1 for r in settles if float(r.get("pnl") or 0.0) > 0),
        "losses": sum(1 for r in settles if float(r.get("pnl") or 0.0) <= 0),
        "realised_pnl": round(realised, 4),
        "curve": curve,
        "view": view,
        "mode": mode,
        "modes": sorted({str(r.get("mode")) for r in ledger.load(TRADE_LOG) if r.get("mode")}),
        "trips_summary": ledger.summary(trips),
        "rows": listed[start:start + page_size],
        "events_total": len(rows),
        "trips_total": len(trips),
        "page": page,
        "page_size": page_size,
        "total_rows": total_rows,
        "total_pages": total_pages,
    }
class _EarningsCache:
    """Calendar-bucketed PnL, re-read only when the log actually grows.

    The earnings log is append-only and never truncated, so it is the one file
    here that grows without bound. Parsing it on every dashboard poll would be
    wasteful; parsing it when its size or mtime changes is exact, because
    appends always move both.
    """

    def __init__(self) -> None:
        self._rows: list[earnings.Settlement] = []
        self._stamp: tuple[float, int] | None = None
        self._lock = threading.Lock()

    def _fresh(self) -> list[earnings.Settlement]:
        try:
            st = earnings.EARNINGS_LOG.stat()
            stamp = (st.st_mtime, st.st_size)
        except OSError:
            stamp = (0.0, 0)
        with self._lock:
            if stamp != self._stamp:
                self._rows = earnings.load()
                self._stamp = stamp
            return self._rows

    def get(self, period: str, scope: str, span: int | None) -> dict:
        return earnings.aggregate(self._fresh(), period=period, scope=scope, span=span)


EARNINGS = _EarningsCache()
CONTROLS = control.ControlFile()
ARCHIVE = archive.ArchiveIndex()


def _controls_payload() -> dict:
    """The catalogue, what is stored per scope, and the kill switch."""
    stored = CONTROLS.read()
    return {
        "spec": control.spec(),
        "start_args": control.START_ARGS,
        "scopes": list(control.SCOPES),
        "stored": {s: stored.get(s) or {} for s in control.SCOPES},
        "updated_ts": stored.get("updated_ts"),
        "kill_active": control.kill_active(),
        "kill_file": str(control.KILL_PATH),
    }


CHART_DIR = Path("data/charts")
#: A market slug, and nothing else — this value becomes a filename.
SLUG_RE = re.compile(r"^[a-z0-9]+-updown-\d+m-\d+$")
MAX_PNG_BYTES = 4 * 1024 * 1024
MAX_TRADE_ROWS = 1500          # downsample the scatter; the browser gains nothing from 40k


class Job:
    def __init__(self, job_id: str, params: dict[str, Any], profiles: list[str]) -> None:
        self.id = job_id
        self.params = params
        self.profiles = profiles
        self.status = "queued"          # queued | running | done | error | cancelled
        self.progress = 0.0
        self.stage = ""
        self.results: list[dict] = []
        self.error: str | None = None
        self.started = time.time()
        self.finished: float | None = None
        self.cancel = threading.Event()

    def snapshot(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "progress": round(self.progress, 3),
            "stage": self.stage,
            "results": self.results,
            "error": self.error,
            "elapsed_s": round((self.finished or time.time()) - self.started, 2),
        }


def _finite(v: float | None) -> float | None:
    """NaN and inf are not JSON. A single one anywhere breaks the entire
    payload, so nothing numeric leaves this module without passing through."""
    if v is None:
        return None
    v = float(v)
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _downsample(rows: list[dict], limit: int) -> list[dict]:
    if len(rows) <= limit:
        return rows
    step = len(rows) / limit
    return [rows[int(i * step)] for i in range(limit)]


def serialise(result: SimResult, starting_balance: float) -> dict[str, Any]:
    cal = result.calibration()
    return {
        "profile": result.profile_name,
        "reaction_lag_ms": round(result.reaction_lag_ms, 1),
        "pnl": round(result.pnl, 2),
        "final_equity": round(result.final_equity, 2),
        "return_pct": round(100.0 * result.pnl / max(starting_balance, 1e-9), 2),
        "max_drawdown": _finite(round(result.max_drawdown(), 2)),
        "n_trades": result.n_trades,
        "n_windows_traded": result.n_windows_traded,
        "latency": {k: _finite(round(v, 4)) for k, v in result.latency.items()},
        "skips": result.skips,
        "calibration": {k: _finite(round(v, 5)) for k, v in cal.items()} if cal else {},
        "equity_curve": result.equity_curve,
        "trades": _downsample(
            [
                {
                    "fair": _finite(t["fair"]),
                    "price": round(t["price"], 4),
                    "size": round(t["size"], 2),
                    "won": t.get("won", 0.0),
                    "secs_left": round(t["secs_left"], 2),
                }
                for t in result.trades
            ],
            MAX_TRADE_ROWS,
        ),
    }


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in ("done", "error", "cancelled"):
            return False
        job.cancel.set()
        return True

    def submit(self, params: dict[str, Any], profiles: list[str]) -> Job:
        job = Job(uuid.uuid4().hex[:12], params, profiles)
        with self._lock:
            self._jobs[job.id] = job
            # keep the map from growing without bound across a long session
            if len(self._jobs) > 40:
                for old in sorted(self._jobs.values(), key=lambda j: j.started)[:10]:
                    if old.status in ("done", "error", "cancelled"):
                        self._jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        return job

    def _run(self, job: Job) -> None:
        job.status = "running"
        try:
            sim_cfg, bot_cfg = apply_params(job.params)
            chosen = [p for p in job.profiles if p in PROFILES] or ["home_broadband"]
            total = len(chosen)

            for i, name in enumerate(chosen):
                if job.cancel.is_set():
                    job.status = "cancelled"
                    return
                job.stage = f"{name} ({i + 1}/{total})"

                def progress(done: int, n: int, _i=i) -> bool:
                    job.progress = (_i + done / max(n, 1)) / total
                    return not job.cancel.is_set()

                result = run_sim(sim_cfg, bot_cfg, PROFILES[name], progress=progress)
                job.results.append(serialise(result, bot_cfg.starting_balance))

            job.progress = 1.0
            job.status = "cancelled" if job.cancel.is_set() else "done"
        except Exception as exc:                      # noqa: BLE001 - surfaced to the UI
            log.exception("job %s failed", job.id)
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"
        finally:
            job.finished = time.time()


MANAGER = JobManager()


class BotController:
    """Starts, stops and restarts the live paper trader as a child process.

    Run as a separate process rather than in-thread on purpose: the trader owns
    long-lived websockets and its own asyncio loop, and a hung feed should never
    be able to take the dashboard down with it. Stop is also then a real kill
    rather than a cooperative flag the bot might be too busy to notice.
    """

    #: A bot this server did not start (e.g. launched from a terminal) is
    #: detected from the state file, but cannot be stopped from here -- we do
    #: not own the handle. The UI says so rather than pretending.
    STALE_AFTER_S = 90.0

    #: Start-time arguments this page may set. ``--live`` and ``--armed`` are
    #: deliberately absent: arming real money stays a command-line act, so a
    #: stray click on a web page can never start spending.
    FLAGS: dict[str, tuple[str, str]] = {
        "balance": ("--balance", "num"),
        "assets": ("--assets", "text"),
        "exchanges": ("--exchanges", "text"),
        "min_edge": ("--min-edge", "num"),
        "blend": ("--blend", "num"),
        "take_profit": ("--take-profit", "num"),
        "stop_loss": ("--stop-loss", "num"),
    }

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.last_error: str | None = None
        self.args: dict[str, Any] = {"balance": 100.0, "assets": "",
                                     "exchanges": "binance,bybit,coinbase",
                                     "keep_history": False}
        self._lock = threading.Lock()

    def _external_alive(self) -> bool:
        try:
            return (time.time() - LIVE_STATE.stat().st_mtime) < self.STALE_AFTER_S
        except OSError:
            return False

    def status(self) -> dict[str, Any]:
        with self._lock:
            owned = self.proc is not None and self.proc.poll() is None
            if self.proc is not None and self.proc.poll() is not None and self.started_at:
                # it exited on its own; surface that instead of silently idling
                code = self.proc.returncode
                if code not in (0, None) and not self.last_error:
                    self.last_error = f"bot exited with code {code}"
                self.proc = None
                self.started_at = None
            external = (not owned) and self._external_alive()
            return {
                "running": owned or external,
                "managed": owned,
                "pid": self.proc.pid if owned and self.proc else None,
                "uptime_s": round(time.time() - self.started_at, 1)
                if owned and self.started_at else None,
                "args": dict(self.args),
                "error": self.last_error,
            }

    def _absorb(self, opts: dict[str, Any] | None) -> None:
        """Keep only the flags we know, coerced. Anything else is ignored."""
        for key, (_, kind) in self.FLAGS.items():
            if opts is None or key not in opts:
                continue
            raw = opts[key]
            if raw is None or (isinstance(raw, str) and not raw.strip() and kind == "num"):
                self.args.pop(key, None)
                continue
            if kind == "num":
                try:
                    self.args[key] = float(raw)
                except (TypeError, ValueError):
                    continue
            else:
                self.args[key] = str(raw).strip()
        if opts is not None and "keep_history" in opts:
            self.args["keep_history"] = bool(opts["keep_history"])

    def _command(self) -> list[str]:
        cmd = [sys.executable, "-m", "troll_poly_bot"]
        for key, (flag, _) in self.FLAGS.items():
            value = self.args.get(key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue           # an empty --assets means "every listed asset"
            cmd += [flag, f"{value:g}" if isinstance(value, float) else str(value)]
        if self.args.get("keep_history"):
            cmd.append("--keep-history")
        return cmd

    def start(self, opts: dict[str, Any] | None = None) -> dict:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return {"ok": False, "reason": "already running"}
        if self._external_alive():
            return {"ok": False,
                    "reason": "a bot is already running that this server did not "
                              "start; stop it in its own terminal first"}
        self._absorb(opts)
        cmd = self._command()
        try:
            log_path = Path("data/live_bot.log")
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = log_path.open("a", encoding="utf-8")
            with self._lock:
                self.proc = subprocess.Popen(
                    cmd, stdout=fh, stderr=subprocess.STDOUT,
                    cwd=str(Path.cwd()),
                )
                self.started_at = time.time()
                self.last_error = None
            log.info("started live bot pid=%s %s", self.proc.pid, " ".join(cmd[2:]))
            return {"ok": True, "pid": self.proc.pid}
        except Exception as exc:                        # noqa: BLE001
            self.last_error = f"{type(exc).__name__}: {exc}"
            log.exception("could not start bot")
            return {"ok": False, "reason": self.last_error}

    def stop(self, timeout: float = 8.0) -> dict:
        with self._lock:
            proc = self.proc
        if proc is None or proc.poll() is not None:
            if self._external_alive():
                return {"ok": False,
                        "reason": "the running bot was not started here, so it "
                                  "cannot be stopped from this page"}
            return {"ok": False, "reason": "not running"}
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()                                 # it ignored terminate
            proc.wait(timeout=timeout)
        with self._lock:
            self.proc = None
            self.started_at = None
        # the state file is now stale; drop it so the UI does not show a ghost
        try:
            LIVE_STATE.unlink()
        except OSError:
            pass
        log.info("stopped live bot")
        return {"ok": True}

    def restart(self, opts: dict[str, Any] | None = None) -> dict:
        self.stop()
        time.sleep(0.6)
        return self.start(opts)


BOT = BotController()


class _AccountCache:
    """A real account, read on a timer rather than on every request.

    The Data API is public and free, which is exactly why this must not hammer
    it: the panel refreshes on a 15 s timer and several open tabs would
    otherwise multiply that. Reads are served from here; one thread at a time
    goes out to the network, and a failure keeps serving the last good
    snapshot with its age attached so the page can say the number is stale
    rather than silently showing an old one as current.
    """

    TTL_S = 10.0

    def __init__(self) -> None:
        self.wallet: str | None = os.environ.get("TPB_POLYMARKET_WALLET") or None
        self._snap: dict | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> dict:
        if not self.wallet:
            return {"configured": False,
                    "hint": "set TPB_POLYMARKET_WALLET to your Polymarket proxy "
                            "wallet address, or pass --wallet"}
        with self._lock:
            fresh = self._snap is not None and (time.time() - self._at) < self.TTL_S
            if not fresh:
                try:
                    self._snap = account.snapshot(self.wallet)
                    self._at = time.time()
                except account.AccountError as exc:
                    if self._snap is None:
                        return {"configured": True, "wallet": self.wallet,
                                "error": str(exc)}
                    self._snap = dict(self._snap, error=str(exc))
            snap = dict(self._snap or {})
        snap["configured"] = True
        snap["age_s"] = round(time.time() - self._at, 1)
        return snap


ACCOUNT = _AccountCache()

#: How often the push loop stats the state file. The bot writes it at 10 Hz, so
#: this only decides how much of a 100 ms tick is spent waiting -- not the rate.
WS_WATCH_S = 0.02
#: Idle keepalive. Proxies drop a silent socket long before this matters, and a
#: dead peer is otherwise only noticed when the bot next writes.
WS_PING_S = 20.0


class _Subscription:
    """What one connected page is watching. Shared across its two threads."""

    __slots__ = ("chart", "since", "closed", "lock")

    def __init__(self) -> None:
        self.chart: str | None = None
        self.since: int | None = None
        self.closed = False
        self.lock = threading.Lock()

    def select(self, chart: str | None) -> None:
        with self.lock:
            self.chart = chart
            # a different market shares no points with the old one, so the next
            # push must be a full history rather than a delta
            self.since = None

    def snapshot(self) -> tuple[str | None, int | None]:
        with self.lock:
            return self.chart, self.since

    def advance(self, since: int | None) -> None:
        with self.lock:
            if since is not None:
                self.since = since


class Handler(BaseHTTPRequestHandler):
    server_version = "troll-poly-bot"

    def log_message(self, fmt: str, *args) -> None:       # quieter console
        log.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------ helpers

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _raw(self, body: bytes, ctype: str, cache: str = "no-store") -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _chart_png(self, name: str) -> None:
        """One archived window's rendered chart.

        The slug is matched against the market-slug pattern rather than
        sanitised, so no crafted name can reach outside the chart directory.
        These files never change once written, so they may be cached.
        """
        slug = name[:-4] if name.endswith(".png") else name
        target = ARCHIVE.png(slug)
        if target is None:
            self._json({"error": "not found"}, 404)
            return
        self._raw(target.read_bytes(), "image/png", cache="max-age=86400")

    def _static(self, rel: str) -> None:
        # resolve() + relative_to keeps a crafted path from escaping the dir
        target = (STATIC_DIR / rel).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json({"error": "forbidden"}, 403)
            return
        if not target.is_file():
            self._json({"error": "not found", "path": rel}, 404)
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _save_chart(self, slug: str) -> None:
        """Store a PNG the dashboard rendered for a closed window.

        The slug becomes a filename, so it is matched against a strict pattern
        rather than sanitised — anything that is not exactly a market slug is
        refused outright.
        """
        slug = urllib.parse.unquote(slug).strip()
        if not SLUG_RE.match(slug):
            self._json({"error": "bad slug"}, 400)
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_PNG_BYTES:
            self._json({"error": "bad length"}, 400)
            return
        body = self.rfile.read(length)
        if not body.startswith(b"\x89PNG\r\n\x1a\n"):        # must really be a PNG
            self._json({"error": "not a png"}, 400)
            return
        try:
            CHART_DIR.mkdir(parents=True, exist_ok=True)
            (CHART_DIR / f"{slug}.png").write_bytes(body)
        except OSError as exc:
            self._json({"error": str(exc)}, 500)
            return
        log.info("saved chart %s.png (%.0f KB)", slug, len(body) / 1024)
        self._json({"ok": True, "path": f"data/charts/{slug}.png",
                    "bytes": len(body)})

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    # ---------------------------------------------------------------- GET

    # ----------------------------------------------------------- websocket

    def _ws_send(self, sock_lock: threading.Lock, data: bytes) -> None:
        with sock_lock:
            self.wfile.write(data)
            self.wfile.flush()

    def _ws_reader(self, sub: _Subscription, sock_lock: threading.Lock) -> None:
        """Client -> server: only ever a subscription change, a ping or a close.

        Its own thread because the push loop must not block on a client that
        never speaks, and a blocking read is the only way to notice one that
        hangs up.
        """
        try:
            while not sub.closed:
                opcode, payload = ws.read_frame(self.rfile.read)
                if opcode == ws.OP_CLOSE:
                    break
                if opcode == ws.OP_PING:
                    self._ws_send(sock_lock, ws.encode_frame(payload, ws.OP_PONG))
                    continue
                if opcode != ws.OP_TEXT:
                    continue
                try:
                    msg = json.loads(payload.decode() or "{}")
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if isinstance(msg, dict) and "chart" in msg:
                    chart = msg["chart"]
                    sub.select(chart if isinstance(chart, str) and chart else None)
        except (ws.WSError, OSError, ValueError):
            pass
        finally:
            sub.closed = True

    def _ws_live(self) -> None:
        """Push the live state as it is written, instead of being polled for it.

        Same payload as GET /api/live, same `chart`/`since` trimming -- this
        changes the transport, not the contract, so the page can fall back to
        polling without the server caring.
        """
        key = self.headers.get("Sec-WebSocket-Key")
        if not key or "websocket" not in (self.headers.get("Upgrade") or "").lower():
            self._json({"error": "expected a websocket upgrade"}, 400)
            return
        self.close_connection = True          # this socket leaves HTTP behind
        sock_lock = threading.Lock()
        sub = _Subscription()
        # the page names its market in the upgrade URL so the FIRST push is
        # already the right one, instead of a full state we then throw away
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        sub.select((qs.get("chart") or [None])[0] or None)
        try:
            self.wfile.write(ws.handshake_response(key))
            self.wfile.flush()
        except OSError:
            return

        reader = threading.Thread(target=self._ws_reader, args=(sub, sock_lock),
                                  daemon=True)
        reader.start()

        last_mtime = -1.0
        last_ping = time.time()
        try:
            while not sub.closed:
                now = time.time()
                try:
                    mtime = LIVE_STATE.stat().st_mtime
                except OSError:
                    mtime = -1.0
                if mtime != last_mtime and mtime > 0:
                    chart, since = sub.snapshot()
                    payload = self._live_payload(chart, since)
                    if payload is None:
                        # caught the writer mid-file; leave last_mtime alone so
                        # this same write is retried rather than skipped
                        time.sleep(WS_WATCH_S)
                        continue
                    last_mtime = mtime
                    pts = (payload.get("price_history") or {}).get(chart or "")
                    if pts:
                        sub.advance(pts[-1].get("t"))
                    self._ws_send(sock_lock,
                                  ws.encode_frame(json.dumps(payload).encode()))
                    last_ping = now
                elif now - last_ping >= WS_PING_S:
                    self._ws_send(sock_lock, ws.encode_frame(b"", ws.OP_PING))
                    last_ping = now
                else:
                    time.sleep(WS_WATCH_S)
        except (OSError, ValueError):
            pass
        finally:
            sub.closed = True
            with contextlib.suppress(OSError, ValueError):
                self._ws_send(sock_lock, ws.close_frame())

    @staticmethod
    def _live_payload(chart: str | None, since: int | None) -> dict | None:
        """The live state trimmed for one viewer, or None if it is unreadable.

        None specifically means "could not read it *this time*" -- a torn read
        while the bot rewrites the file, which at 10 Hz happens. Callers must
        not turn that into `running: false`: the page would blank a working
        dashboard for one frame every time it caught the writer mid-stride.
        """
        try:
            payload = json.loads(LIVE_STATE.read_text(encoding="utf-8"))
            mtime = LIVE_STATE.stat().st_mtime
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        payload["stale_s"] = round(time.time() - mtime, 1)
        return _filter_live(payload, chart, since)

    # ---------------------------------------------------------------- GET

    def do_GET(self) -> None:                              # noqa: N802
        path = self.path.split("?")[0]
        if path == "/ws":
            self._ws_live()
        elif path in ("/", "/index.html"):
            self._static("index.html")
        elif path == "/api/schema":
            self._json(build_schema())
        elif path == "/api/bot":
            self._json(BOT.status())
        elif path == "/api/account":
            # read-only: this endpoint cannot place, cancel or modify an order
            self._json(ACCOUNT.get())
        elif path == "/api/history":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            page = _qs_int(qs, "page", 1)
            page_size = _qs_int(qs, "page_size", DEFAULT_HISTORY_PAGE_SIZE)
            self._json(_history(page, page_size,
                                view=(qs.get("view") or ["trips"])[0],
                                mode=(qs.get("mode") or ["all"])[0]))
        elif path == "/api/live":
            # Written by the live paper trader (python -m troll_poly_bot).
            # Served read-only so the dashboard can show it without the two
            # processes needing to know about each other.
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            chart = (qs.get("chart") or [None])[0]
            try:
                since = int((qs.get("since") or [None])[0])
            except (TypeError, ValueError):
                since = None
            # same builder the websocket uses, so the two transports cannot
            # drift apart in what they consider a payload
            self._json(self._live_payload(chart, since) or {"running": False})  # noqa: E501
        elif path == "/api/earnings":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            period = (qs.get("period") or ["day"])[0]
            scope = (qs.get("scope") or ["all"])[0]
            span = _qs_int(qs, "span", 0) or None
            self._json(EARNINGS.get(period, scope, span))
        elif path == "/api/controls":
            self._json(_controls_payload())
        elif path == "/api/saved":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            one = lambda k: (qs.get(k) or [""])[0]            # noqa: E731
            rows = archive.filter_rows(ARCHIVE.all_rows(), asset=one("asset"),
                                       outcome=one("outcome"), traded=one("traded"),
                                       search=one("q"))
            out = archive.page(rows, _qs_int(qs, "page", 1),
                               _qs_int(qs, "page_size", archive.DEFAULT_PAGE_SIZE),
                               one("sort") or "recent")
            out["stats"] = archive.stats(rows)
            out["all_assets"] = archive.stats(ARCHIVE.all_rows())["assets"]
            self._json(out)
        elif path.startswith("/api/saved/"):
            # the whole recorded window, sample path and all, for one market
            src = ARCHIVE.one(urllib.parse.unquote(path[len("/api/saved/"):]))
            if src is None:
                self._json({"error": "unknown window"}, 404)
            else:
                self._raw(src.read_bytes(), "application/json")
        elif path.startswith("/charts/"):
            self._chart_png(urllib.parse.unquote(path[len("/charts/"):]))
        elif path.startswith("/api/job/"):
            job = MANAGER.get(path.rsplit("/", 1)[-1])
            self._json(job.snapshot() if job else {"error": "unknown job"},
                       200 if job else 404)
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        else:
            self._json({"error": "not found"}, 404)

    # --------------------------------------------------------------- POST

    def do_POST(self) -> None:                             # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/api/chart/"):
            self._save_chart(path[len("/api/chart/"):])
        elif path.startswith("/api/bot/"):
            action = path.rsplit("/", 1)[-1]
            body = self._body()
            if action == "start":
                self._json(BOT.start(body))
            elif action == "stop":
                self._json(BOT.stop())
            elif action == "restart":
                self._json(BOT.restart(body))
            else:
                self._json({"error": "unknown action"}, 404)
        elif path == "/api/controls":
            body = self._body()
            scope = str(body.get("scope") or "all")
            values = body.get("values") or {}
            if body.get("reset"):
                result = CONTROLS.clear_scope(scope)
            elif body.get("replace"):
                # the page sends the whole section, so clearing a field removes it
                result = CONTROLS.replace_scope(scope, values)
            else:
                result = CONTROLS.write_scope(scope, values)
            self._json({**result, **_controls_payload()},
                       200 if result.get("ok") else 400)
        elif path == "/api/kill":
            body = self._body()
            active = control.set_kill(bool(body.get("active")))
            self._json({"ok": True, "kill_active": active})
        elif path == "/api/run":
            body = self._body()
            job = MANAGER.submit(
                body.get("params") or {},
                body.get("profiles") or ["home_broadband"],
            )
            self._json({"id": job.id})
        elif path.startswith("/api/cancel/"):
            ok = MANAGER.cancel(path.rsplit("/", 1)[-1])
            self._json({"cancelled": ok})
        else:
            self._json({"error": "not found"}, 404)


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s"
    )
    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/"
    log.info("dashboard on %s  (paper mode only -- no order-signing code exists)", url)
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("shutting down")
    finally:
        httpd.server_close()
