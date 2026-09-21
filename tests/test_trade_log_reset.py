"""A restart starts a fresh book.

`--balance` is re-applied on every start, so the paper balance resets. Leaving
the previous run's fills in `data/live_trades.jsonl` made the dashboard's
history panel report PnL that the current run's equity never accounted for --
two different runs added together and presented as one.
"""
from __future__ import annotations

from types import SimpleNamespace

from troll_poly_bot.live import LiveBot


def _open_market(close_ts=1e18):
    """The shape _reap looks at: m.meta.market.close_ts and m.books."""
    return SimpleNamespace(
        meta=SimpleNamespace(market=SimpleNamespace(close_ts=close_ts, strike=1.0)),
        books={})


def _bot(tmp_path, **kw):
    return LiveBot(
        assets=("BTC",),
        state_path=str(tmp_path / "state.json"),
        trade_log=str(tmp_path / "trades.jsonl"),
        **kw,
    )


def test_restart_clears_the_trade_log(tmp_path):
    log = tmp_path / "trades.jsonl"
    log.write_text('{"event":"fill","slug":"btc-updown-5m-1"}\n', encoding="utf-8")
    _bot(tmp_path)
    assert log.read_text(encoding="utf-8") == ""


def test_keep_history_preserves_the_trade_log(tmp_path):
    log = tmp_path / "trades.jsonl"
    log.write_text('{"event":"fill","slug":"btc-updown-5m-1"}\n', encoding="utf-8")
    _bot(tmp_path, reset_history=False)
    assert "btc-updown-5m-1" in log.read_text(encoding="utf-8")


def test_missing_trade_log_is_not_an_error(tmp_path):
    bot = _bot(tmp_path)
    assert bot.trade_log.exists()
    assert bot.trade_log.read_text(encoding="utf-8") == ""


def test_ledger_starts_empty(tmp_path):
    assert _bot(tmp_path).ledger == []


def test_ledger_append_writes_both_sides(tmp_path):
    """The tail the dashboard reads and the file the history panel reads."""
    bot = _bot(tmp_path)
    bot._ledger_append({"event": "fill", "slug": "btc-updown-5m-1"})
    assert bot.ledger == [{"event": "fill", "slug": "btc-updown-5m-1"}]
    assert "btc-updown-5m-1" in bot.trade_log.read_text(encoding="utf-8")


def test_ledger_tail_is_bounded(tmp_path):
    bot = _bot(tmp_path)
    for i in range(250):
        bot._ledger_append({"event": "fill", "slug": f"s{i}"})
    assert len(bot.ledger) == 200
    assert bot.ledger[-1]["slug"] == "s249"


# ───────────────────── the state file must not grow forever ──────────────────


def test_reap_drops_history_for_markets_it_retires(tmp_path):
    """price_history is keyed by slug and nothing else pruned it.

    Each market's deque is capped, but the NUMBER of markets was not. In one
    5-hour run 244 markets and 154k points accumulated into a 41 MB state
    file; writing that takes ~1s, which capped the dashboard at 1 Hz however
    often state_loop was told to run.
    """
    from collections import deque
    bot = _bot(tmp_path)
    bot.price_history["closed-updown-5m-1"] = deque([{"t": 1}])
    bot.price_history["open-updown-5m-2"] = deque([{"t": 2}])
    bot.markets = {"open-updown-5m-2": _open_market()}   # only this one is live

    bot._reap(now_ms=0.0)

    assert set(bot.price_history) == {"open-updown-5m-2"}


def test_reap_keeps_history_for_live_markets(tmp_path):
    from collections import deque
    bot = _bot(tmp_path)
    bot.price_history["a"] = deque([{"t": 1}])
    bot.markets = {"a": _open_market()}
    bot._reap(now_ms=0.0)
    assert "a" in bot.price_history


def test_state_write_survives_an_int_keyed_dict(tmp_path):
    """epoch_pnl is keyed by int epoch; orjson refuses that without the option."""
    bot = _bot(tmp_path)
    tmp = bot.state_path.with_suffix(".tmp")
    bot._write_state(tmp, {"epoch_pnl": {1789: 1.5}, "equity": 100.0})
    import json as _json
    back = _json.loads(bot.state_path.read_text(encoding="utf-8"))
    assert back["epoch_pnl"] == {"1789": 1.5}
    assert back["equity"] == 100.0


def test_state_write_is_atomic(tmp_path):
    """The reader must never see a partial file; the tmp is swapped in."""
    bot = _bot(tmp_path)
    tmp = bot.state_path.with_suffix(".tmp")
    bot._write_state(tmp, {"equity": 1.0})
    bot._write_state(tmp, {"equity": 2.0})
    import json as _json
    assert _json.loads(bot.state_path.read_text(encoding="utf-8"))["equity"] == 2.0
    assert not tmp.exists(), "the temp file is renamed, not left behind"
