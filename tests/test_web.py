"""Tests for the dashboard's parameter plumbing.

The schema is the only thing standing between a slider and a config field, so
the round trip and the guards get covered. A silently-dropped parameter would
show up as a run that ignores what you asked for.
"""
from __future__ import annotations

import json

import pytest

from troll_poly_bot.execution.latency import PROFILES
from troll_poly_bot.sim import SimResult
from troll_poly_bot.web.schema import GROUPS, apply_params, build_schema
from troll_poly_bot.web.server import serialise


def test_schema_exposes_every_declared_field_with_a_value():
    schema = build_schema()
    for group in schema["groups"]:
        for f in group["fields"]:
            assert f["value"] is not None, f"{group['id']}.{f['key']} has no default"
            assert f["target"] in ("sim", "strategy", "vol", "top")


def test_schema_field_keys_actually_exist_on_their_config():
    """Catches a renamed dataclass field before it becomes a dead slider."""
    sim, bot = apply_params({})
    owner = {"sim": sim, "strategy": bot.strategy, "vol": bot.vol}
    for group in GROUPS:
        if group["target"] == "top":
            continue
        for f in group["fields"]:
            assert hasattr(owner[group["target"]], f["key"]), \
                f"{group['target']}.{f['key']} is not a real config field"


def test_schema_is_json_serialisable():
    json.dumps(build_schema())


def test_every_profile_is_offered():
    names = {p["name"] for p in build_schema()["profiles"]}
    assert names == set(PROFILES)


def test_values_round_trip():
    sim, bot = apply_params({
        "sim": {"n_windows": 77, "maker_bias": 0.11, "maker_lag_ms": 450},
        "strategy": {"min_edge": 0.033, "kelly_fraction": 0.4},
        "vol": {"halflife_s": 90},
        "top": {"fee_rate": 0.015, "starting_balance": 2500, "latency_seed": 12},
    })
    assert sim.n_windows == 77
    assert sim.maker_bias == pytest.approx(0.11)
    assert bot.strategy.min_edge == pytest.approx(0.033)
    assert bot.vol.halflife_s == pytest.approx(90)
    assert bot.fees.rate == pytest.approx(0.015)
    assert bot.starting_balance == pytest.approx(2500)
    assert bot.latency_seed == 12


def test_scaled_field_converts_units():
    """The UI shows bps/sec; the model wants a plain per-second stdev."""
    sim, _ = apply_params({"sim": {"sigma_per_sec": 0.9}})
    assert sim.sigma_per_sec == pytest.approx(9e-5)


def test_int_fields_stay_ints():
    sim, bot = apply_params({"sim": {"n_windows": 50.0}, "top": {"latency_seed": 3.0}})
    assert isinstance(sim.n_windows, int)
    assert isinstance(bot.latency_seed, int)


def test_toggle_round_trips():
    _, bot = apply_params({"strategy": {"use_twap": False}})
    assert bot.strategy.use_twap is False
    _, bot2 = apply_params({"strategy": {"use_twap": True}})
    assert bot2.strategy.use_twap is True


def test_unknown_and_junk_values_are_ignored_not_fatal():
    """A stale browser tab should fall back to defaults, not 500."""
    sim, bot = apply_params({
        "sim": {"not_a_field": 1, "n_windows": "banana"},
        "strategy": {"min_edge": None},
        "nonsense": {"x": 1},
    })
    assert sim.n_windows > 0
    assert bot.strategy.min_edge > 0


def test_window_count_is_clamped():
    assert apply_params({"sim": {"n_windows": 99999}})[0].n_windows == 3000
    assert apply_params({"sim": {"n_windows": -5}})[0].n_windows == 1


def test_inverted_bounds_are_repaired():
    """Bounds the dataclass cannot express, but that would break the run."""
    _, bot = apply_params({"strategy": {
        "min_fair_for_trade": 0.8, "max_fair_for_trade": 0.2,
        "trade_window_start_s": 5, "trade_window_end_s": 90,
    }})
    assert bot.strategy.max_fair_for_trade > bot.strategy.min_fair_for_trade
    assert bot.strategy.trade_window_start_s > bot.strategy.trade_window_end_s


