"""A restart starts a fresh book.

`--balance` is re-applied on every start, so the paper balance resets. Leaving
the previous run's fills in `data/live_trades.jsonl` made the dashboard's
history panel report PnL that the current run's equity never accounted for --
two different runs added together and presented as one.
"""
from __future__ import annotations

from troll_poly_bot.live import LiveBot


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
