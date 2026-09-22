"""Folding the event stream into the round trips a person recognises."""
from __future__ import annotations

import json

import pytest

from troll_poly_bot.ledger import is_sell, load, round_trips, summary

SLUG = "btc-updown-5m-1790000000"


def buy(ts, size, price, side="UP", slug=SLUG, fee=0.05, expected=None, action="BUY"):
    row = {"ts": ts, "event": "fill", "action": action, "slug": slug, "asset": "BTC",
           "side": side, "size": size, "price": price, "fee": fee,
           "cost": round(size * price, 4), "epoch": 1790000000, "mode": "live-paper"}
    if expected is not None:
        row["expected"] = expected
    return row


def sell(ts, size, price, side="UP", slug=SLUG, fee=0.05):
    return {"ts": ts, "event": "fill", "action": "SELL", "slug": slug, "asset": "BTC",
            "side": side, "size": size, "price": price, "fee": fee,
            "cost": -round(size * price, 4), "epoch": 1790000000, "mode": "live-paper"}


def settle(ts, pnl, slug=SLUG, exited=False, balance=100.0):
    return {"ts": ts, "event": "settle", "slug": slug, "asset": "BTC", "epoch": 1790000000,
            "pnl": pnl, "exited": exited, "balance": balance, "mode": "live-paper"}


def test_two_entries_a_sell_and_a_settle_are_one_trade_not_four():
    """The complaint this fixes: one position looked like several trades."""
    rows = [buy(1000, 2.0, 0.60), buy(2000, 4.0, 0.63),
            sell(9000, 6.0, 0.86), settle(9000, 1.36, exited=True)]
    trips = round_trips(rows)
    assert len(trips) == 1
    t = trips[0]
    assert t["buys"] == 2 and t["sells"] == 1
    assert t["shares"] == 6.0 and t["sold_shares"] == 6.0
    assert t["bought_ts"] == 1000 and t["last_buy_ts"] == 2000
    assert t["sold_ts"] == 9000 and t["closed_ts"] == 9000
    assert t["hold_s"] == pytest.approx(8.0)
    assert t["entry_price"] == pytest.approx((2 * 0.60 + 4 * 0.63) / 6)
    assert t["exit_price"] == pytest.approx(0.86)
    assert t["pnl"] == 1.36 and t["status"] == "sold" and t["exited"] is True
    assert t["fees"] == pytest.approx(0.15)
    assert t["side"] == "UP" and t["asset"] == "BTC" and t["mode"] == "live-paper"


def test_a_position_held_to_resolution_has_a_settle_time_and_no_sell_time():
    rows = [buy(1000, 5.0, 0.40), settle(301000, -2.0)]
    t = round_trips(rows)[0]
    assert t["sold_ts"] is None and t["exit_price"] is None
    assert t["settled_ts"] == 301000 and t["closed_ts"] == 301000
    assert t["hold_s"] == pytest.approx(300.0)
    assert t["status"] == "settled" and t["exited"] is False
    assert t["pnl"] == -2.0


def test_a_position_still_open_reports_no_close():
    t = round_trips([buy(1000, 5.0, 0.40)])[0]
    assert t["status"] == "open"
    assert t["closed_ts"] is None and t["hold_s"] is None and t["pnl"] is None
    assert t["shares"] == 5.0


def test_going_flat_and_buying_again_is_a_second_trade():
    rows = [buy(1000, 5.0, 0.40), sell(2000, 5.0, 0.50),
            buy(3000, 4.0, 0.45), sell(4000, 4.0, 0.60), settle(4000, 1.1, exited=True)]
    trips = round_trips(rows)
    assert len(trips) == 2
    first, second = trips
    assert first["bought_ts"] == 1000 and first["sold_ts"] == 2000 and first["buys"] == 1
    assert second["bought_ts"] == 3000 and second["sold_ts"] == 4000
    # the settlement reports the window's realised PnL once, on the last trip
    assert first["pnl"] is None and second["pnl"] == 1.1


def test_both_sides_of_one_window_are_separate_trades():
    rows = [buy(1000, 5.0, 0.40, side="UP"), buy(1100, 3.0, 0.55, side="DOWN"),
            settle(9000, 0.5)]
    trips = round_trips(rows)
    assert {t["side"] for t in trips} == {"UP", "DOWN"}
    assert all(t["settled_ts"] == 9000 for t in trips)
    assert [t["pnl"] for t in trips] == [None, 0.5]        # counted once


def test_older_rows_without_an_action_still_count_as_buys():
    assert is_sell({"action": "SELL"}) is True
    assert is_sell({"action": "BUY"}) is False
    assert is_sell({}) is False                            # the old writer's shape
    rows = [buy(1000, 5.0, 0.40, action=None), sell(2000, 5.0, 0.50), settle(2000, 0.4, exited=True)]
    rows[0].pop("action")
    t = round_trips(rows)[0]
    assert t["buys"] == 1 and t["sells"] == 1 and t["shares"] == 5.0


def test_events_out_of_order_or_missing_a_timestamp_do_not_break_the_fold():
    rows = [settle(9000, 1.0), buy(2000, 4.0, 0.63), buy(1000, 2.0, 0.60), sell(8000, 6.0, 0.86)]
    rows.append({"event": "fill", "slug": SLUG, "size": 1})     # no ts at all
    t = round_trips(rows)[0]
    assert t["bought_ts"] == 1000 and t["sold_ts"] == 8000 and t["buys"] == 2
    assert round_trips([]) == []


def test_slippage_is_the_average_paid_against_the_average_expected():
    rows = [buy(1000, 5.0, 0.42, expected=0.40), buy(1100, 5.0, 0.44, expected=0.40)]
    t = round_trips(rows)[0]
    assert t["entry_price"] == pytest.approx(0.43)
    assert t["slippage"] == pytest.approx(0.03)
    assert round_trips([buy(1000, 5.0, 0.42)])[0]["slippage"] is None


def test_summary_describes_the_set():
    rows = [
        buy(1000, 5.0, 0.40), sell(2000, 5.0, 0.50), settle(2000, 0.4, exited=True),
        buy(3000, 5.0, 0.40, slug="eth-updown-5m-2"), settle(303000, -1.0, slug="eth-updown-5m-2"),
        buy(4000, 5.0, 0.40, slug="sol-updown-5m-3"),          # still open
    ]
    s = summary(round_trips(rows))
    assert s["trips"] == 3 and s["open"] == 1 and s["closed"] == 2
    assert s["wins"] == 1 and s["losses"] == 1 and s["win_rate"] == 0.5
    assert s["pnl"] == pytest.approx(-0.6)
    assert s["sold_early"] == 1 and s["held_to_resolution"] == 1
    assert s["shares"] == 15.0
    assert s["median_hold_s"] is not None
    assert s["modes"] == ["live-paper"]
    empty = summary([])
    assert empty["trips"] == 0 and empty["win_rate"] is None and empty["median_hold_s"] is None


def test_load_skips_junk_and_a_missing_file(tmp_path):
    p = tmp_path / "trades.jsonl"
    p.write_text("\n".join([
        json.dumps(buy(1000, 5.0, 0.4)),
        "{not json",
        "",
        json.dumps({"no": "event key"}),
        json.dumps(settle(2000, 1.0)),
    ]), encoding="utf-8")
    rows = load(p)
    assert [r["event"] for r in rows] == ["fill", "settle"]
    assert load(tmp_path / "gone.jsonl") == []