def test_serialise_shape_is_json_clean():
    r = SimResult(
        profile_name="home_broadband", reaction_lag_ms=260.0,
        final_equity=1100.0, pnl=100.0, n_trades=5, n_windows_traded=3,
        latency={"fill_rate": 0.5}, skips={"no_edge": 2},
        trades=[{"fair": float("nan"), "price": 0.8, "size": 10,
                 "won": 1.0, "secs_left": 30.0}],
        equity_curve=[{"window": 0, "equity": 1100.0, "pnl": 100.0,
                       "trades": 5, "fills": 1}],
    )
    payload = serialise(r, starting_balance=1000.0)
    # NaN is not valid JSON — it must not survive serialisation
    assert payload["trades"][0]["fair"] is None
    json.dumps(payload, allow_nan=False)
    assert payload["return_pct"] == pytest.approx(10.0)


def test_max_drawdown_measures_peak_to_trough():
    r = SimResult(
        profile_name="x", reaction_lag_ms=0, final_equity=0, pnl=0,
        n_trades=0, n_windows_traded=0,
        equity_curve=[{"equity": v} for v in (1000, 1200, 900, 1100, 850)],
    )
    assert r.max_drawdown() == pytest.approx(-350.0)   # 1200 -> 850


def test_max_drawdown_of_a_monotonic_rise_is_zero():
    r = SimResult(
        profile_name="x", reaction_lag_ms=0, final_equity=0, pnl=0,
        n_trades=0, n_windows_traded=0,
        equity_curve=[{"equity": v} for v in (1000, 1100, 1200)],
    )
    assert r.max_drawdown() == pytest.approx(0.0)


def test_price_band_keeps_the_bot_out_of_penny_longshots():
    """A live run bought 20 shares of a 1-cent longshot on a model that said 6%.
    The tail is where the estimate is least trustworthy and where the
    favourite-longshot bias works hardest against a buyer."""
    from troll_poly_bot.config import StrategyConfig
    from troll_poly_bot.execution.latency import VPS_NEARBY
    from troll_poly_bot.execution.paper import FeeModel
    from troll_poly_bot.pricing.vol import EwmaVol
    from troll_poly_bot.strategy.taker import TakerStrategy
    from troll_poly_bot.types import BookLevel, Market, OrderBook

    cfg = StrategyConfig(use_twap=False, min_edge=0.01)
    vol = EwmaVol()
    for i in range(400):
        vol.update(100_000.0 * (1 + 1e-5 * ((-1) ** i)), i * 1000.0)

    strat = TakerStrategy(cfg=cfg, latency=VPS_NEARBY, fees=FeeModel(rate=0.0),
                          vol={"BTC": vol})
    now = 400_000.0
    market = Market(condition_id="c", asset="BTC", yes_token_id="UP",
                    no_token_id="DOWN", strike=100_000.0,
                    open_ts=now - 200_000.0, close_ts=now + 60_000.0)
    # a 1-cent ask on the UP token, far out of the money
    up = OrderBook("UP", bids=[BookLevel(0.01, 500)], asks=[BookLevel(0.01, 500)], ts=now)
    down = OrderBook("DOWN", bids=[BookLevel(0.98, 500)], asks=[BookLevel(0.99, 500)], ts=now)

    intent = strat.evaluate(
        market=market, book_up=up, book_down=down, spot=99_000.0,
        spot_age_ms=50.0, book_age_ms=50.0, now=now,
        gross_exposure=0.0, position_usdc=0.0, balance=100.0,
    )
    assert intent is None or intent.expected_price >= cfg.min_trade_price


def test_equity_does_not_value_an_unsettled_position_at_zero():
    """Between a window closing and settlement landing the book stops quoting.
    Marking those positions at zero reported a $10.79 loss on a book that was
    actually up ~$3."""
    from troll_poly_bot.execution.latency import INSTANT, LatencyModel
    from troll_poly_bot.execution.paper import FeeModel, PaperExchange
    from troll_poly_bot.types import Fill, Side

    ex = PaperExchange(LatencyModel(INSTANT), FeeModel(rate=0.0), starting_balance=100.0)
    ex.positions["T"].token_id = "T"
    ex.positions["T"].apply(Fill(order_id="o", token_id="T", side=Side.BUY,
                                 price=0.80, size=10.0, fee=0.0,
                                 exchange_ts=0.0, ack_ts=0.0, decision_ts=0.0))
    ex.balance -= 8.0

    assert ex.equity(lambda t: None) == pytest.approx(92.0)      # position zeroed
    assert ex.equity(lambda t: 0.80) == pytest.approx(100.0)     # marked at cost
