"""The dashboard's two new powers: steering a running bot, and counting money."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from troll_poly_bot import earnings
from troll_poly_bot.control import (
    START_ARGS, TUNABLES, ControlFile, apply_to, current_values, kill_active, set_kill, spec,
    validate,
)
from troll_poly_bot.config import BotConfig
from troll_poly_bot.execution.polymarket import LiveCaps


def ms(dt: datetime) -> float:
    return dt.timestamp() * 1000.0


# ─────────────────────────────── control file ───────────────────────────────


def test_every_tunable_points_at_a_real_attribute():
    cfg = BotConfig()
    caps = LiveCaps()
    holders = {"engine": cfg.engine, "risk": cfg.risk, "caps": caps}
    for key, t in TUNABLES.items():
        assert t.target in holders, key
        assert hasattr(holders[t.target], t.attr), key
        assert t.lo < t.hi and t.label and t.help, key
    # the catalogue the UI renders covers all of them exactly once
    keys = [row["key"] for row in spec()]
    assert sorted(keys) == sorted(TUNABLES)
    assert set(START_ARGS) == {"balance", "assets", "exchanges"}


def test_values_are_clamped_and_nonsense_is_refused():
    clean, errors = validate({"engine.min_net_edge": 0.03, "risk.kelly_fraction": 0.1})
    assert clean == {"engine.min_net_edge": 0.03, "risk.kelly_fraction": 0.1} and not errors

    clean, errors = validate({"engine.min_net_edge": 99, "risk.max_concurrent_positions": 3.7})
    assert clean["engine.min_net_edge"] == 0.5                  # clamped to the ceiling
    assert clean["risk.max_concurrent_positions"] == 4          # ints round
    assert not errors

    clean, errors = validate({"engine.min_net_edge": "abc", "nope.at.all": 1,
                              "engine.take_profit_enabled": "maybe"})
    assert clean == {}
    assert len(errors) == 3 and any("not a tunable" in e for e in errors)

    clean, _ = validate({"engine.take_profit_enabled": "false", "engine.stop_loss_enabled": 1})
    assert clean == {"engine.take_profit_enabled": False, "engine.stop_loss_enabled": True}

    assert validate({"engine.min_net_edge": float("nan")})[1]
    assert validate("not a dict")[1]


def test_scopes_layer_so_a_live_bot_can_differ_from_the_paper_one(tmp_path):
    cf = ControlFile(tmp_path / "controls.json")
    cf.write_scope("all", {"engine.min_net_edge": 0.03, "risk.kelly_fraction": 0.2})
    cf.write_scope("live", {"engine.min_net_edge": 0.06, "caps.max_order_usdc": 5})

    paper, _ = cf.merged("paper")
    live, _ = cf.merged("live")
    assert paper["engine.min_net_edge"] == 0.03 and "caps.max_order_usdc" not in paper
    assert live["engine.min_net_edge"] == 0.06                  # the scope wins
    assert live["risk.kelly_fraction"] == 0.2                   # inherited from "all"
    assert live["caps.max_order_usdc"] == 5.0

    # writing one scope leaves the others alone
    cf.write_scope("paper", {"risk.kelly_fraction": 0.5})
    assert cf.merged("live")[0]["risk.kelly_fraction"] == 0.2
    assert cf.merged("paper")[0]["risk.kelly_fraction"] == 0.5

    cf.clear_scope("live")
    assert cf.merged("live")[0]["engine.min_net_edge"] == 0.03  # back to the shared value
    assert cf.write_scope("nowhere", {})["ok"] is False


def test_a_missing_or_corrupt_file_is_empty_not_fatal(tmp_path):
    cf = ControlFile(tmp_path / "gone.json")
    assert cf.merged("paper") == ({}, [])
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert ControlFile(bad).merged("live") == ({}, [])
    bad.write_text('["a list"]', encoding="utf-8")
    assert ControlFile(bad).merged("live") == ({}, [])


def test_changed_fires_once_per_write(tmp_path):
    path = tmp_path / "c.json"
    cf = ControlFile(path)
    assert cf.changed() is False                # nothing on disk, nothing to apply
    cf.write_scope("all", {"engine.min_net_edge": 0.04})
    assert cf.changed() is True
    assert cf.changed() is False
    # a bot starting with settings already on disk picks them up on its first poll
    assert ControlFile(path).changed() is True


def test_contradictory_settings_are_reported_not_silently_kept(tmp_path):
    cf = ControlFile(tmp_path / "c.json")
    out = cf.write_scope("live", {"risk.min_order_usdc": 4.0, "caps.max_order_usdc": 2.0})
    assert out["ok"] and any("refused" in w for w in out["warnings"])
    out = cf.write_scope("all", {"engine.trade_window_start_s": 60, "engine.trade_window_end_s": 90})
    assert any("nothing can trade" in w for w in out["warnings"])


def test_apply_changes_the_objects_the_bot_is_using():
    cfg = BotConfig()
    caps = LiveCaps()
    exchange = type("X", (), {"caps": caps})()
    before = cfg.engine.min_net_edge

    changed = apply_to({"engine.min_net_edge": 0.09, "risk.kelly_fraction": 0.3,
                        "caps.max_order_usdc": 7.5, "engine.take_profit_enabled": False},
                       cfg, exchange)
    assert cfg.engine.min_net_edge == 0.09
    assert cfg.risk.kelly_fraction == 0.3
    assert caps.max_order_usdc == 7.5
    assert cfg.engine.take_profit_enabled is False
    assert len(changed) == 4 and any(f"{before:g} -> 0.09" in c for c in changed)

    # re-applying the same values reports nothing
    assert apply_to({"engine.min_net_edge": 0.09}, cfg, exchange) == []
    # a caps value with no live exchange is skipped, not an error
    assert apply_to({"caps.max_order_usdc": 3.0}, cfg, None) == []


def test_current_values_reads_back_what_apply_wrote():
    cfg = BotConfig()
    exchange = type("X", (), {"caps": LiveCaps()})()
    apply_to({"engine.min_net_edge": 0.07, "risk.max_concurrent_positions": 4}, cfg, exchange)
    now = current_values(cfg, exchange)
    assert now["engine.min_net_edge"] == 0.07
    assert now["risk.max_concurrent_positions"] == 4
    assert isinstance(now["engine.take_profit_enabled"], bool)
    # without a live exchange the caps simply are not reported
    assert not any(k.startswith("caps.") for k in current_values(cfg, None))


def test_kill_switch_round_trip(tmp_path):
    kill = tmp_path / "KILL"
    assert kill_active(kill) is False
    assert set_kill(True, kill) is True and kill.exists()
    assert set_kill(False, kill) is False and not kill.exists()
    assert set_kill(False, kill) is False          # removing twice is fine


# ───────────────────────────────── earnings ─────────────────────────────────


def _row(when: datetime, pnl: float, live: bool = False, asset: str = "BTC") -> dict:
    return {"ts": ms(when), "pnl": pnl, "asset": asset, "slug": f"{asset.lower()}-x",
            "mode": "LIVE-ARMED" if live else "live-paper", "live": live}


def test_log_round_trip_skips_junk_without_losing_good_rows(tmp_path):
    path = tmp_path / "earnings.jsonl"
    earnings.append(_row(datetime(2026, 9, 20, 10, 0), 1.5), path)
    earnings.append(_row(datetime(2026, 9, 20, 11, 0), -0.5, live=True), path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{not json}\n\n")
        fh.write(json.dumps({"pnl": 1.0}) + "\n")           # no ts
        fh.write(json.dumps({"ts": "x", "pnl": 1.0}) + "\n")
    rows = earnings.load(path)
    assert len(rows) == 2
    assert [r.pnl for r in rows] == [1.5, -0.5]
    assert rows[1].live is True and rows[1].mode == "LIVE-ARMED"
    assert earnings.load(tmp_path / "absent.jsonl") == []


def test_daily_buckets_fill_gaps_and_end_on_today():
    today = datetime(2026, 9, 22, 12, 0)
    rows = [
        earnings.Settlement(ts=ms(today - timedelta(days=4)), pnl=2.0, mode="p", live=False),
        earnings.Settlement(ts=ms(today - timedelta(days=4)), pnl=-0.5, mode="p", live=False),
        earnings.Settlement(ts=ms(today - timedelta(days=1)), pnl=1.0, mode="p", live=False),
    ]
    out = earnings.aggregate(rows, period="day", span=5, now=ms(today))
    assert [b["label"] for b in out["buckets"]][-1] == "22 Sep"
    assert len(out["buckets"]) == 5
    pnls = [b["pnl"] for b in out["buckets"]]
    assert pnls == [1.5, 0.0, 0.0, 1.0, 0.0]               # the empty days are zeros, not gaps
    assert [b["trades"] for b in out["buckets"]] == [2, 0, 0, 1, 0]
    assert [b["cum"] for b in out["buckets"]] == [1.5, 1.5, 1.5, 2.5, 2.5]
    assert out["totals"]["pnl"] == 2.5 and out["totals"]["trades"] == 3
    assert out["totals"]["wins"] == 2 and out["totals"]["losses"] == 1
    assert out["best"]["pnl"] == 1.5 and out["worst"]["pnl"] == 1.0
    assert out["totals"]["win_rate"] == pytest.approx(2 / 3, abs=1e-4)


def test_weekly_buckets_start_on_monday_and_monthly_on_the_first():
    rows = [
        earnings.Settlement(ts=ms(datetime(2026, 9, 15, 9, 0)), pnl=1.0, mode="p", live=False),
        earnings.Settlement(ts=ms(datetime(2026, 9, 20, 9, 0)), pnl=2.0, mode="p", live=False),
        earnings.Settlement(ts=ms(datetime(2026, 9, 21, 9, 0)), pnl=4.0, mode="p", live=False),
    ]
    now = ms(datetime(2026, 9, 22, 12, 0))
    weekly = earnings.aggregate(rows, period="week", span=2, now=now)
    # 15 Sep (Tue) and 20 Sep (Sun) share the week beginning Mon 14 Sep
    assert weekly["buckets"][0]["key"] == "2026-09-14" and weekly["buckets"][0]["pnl"] == 3.0
    assert weekly["buckets"][1]["key"] == "2026-09-21" and weekly["buckets"][1]["pnl"] == 4.0

    monthly = earnings.aggregate(rows, period="month", span=2, now=now)
    assert monthly["buckets"][-1]["key"] == "2026-09-01"
    assert monthly["buckets"][-1]["pnl"] == 7.0
    assert monthly["buckets"][-1]["label"] == "Sep 2026"
    assert monthly["buckets"][0]["key"] == "2026-08-01" and monthly["buckets"][0]["pnl"] == 0.0


def test_paper_and_real_money_are_never_summed_together():
    day = datetime(2026, 9, 22, 9, 0)
    rows = [
        earnings.Settlement(ts=ms(day), pnl=10.0, mode="live-paper", live=False),
        earnings.Settlement(ts=ms(day), pnl=-1.0, mode="LIVE-ARMED", live=True),
    ]
    now = ms(day)
    assert earnings.aggregate(rows, scope="all", span=1, now=now)["totals"]["pnl"] == 9.0
    assert earnings.aggregate(rows, scope="paper", span=1, now=now)["totals"]["pnl"] == 10.0
    live = earnings.aggregate(rows, scope="live", span=1, now=now)
    assert live["totals"]["pnl"] == -1.0 and live["totals"]["trades"] == 1
    assert live["modes"] == ["LIVE-ARMED"]


def test_history_before_the_visible_window_still_counts_in_the_running_total():
    today = datetime(2026, 9, 22, 12, 0)
    rows = [
        earnings.Settlement(ts=ms(today - timedelta(days=40)), pnl=5.0, mode="p", live=False),
        earnings.Settlement(ts=ms(today), pnl=1.0, mode="p", live=False),
    ]
    out = earnings.aggregate(rows, period="day", span=3, now=ms(today))
    assert out["opening_cum"] == 5.0            # the older row is off-chart but not forgotten
    assert out["buckets"][-1]["cum"] == 6.0
    assert out["totals"]["pnl"] == 1.0          # the window's own earnings
    assert out["totals"]["all_time_pnl"] == 6.0
    assert out["totals"]["all_time_trades"] == 2


def test_empty_history_still_renders_a_frame():
    out = earnings.aggregate([], period="week", now=ms(datetime(2026, 9, 22)))
    assert len(out["buckets"]) == earnings.DEFAULT_SPAN["week"]
    assert all(b["pnl"] == 0.0 and b["trades"] == 0 for b in out["buckets"])
    assert out["best"] is None and out["worst"] is None
    assert out["totals"]["win_rate"] is None and out["totals"]["per_trade"] is None


def test_span_and_period_are_bounded():
    now = ms(datetime(2026, 9, 22))
    assert earnings.aggregate([], period="nonsense", now=now)["period"] == "day"
    assert earnings.aggregate([], scope="nonsense", now=now)["scope"] == "all"
    assert len(earnings.aggregate([], period="day", span=10_000, now=now)["buckets"]) == earnings.MAX_SPAN
    assert len(earnings.aggregate([], period="day", span=0, now=now)["buckets"]) == 1


def test_a_settled_window_is_written_to_the_durable_log(tmp_path, monkeypatch):
    """The run ledger and the earnings record are written from one place.

    ``_ledger_append`` is called from all three settle sites, so testing it
    covers the early exit, the normal settlement and the fallback alike.
    """
    from types import SimpleNamespace

    from troll_poly_bot.live import LiveBot

    path = tmp_path / "earnings.jsonl"
    monkeypatch.setattr(earnings, "EARNINGS_LOG", path)

    written: list[dict] = []
    bot = SimpleNamespace(_append_log=written.append, ledger=[],
                          mode_label="LIVE-ARMED", is_live=True)

    LiveBot._ledger_append(bot, {"ts": 1.0, "event": "fill", "slug": "btc-x", "price": 0.4})
    assert earnings.load(path) == []                      # fills are not earnings

    LiveBot._ledger_append(bot, {"ts": 2.0, "event": "settle", "slug": "btc-x",
                                 "asset": "BTC", "pnl": 0.75, "exited": True})
    rows = earnings.load(path)
    assert len(rows) == 1
    assert rows[0].pnl == 0.75 and rows[0].asset == "BTC"
    assert rows[0].live is True and rows[0].mode == "LIVE-ARMED" and rows[0].exited is True
    assert len(written) == 2 and len(bot.ledger) == 2      # the run ledger still gets both
