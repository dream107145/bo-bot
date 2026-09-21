"""The websocket push path, against a real server on a real socket.

The codec tests pin the bytes; this pins the behaviour the page depends on:
a push when the bot writes, deltas after the first message, and a full history
again when the page switches market.

Driven with the `websockets` client already in this project's dependencies, so
the server is talking to a third-party implementation rather than to its own
encoder -- which is the only way a hand-rolled protocol gets honestly tested.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import ThreadingHTTPServer

import pytest
import websockets

from troll_poly_bot.web import server as srv

SLUG = "btc-updown-5m-1"


class _Writer:
    """Stands in for LiveBot.state_loop: rewrites the state on a timer.

    A test that writes the file ONCE and waits for exactly one push is racing
    the watcher, and the real bot never behaves that way -- it rewrites ten
    times a second. Driving the tests the same way removes the race without
    weakening what they assert, and exercises the repeated-write path the
    server actually lives in.
    """

    def __init__(self, state):
        self.state = state
        self.tmp = state.with_suffix(".tmp")
        self.points = [1, 2, 3]
        self.paused = False
        self._stop = threading.Event()
        self.write()                       # exists before the server starts
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def write(self):
        # atomic, exactly as state_loop does it: a reader never sees half a file
        self.tmp.write_text(json.dumps(_state(self.points)), encoding="utf-8")
        self.tmp.replace(self.state)

    def _loop(self):
        while not self._stop.wait(0.02):
            if not self.paused:
                self.write()

    def set(self, points):
        self.points = points

    def stop(self):
        self._stop.set()


def _state(points):
    return {
        "running": True, "equity": 100.0,
        "price_history": {SLUG: [{"t": t, "up": 0.5} for t in points],
                          "eth-updown-5m-1": [{"t": 1, "up": 0.4}, {"t": 2, "up": 0.4}]},
    }


@pytest.fixture()
def live_server(tmp_path, monkeypatch):
    """A real ThreadingHTTPServer on an ephemeral port, with a writable state."""
    state = tmp_path / "live_state.json"
    monkeypatch.setattr(srv, "LIVE_STATE", state)
    monkeypatch.setattr(srv, "WS_WATCH_S", 0.005)
    writer = _Writer(state)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"ws://127.0.0.1:{httpd.server_address[1]}/ws", writer
    finally:
        writer.stop()
        httpd.shutdown()
        httpd.server_close()


async def _recv(conn, timeout=5.0):
    return json.loads(await asyncio.wait_for(conn.recv(), timeout))


async def _recv_until(conn, pred, timeout=5.0):
    """Wait for a push satisfying `pred`.

    The server pushes on every write, and a test cannot control how many
    writes land between its own actions -- asserting on "the next frame"
    encodes an ordering the protocol does not promise. The contract being
    tested is what a push CONTAINS, not which frame number it arrives in.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    last = None
    while asyncio.get_running_loop().time() < deadline:
        last = await _recv(conn, timeout=max(0.05, deadline - asyncio.get_running_loop().time()))
        if pred(last):
            return last
    raise AssertionError(f"no push matched; last was {str(last)[:200]}")


@pytest.mark.asyncio
async def test_a_write_is_pushed_without_being_asked(live_server):
    url, writer = live_server
    async with websockets.connect(f"{url}?chart={SLUG}") as conn:
        first = await _recv(conn)
        assert first["equity"] == 100.0
        assert [p["t"] for p in first["price_history"][SLUG]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_the_second_push_carries_only_new_points(live_server):
    url, writer = live_server
    async with websockets.connect(f"{url}?chart={SLUG}") as conn:
        await _recv(conn)                                  # full history
        writer.set([1, 2, 3, 4, 5])
        delta = await _recv_until(conn, lambda m: m["price_history"].get(SLUG))
        assert [p["t"] for p in delta["price_history"][SLUG]] == [4, 5], \
            "a delta must carry only the points the client lacks"
        assert delta["history_partial"] is True, "and say it is one, so it appends"


@pytest.mark.asyncio
async def test_switching_market_resends_a_full_history(live_server):
    url, writer = live_server
    async with websockets.connect(f"{url}?chart={SLUG}") as conn:
        await _recv(conn)
        await conn.send(json.dumps({"chart": "eth-updown-5m-1"}))
        await asyncio.sleep(0.1)                           # let the reader apply it
        writer.set([1, 2, 3, 4])
        msg = await _recv_until(conn, lambda m: "eth-updown-5m-1" in m["price_history"])
        assert "eth-updown-5m-1" in msg["price_history"]
        assert len(msg["price_history"]["eth-updown-5m-1"]) == 2
        assert not msg.get("history_partial"), "a new market is not a delta"


@pytest.mark.asyncio
async def test_no_selection_means_no_history_is_pushed(live_server):
    url, writer = live_server
    async with websockets.connect(url) as conn:
        first = await _recv(conn)              # the push every connect opens with
        assert first["price_history"] == {}, "nothing selected, so no points"
        writer.set([1, 2, 3, 4])
        msg = await _recv_until(conn, lambda m: m["history_meta"].get(SLUG) == 4)
        assert msg["price_history"] == {}
        # the picker still learns what exists, it just is not sent the points
        assert msg["history_meta"] == {SLUG: 4, "eth-updown-5m-1": 2}


@pytest.mark.asyncio
async def test_the_socket_survives_an_unchanged_state(live_server):
    """No write, no push -- and the connection must stay open through it."""
    url, writer = live_server
    async with websockets.connect(f"{url}?chart={SLUG}") as conn:
        await _recv(conn)
        writer.paused = True
        await asyncio.sleep(0.2)               # drain anything already in flight
        while True:
            try:
                await _recv(conn, timeout=0.3)
            except asyncio.TimeoutError:
                break                          # quiet, as it should be
        with pytest.raises(asyncio.TimeoutError):
            await _recv(conn, timeout=0.4)
        writer.paused = False
        writer.set([1, 2, 3, 9])
        msg = await _recv_until(conn, lambda m: m["price_history"].get(SLUG))
        assert [p["t"] for p in msg["price_history"][SLUG]] == [9]


@pytest.mark.asyncio
async def test_garbage_from_the_client_does_not_kill_the_stream(live_server):
    url, writer = live_server
    async with websockets.connect(f"{url}?chart={SLUG}") as conn:
        await _recv(conn)
        await conn.send("not json at all")
        await conn.send(json.dumps(["unexpected", "shape"]))
        writer.set([1, 2, 3, 7])
        msg = await _recv_until(conn, lambda m: m["price_history"].get(SLUG))
        assert [p["t"] for p in msg["price_history"][SLUG]] == [7]


@pytest.mark.asyncio
async def test_a_plain_get_on_ws_is_a_400_not_a_crash(live_server):
    """Hitting /ws in a browser address bar must not take the server down."""
    url, _writer = live_server
    http = url.replace("ws://", "http://")
    reader = await asyncio.get_running_loop().run_in_executor(
        None, lambda: __import__("urllib.request", fromlist=["request"]))
    import urllib.error
    import urllib.request
    with pytest.raises(urllib.error.HTTPError) as exc:
        await asyncio.get_running_loop().run_in_executor(
            None, lambda: urllib.request.urlopen(http, timeout=5))
    assert exc.value.code == 400
