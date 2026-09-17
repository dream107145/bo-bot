"""`_history()` paginates the trade log instead of hard-capping it.

The trade log is append-only and grows for as long as the bot runs, so the
dashboard's "History across all runs" table cannot just show a fixed recent
slice forever -- the operator needs to be able to page back through weeks of
fills and settlements. This covers the paging arithmetic directly, without a
live server, the same way test_live_filter.py covers `_filter_live`.
"""
from __future__ import annotations

import json

import pytest

from troll_poly_bot.web import server


def _write_log(path, n):
    with path.open("w", encoding="utf-8") as fh:
        for i in range(n):
            fh.write(json.dumps({"event": "fill", "ts": i, "slug": f"m{i}",
                                  "side": "UP", "price": 0.5, "size": 1.0, "cost": 0.5}) + "\n")
    return path


@pytest.fixture
def trade_log(tmp_path, monkeypatch):
    path = tmp_path / "trades.jsonl"
    monkeypatch.setattr(server, "TRADE_LOG", path)
    return path


def test_missing_log_is_one_empty_page(trade_log):
    h = server._history()
    assert h["total_rows"] == 0
    assert h["total_pages"] == 1
    assert h["page"] == 1
    assert h["rows"] == []


def test_first_page_is_newest_first(trade_log):
    _write_log(trade_log, 63)
    h = server._history(page=1, page_size=25)
    assert h["total_rows"] == 63
    assert h["total_pages"] == 3
    assert h["page"] == 1
    assert len(h["rows"]) == 25
    assert h["rows"][0]["ts"] == 62          # most recently written row first
    assert h["rows"][-1]["ts"] == 38


def test_pages_tile_the_log_without_gaps_or_overlap(trade_log):
    _write_log(trade_log, 63)
    seen = []
    for page in (1, 2, 3):
        h = server._history(page=page, page_size=25)
        seen += [r["ts"] for r in h["rows"]]
    assert seen == list(range(62, -1, -1))   # every row, newest first, exactly once


def test_last_page_can_be_partial(trade_log):
    _write_log(trade_log, 63)
    h = server._history(page=3, page_size=25)
    assert len(h["rows"]) == 13              # 63 - 2*25


def test_out_of_range_page_clamps_to_the_last_real_page(trade_log):
    _write_log(trade_log, 63)
    last = server._history(page=3, page_size=25)
    clamped = server._history(page=99, page_size=25)
    assert clamped["page"] == 3
    assert clamped["rows"] == last["rows"]


def test_page_below_one_clamps_to_one(trade_log):
    _write_log(trade_log, 10)
    h = server._history(page=0, page_size=25)
    assert h["page"] == 1


def test_page_size_is_bounded(trade_log):
    _write_log(trade_log, 5)
    h = server._history(page=1, page_size=0)
    assert h["page_size"] == 1
    h2 = server._history(page=1, page_size=10_000)
    assert h2["page_size"] == server.MAX_HISTORY_PAGE_SIZE


def test_kpis_and_curve_cover_the_whole_log_not_just_the_page(trade_log):
    with trade_log.open("w", encoding="utf-8") as fh:
        for i in range(40):
            fh.write(json.dumps({"event": "settle", "ts": i, "slug": f"m{i}", "pnl": 1.0,
                                  "balance": 100.0 + i}) + "\n")
    h = server._history(page=1, page_size=10)
    assert h["settled"] == 40
    assert len(h["curve"]) == 40             # unbounded, unlike `rows`
    assert h["realised_pnl"] == pytest.approx(40.0)


def test_qs_int_falls_back_on_garbage_or_missing():
    assert server._qs_int({}, "page", 1) == 1
    assert server._qs_int({"page": ["3"]}, "page", 1) == 3
    assert server._qs_int({"page": ["not-a-number"]}, "page", 1) == 1
