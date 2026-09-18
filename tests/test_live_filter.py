"""The dashboard draws one market; the server must not ship all of them.

At 200 ms sampling and 5 Hz polling, every market's full 5-minute history is
megabytes a second for nothing. `?chart=<slug>` trims the response to the one
being drawn, while `history_meta` keeps the per-market point counts so the
picker and the archive exporter still know what exists.
"""
from __future__ import annotations

from troll_poly_bot.web.server import _filter_live


def _payload():
    return {
        "equity": 100.0,
        "price_history": {
            "btc-updown-5m-1": [{"t": 1}, {"t": 2}, {"t": 3}],
            "eth-updown-5m-1": [{"t": 1}],
        },
    }


def test_meta_counts_every_market():
    out = _filter_live(_payload(), None)
    assert out["history_meta"] == {"btc-updown-5m-1": 3, "eth-updown-5m-1": 1}


def test_no_chart_sends_no_history():
    """No selection means no chart is drawn, so no points are needed.

    This used to return every market. That was the single most expensive
    response the server could produce -- ~9 MB of the ~9.65 MB payload -- and
    the page asks for it on every poll until the user picks a market.
    """
    out = _filter_live(_payload(), None)
    assert out["price_history"] == {}


def test_chart_keeps_only_the_drawn_market():
    out = _filter_live(_payload(), "btc-updown-5m-1")
    assert list(out["price_history"]) == ["btc-updown-5m-1"]
    assert len(out["price_history"]["btc-updown-5m-1"]) == 3


def test_meta_stays_complete_when_history_is_trimmed():
    """The picker must still see markets whose points were not sent."""
    out = _filter_live(_payload(), "btc-updown-5m-1")
    assert out["history_meta"] == {"btc-updown-5m-1": 3, "eth-updown-5m-1": 1}


def test_unknown_chart_yields_empty_history_but_full_meta():
    out = _filter_live(_payload(), "nope")
    assert out["price_history"] == {}
    assert out["history_meta"] == {"btc-updown-5m-1": 3, "eth-updown-5m-1": 1}


def test_missing_history_is_tolerated():
    out = _filter_live({"equity": 1.0}, "btc-updown-5m-1")
    assert out["history_meta"] == {}
    assert out["price_history"] == {}


def test_other_fields_pass_through_untouched():
    out = _filter_live(_payload(), "btc-updown-5m-1")
    assert out["equity"] == 100.0


def test_since_sends_only_newer_points():
    out = _filter_live(_payload(), "btc-updown-5m-1", since=2)
    assert out["price_history"]["btc-updown-5m-1"] == [{"t": 3}]
    assert out["history_partial"] is True


def test_since_at_head_sends_nothing_but_still_marks_partial():
    """An empty delta must not read as 'this market has no history'."""
    out = _filter_live(_payload(), "btc-updown-5m-1", since=3)
    assert out["price_history"]["btc-updown-5m-1"] == []
    assert out["history_partial"] is True


def test_client_behind_the_buffer_gets_a_full_replace():
    """Every point is new, so appending would duplicate; say it is not partial."""
    out = _filter_live(_payload(), "btc-updown-5m-1", since=0)
    assert len(out["price_history"]["btc-updown-5m-1"]) == 3
    assert out["history_partial"] is False


def test_since_is_ignored_without_a_chart():
    out = _filter_live(_payload(), None, since=2)
    assert out["price_history"] == {}
