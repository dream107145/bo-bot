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


def test_no_chart_keeps_all_history():
    out = _filter_live(_payload(), None)
    assert set(out["price_history"]) == {"btc-updown-5m-1", "eth-updown-5m-1"}


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
