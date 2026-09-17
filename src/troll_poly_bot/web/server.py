"""Local dashboard server.

Stdlib only, bound to loopback. This drives the *real* Python model -- the same
pricer, paper engine and latency model the tests cover -- rather than a second
implementation in JavaScript that would quietly drift out of agreement with it.

    python -m troll_poly_bot.web

Runs are executed on a worker thread so the UI stays responsive, and can be
cancelled: the sim takes a progress callback that aborts when it returns False.
"""
from __future__ import annotations

import json
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

from ..execution.latency import PROFILES
from ..sim import SimResult, run_sim
from .schema import apply_params, build_schema

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
LIVE_STATE = Path("data/live_state.json")
TRADE_LOG = Path("data/live_trades.jsonl")


def _filter_live(payload: dict, chart: str | None) -> dict:
    """Trim the state to what one page needs.

    The bot writes every live market's full 5-minute history five times a
    second; the page only ever draws ONE of them. Sending all of them at that
    rate is most of the bytes for nothing. ``history_meta`` keeps the point
    count per market so the picker and the archive exporter still know what
    exists without receiving it.
    """
    hist = payload.get("price_history") or {}
    payload["history_meta"] = {slug: len(pts) for slug, pts in hist.items()}
    if chart is not None:
        payload["price_history"] = {chart: hist[chart]} if chart in hist else {}
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


def _history(page: int = 1, page_size: int = DEFAULT_HISTORY_PAGE_SIZE) -> dict:
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
    rows = []
    try:
        for line in TRADE_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    settles = [r for r in rows if r.get("event") == "settle"]
    fills = [r for r in rows if r.get("event") == "fill"]
    realised, curve = 0.0, []
    for r in settles:
        pnl = float(r.get("pnl") or 0.0)
        realised += pnl
        curve.append({"ts": r.get("ts"), "slug": r.get("slug"),
                      "pnl": round(pnl, 4), "cum": round(realised, 4)})

    page_size = min(max(page_size, 1), MAX_HISTORY_PAGE_SIZE)
    total_rows = len(rows)
    total_pages = max(1, -(-total_rows // page_size))          # ceil div
    page = min(max(page, 1), total_pages)
    newest_first = rows[::-1]
    start = (page - 1) * page_size
    return {
        "fills": len(fills),
        "settled": len(settles),
        "wins": sum(1 for r in settles if float(r.get("pnl") or 0.0) > 0),
        "losses": sum(1 for r in settles if float(r.get("pnl") or 0.0) <= 0),
        "realised_pnl": round(realised, 4),
        "curve": curve,
        "rows": newest_first[start:start + page_size],
        "page": page,
        "page_size": page_size,
        "total_rows": total_rows,
        "total_pages": total_pages,
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

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.last_error: str | None = None
        self.args: dict[str, Any] = {"balance": 100.0, "assets": "BTC,ETH,SOL,XRP"}
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

    def start(self, balance: float | None = None, assets: str | None = None) -> dict:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return {"ok": False, "reason": "already running"}
        if self._external_alive():
            return {"ok": False,
                    "reason": "a bot is already running that this server did not "
                              "start; stop it in its own terminal first"}
        if balance is not None:
            self.args["balance"] = float(balance)
        if assets:
            self.args["assets"] = assets
        cmd = [sys.executable, "-m", "troll_poly_bot",
               "--balance", str(self.args["balance"]),
               "--assets", str(self.args["assets"])]
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

    def restart(self, balance: float | None = None, assets: str | None = None) -> dict:
        self.stop()
        time.sleep(0.6)
        return self.start(balance, assets)


BOT = BotController()


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

    def do_GET(self) -> None:                              # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path == "/api/schema":
            self._json(build_schema())
        elif path == "/api/bot":
            self._json(BOT.status())
        elif path == "/api/history":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            page = _qs_int(qs, "page", 1)
            page_size = _qs_int(qs, "page_size", DEFAULT_HISTORY_PAGE_SIZE)
            self._json(_history(page, page_size))
        elif path == "/api/live":
            # Written by the live paper trader (python -m troll_poly_bot).
            # Served read-only so the dashboard can show it without the two
            # processes needing to know about each other.
            try:
                raw = LIVE_STATE.read_text(encoding="utf-8")
                payload = json.loads(raw)
                payload["stale_s"] = round(time.time() - LIVE_STATE.stat().st_mtime, 1)
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                chart = (qs.get("chart") or [None])[0]
                self._json(_filter_live(payload, chart))
            except (OSError, json.JSONDecodeError):
                self._json({"running": False}, 200)
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
                self._json(BOT.start(body.get("balance"), body.get("assets")))
            elif action == "stop":
                self._json(BOT.stop())
            elif action == "restart":
                self._json(BOT.restart(body.get("balance"), body.get("assets")))
            else:
                self._json({"error": "unknown action"}, 404)
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
