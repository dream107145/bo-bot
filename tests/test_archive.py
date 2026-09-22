"""The saved-market archive: summarising, caching, filtering, paging."""
from __future__ import annotations

import json

import pytest

from troll_poly_bot.archive import (
    ArchiveIndex, DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, filter_rows, page, stats, summarise,
)


def window(slug="btc-updown-5m-1790000000", asset="BTC", outcome="UP", pnl=0.0,
           fills=(), strike=100.0, last_spot=101.0, points=5):
    path = []
    for i in range(points):
        frac = i / max(points - 1, 1)
        path.append({"t": 1790000000000 + i * 1000, "left": 300 - frac * 300,
                     "up": 0.4 + frac * 0.2, "down": 0.6 - frac * 0.2,
                     "spot": strike + (last_spot - strike) * frac})
    return {
        "slug": slug, "asset": asset, "question": f"{asset} Up or Down - test",
        "strike": strike, "open_ts": 1790000000000.0, "close_ts": 1790000300000.0,
        "outcome": outcome, "outcome_source": "venue", "pnl": pnl,
        "fee": {"rate": 0.07, "exponent": 1.0}, "exchanges": ["binance"],
        "fills": list(fills), "points": path,
    }


def write(tmp_path, doc, png=False):
    d = tmp_path / "charts"
    d.mkdir(exist_ok=True)
    (d / f"{doc['slug']}.json").write_text(json.dumps(doc), encoding="utf-8")
    if png:
        (d / f"{doc['slug']}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return d


def test_summary_keeps_the_facts_and_drops_the_sample_path():
    doc = window(pnl=1.25, fills=[{"size": 6, "price": 0.42, "fee": 0.1, "tag": "UP"},
                                  {"size": 4, "price": 0.44, "fee": 0.1, "tag": "UP"}])
    r = summarise(doc, doc["slug"], has_png=True)
    assert r["asset"] == "BTC" and r["outcome"] == "UP" and r["has_png"] is True
    assert r["fills"] == 2 and r["shares"] == 10.0 and r["traded"] is True
    assert r["pnl"] == 1.25 and r["fee_rate"] == 0.07
    assert r["up_open"] == 0.4 and r["up_close"] == pytest.approx(0.6)
    assert r["move_bps"] == pytest.approx(100.0)          # 100 -> 101 is +100 bps
    assert r["points"] == 5
    assert "points" not in [k for k in r if isinstance(r[k], list)]
    # an untraded window is marked as such rather than as a zero-PnL trade
    assert summarise(window(), "x")["traded"] is False


def test_summary_survives_a_window_with_nothing_in_it():
    r = summarise({}, "empty-updown-5m-1")
    assert r["asset"] == "" and r["outcome"] is None and r["move_bps"] is None
    assert r["fills"] == 0 and r["points"] == 0 and r["traded"] is False


def test_index_builds_caches_and_follows_the_directory(tmp_path):
    d = write(tmp_path, window(slug="btc-updown-5m-1", pnl=2.0,
                               fills=[{"size": 5, "price": 0.4}]), png=True)
    write(tmp_path, window(slug="eth-updown-5m-2", asset="ETH", outcome="DOWN"))
    ix = ArchiveIndex(d, tmp_path / "index.json")

    first = ix.refresh()
    assert first == {"total": 2, "added": 2, "updated": 0, "removed": 0}
    assert (tmp_path / "index.json").is_file()

    # nothing changed on disk -> nothing is re-parsed
    assert ix.refresh() == {"total": 2, "added": 0, "updated": 0, "removed": 0}

    # a fresh index reads the cache rather than the windows
    again = ArchiveIndex(d, tmp_path / "index.json")
    assert again.refresh()["added"] == 0
    assert len(again.all_rows()) == 2

    (d / "eth-updown-5m-2.json").unlink()
    assert ix.refresh()["removed"] == 1
    assert [r["slug"] for r in ix.all_rows()] == ["btc-updown-5m-1"]


def test_index_reparses_a_window_that_changed(tmp_path):
    import os
    import time

    doc = window(slug="btc-updown-5m-1", pnl=1.0)
    d = write(tmp_path, doc)
    ix = ArchiveIndex(d, tmp_path / "index.json")
    ix.refresh()
    assert ix.all_rows()[0]["pnl"] == 1.0

    doc["pnl"] = 9.0
    doc["points"].append({"t": 1, "left": 0, "up": 0.9, "down": 0.1, "spot": 105.0})
    (d / "btc-updown-5m-1.json").write_text(json.dumps(doc), encoding="utf-8")
    os.utime(d / "btc-updown-5m-1.json", (time.time() + 5, time.time() + 5))
    assert ix.refresh()["updated"] == 1
    assert ix.all_rows()[0]["pnl"] == 9.0


def test_junk_in_the_directory_is_skipped_not_fatal(tmp_path):
    d = write(tmp_path, window(slug="btc-updown-5m-1"))
    (d / "btc-updown-5m-9.json").write_text("{not json", encoding="utf-8")
    (d / "notes.json").write_text('{"slug": "x"}', encoding="utf-8")   # not a slug
    (d / "btc-updown-5m-8.json").write_text('"a string"', encoding="utf-8")
    ix = ArchiveIndex(d, tmp_path / "index.json")
    ix.refresh()
    assert [r["slug"] for r in ix.all_rows()] == ["btc-updown-5m-1"]


def test_a_corrupt_cache_is_rebuilt_rather_than_trusted(tmp_path):
    d = write(tmp_path, window(slug="btc-updown-5m-1"))
    cache = tmp_path / "index.json"
    cache.write_text("{not json", encoding="utf-8")
    assert ArchiveIndex(d, cache).refresh()["total"] == 1
    cache.write_text(json.dumps({"version": 999, "rows": {"ghost": {}}}), encoding="utf-8")
    ix = ArchiveIndex(d, cache)
    ix.refresh()
    assert [r["slug"] for r in ix.all_rows()] == ["btc-updown-5m-1"]


def test_a_crafted_slug_cannot_reach_outside_the_archive(tmp_path):
    d = write(tmp_path, window(slug="btc-updown-5m-1"), png=True)
    (tmp_path / "secret.json").write_text("{}", encoding="utf-8")
    ix = ArchiveIndex(d, tmp_path / "index.json")
    assert ix.one("btc-updown-5m-1") is not None
    assert ix.png("btc-updown-5m-1") is not None
    for bad in ("../secret", "..\\secret", "/etc/passwd", "btc-updown-5m-1/../../x",
                "", "not a slug", "btc-updown-5m-1.json"):
        assert ix.one(bad) is None, bad
        assert ix.png(bad) is None, bad
    assert ix.one("btc-updown-5m-404") is None          # well formed but absent


def test_filters_narrow_the_set():
    rows = [
        summarise(window(slug="a", asset="BTC", outcome="UP", pnl=1.0,
                         fills=[{"size": 5, "price": 0.4}]), "a"),
        summarise(window(slug="b", asset="ETH", outcome="DOWN"), "b"),
        summarise(window(slug="c", asset="BTC", outcome="DOWN"), "c"),
    ]
    assert [r["slug"] for r in filter_rows(rows, asset="btc")] == ["a", "c"]
    assert [r["slug"] for r in filter_rows(rows, outcome="down")] == ["b", "c"]
    assert [r["slug"] for r in filter_rows(rows, traded="yes")] == ["a"]
    assert [r["slug"] for r in filter_rows(rows, traded="no")] == ["b", "c"]
    assert [r["slug"] for r in filter_rows(rows, search="ETH Up")] == ["b"]
    assert len(filter_rows(rows)) == 3


def test_stats_counts_only_what_we_actually_traded():
    rows = [
        summarise(window(slug="a", outcome="UP", pnl=2.0, fills=[{"size": 5, "price": 0.4}]), "a"),
        summarise(window(slug="b", outcome="DOWN", pnl=-1.0, fills=[{"size": 5, "price": 0.4}]), "b"),
        summarise(window(slug="c", asset="ETH", outcome="UP"), "c"),      # watched only
    ]
    st = stats(rows)
    assert st["windows"] == 3 and st["traded"] == 2
    assert st["pnl"] == 1.0 and st["wins"] == 1 and st["losses"] == 1
    assert st["win_rate"] == 0.5
    assert st["up_share"] == pytest.approx(2 / 3, abs=1e-4)
    assert st["assets"] == ["BTC", "ETH"]
    empty = stats([])
    assert empty["win_rate"] is None and empty["up_share"] is None and empty["assets"] == []


def test_paging_sorts_over_the_whole_set_and_clamps():
    rows = []
    for i in range(10):
        r = summarise(window(slug=f"s{i}", pnl=float(i),
                             fills=[{"size": 1, "price": 0.5}]), f"s{i}")
        r["close_ts"] = 1790000000000.0 + i * 1000
        r["move_bps"] = (i - 5) * 10.0
        rows.append(r)

    newest = page(rows, 1, 3)
    assert [r["slug"] for r in newest["rows"]] == ["s9", "s8", "s7"]
    assert newest["total_pages"] == 4 and newest["total_rows"] == 10

    assert [r["slug"] for r in page(rows, 1, 3, sort="oldest")["rows"]] == ["s0", "s1", "s2"]
    assert [r["slug"] for r in page(rows, 1, 2, sort="best")["rows"]] == ["s9", "s8"]
    assert [r["slug"] for r in page(rows, 1, 2, sort="worst")["rows"]] == ["s0", "s1"]
    # biggest absolute move, either direction, largest first
    moved = page(rows, 1, 10, sort="move")["rows"]
    assert moved[0]["slug"] == "s0"                      # -50 bps is the biggest swing
    swings = [abs(r["move_bps"]) for r in moved]
    assert swings == sorted(swings, reverse=True)
    # an unknown sort falls back rather than raising
    assert page(rows, 1, 3, sort="sideways")["rows"][0]["slug"] == "s9"

    assert page(rows, 99, 3)["page"] == 4               # clamped to the last page
    assert page(rows, -5, 3)["page"] == 1
    assert page(rows, 1, 9999)["page_size"] == MAX_PAGE_SIZE
    assert page(rows, 1, 0)["page_size"] == 1
    assert page([], 1)["total_pages"] == 1 and page([], 1)["rows"] == []
    assert page(rows, 1)["page_size"] == DEFAULT_PAGE_SIZE
