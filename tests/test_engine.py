"""The decision engine says NO with a reason, and YES only when everything clears."""
from __future__ import annotations

import math

import pytest

from troll_poly_bot.execution.latency import COLOCATED
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.feeds.spot import CompositeView
from troll_poly_bot.pricing.twap import TwapState
from troll_poly_bot.pricing.vol import EwmaVol
from troll_poly_bot.risk.limits import RiskConfig, RiskManager
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.strategy.engine import EngineConfig, Reason, StrategyEngine
from troll_poly_bot.types import BookLevel, Market, OrderBook

T0 = 1_000_000_000.0


def _meta(strike=100.0, open_ts=T0, close_ts=T0 + 300_000.0):
    return MarketMeta(
        market=Market(condition_id="c", asset="BTC", yes_token_id="UP", no_token_id="DOWN",
                      strike=strike, open_ts=open_ts, close_ts=close_ts, slug="btc-updown-5m-1"),
        resolution_source="", accepting_orders=True, duration_s=300.0, fee=FeeSchedule())


def _book(token, bid, ask, size=200.0):
    bids = [BookLevel(bid, size)] if bid is not None else []
    asks = [BookLevel(ask, size), BookLevel(round(ask + 0.01, 2), size)] if ask is not None else []
    return OrderBook(token, bids=bids, asks=asks, ts=T0)


def _view(price, n=3, dev=1.0):
    return CompositeView("BTC", price, price, n, dev, dev, 50.0, {"binance": price})


def _warm(price=100.0, sigma_per_sec=1e-4):
    """A vol estimator and TWAP state fed 400 s of a flat-ish path."""
    vol = EwmaVol(grid_ms=1000.0, halflife_s=120.0)
    tw = TwapState()
    px = price
    for s in range(400):
        px = price * (1 + sigma_per_sec * ((s % 2) * 2 - 1))     # tiny alternating moves
        t = T0 - 400_000.0 + s * 1000.0
        vol.update(px, t)
        tw.update(px, t)
    tw.update(price, T0)
    return vol, tw


def _engine(**cfg):
    risk = RiskManager(cfg=RiskConfig(), starting_balance=100.0)
    return StrategyEngine(EngineConfig(**cfg), risk), risk


def _eval(engine, meta, up, down, spot=100.0, now=T0 + 100_000.0, spot_age=300.0, book_age=200.0,
          vol=None, tw=None, view=None, orderflow=None):
    if vol is None:
        vol, tw = _warm()
        # the TWAP state is fed the same composite price the view carries
        tw.update(spot, now - 100.0)
    return engine.evaluate(meta, up, down, view if view is not None else _view(spot), spot_age, book_age,
                           now, vol, tw, COLOCATED, 100.0, orderflow=orderflow)


def test_outside_time_window_and_cannot_land():
    eng, _ = _engine()
    ev = _eval(eng, _meta(), _book("UP", 0.5, 0.51), _book("DOWN", 0.49, 0.5), now=T0 + 290_000.0)
    assert ev.reason is Reason.OUTSIDE_TIME_WINDOW
    eng2, _ = _engine(trade_window_end_s=0.0, latency_safety_margin_ms=5000.0)
    ev2 = _eval(eng2, _meta(), _book("UP", 0.5, 0.51), _book("DOWN", 0.49, 0.5), now=T0 + 298_000.0)
    assert ev2.reason is Reason.CANNOT_LAND_IN_TIME


def test_stale_and_inconsistent_data():
    eng, _ = _engine()
    ev = _eval(eng, _meta(), _book("UP", 0.5, 0.51), _book("DOWN", 0.49, 0.5), spot_age=5000.0)
    assert ev.reason is Reason.STALE_DATA
    ev2 = _eval(eng, _meta(), _book("UP", 0.5, 0.51), _book("DOWN", 0.49, 0.5),
                view=CompositeView("BTC", 100.0, 100.0, 3, 40.0, 40.0, 50.0, {}))
    assert ev2.reason is Reason.DATA_INCONSISTENT
    ev3 = _eval(eng, _meta(strike=0.0), _book("UP", 0.5, 0.51), _book("DOWN", 0.49, 0.5))
    assert ev3.reason is Reason.NO_STRIKE


def test_model_not_ready():
    eng, _ = _engine()
    ev = _eval(eng, _meta(), _book("UP", 0.5, 0.51), _book("DOWN", 0.49, 0.5),
               vol=EwmaVol(), tw=TwapState())
    assert ev.reason is Reason.MODEL_NOT_READY


def test_no_offer_when_book_is_one_sided_both_ways():
    eng, _ = _engine()
    # decided window: winner bids only, loser asks only at 0.01 (outside band)
    ev = _eval(eng, _meta(), _book("UP", 0.99, None), _book("DOWN", None, 0.01))
    assert ev.reason in (Reason.NO_OFFER, Reason.PRICE_BAND, Reason.BAD_REGIME)
    assert not ev.tradeable


def test_edge_too_small_when_market_agrees_with_model():
    eng, _ = _engine()
    # spot at strike -> model ~0.5; market quotes 0.50/0.51 both sides
    ev = _eval(eng, _meta(), _book("UP", 0.49, 0.51), _book("DOWN", 0.49, 0.51))
    assert ev.reason is Reason.EDGE_TOO_SMALL
    assert ev.p_used is not None and abs(ev.p_used - 0.5) < 0.1
    assert eng.rejections["EDGE_TOO_SMALL"] == 1


def test_trades_a_clearly_mispriced_offer_then_refuses_to_double_up():
    eng, risk = _engine(min_net_edge=0.05, uncertainty_charge=0.0)
    # spot well above strike with a warm low-vol estimate -> model P(up) high;
    # the book offers UP at 0.55 (badly cheap)
    ev = _eval(eng, _meta(strike=100.0), _book("UP", 0.54, 0.55), _book("DOWN", 0.44, 0.45), spot=100.4)
    assert ev.tradeable, (ev.reason, ev.detail, ev.p_used)
    assert ev.side == "UP" and ev.size >= 5.0
    assert ev.limit_price == pytest.approx(0.56)
    assert ev.net_edge >= 0.05
    order = eng.to_order(ev)
    assert order.token_id == "UP" and order.size == ev.size
    # a fill on this market makes the next evaluation ALREADY_POSITIONED
    risk.on_fill("btc-updown-5m-1", "BTC", int(T0 // 1000), "UP", ev.size * 0.55, ev.size)
    ev2 = _eval(eng, _meta(strike=100.0), _book("UP", 0.54, 0.55), _book("DOWN", 0.44, 0.45), spot=100.4)
    assert ev2.reason is Reason.ALREADY_POSITIONED


def test_revalidate_never_chases():
    eng, _ = _engine(min_net_edge=0.05, uncertainty_charge=0.0)
    ev = _eval(eng, _meta(), _book("UP", 0.54, 0.55), _book("DOWN", 0.44, 0.45), spot=100.4)
    assert ev.tradeable
    assert eng.revalidate(ev, _book("UP", 0.54, 0.55)) is None
    assert eng.revalidate(ev, _book("UP", 0.60, 0.61)) is Reason.HIGH_SLIPPAGE
    assert eng.revalidate(ev, _book("UP", 0.99, None)) is Reason.NO_OFFER
    assert eng.revalidate(ev, _book("UP", 0.54, 0.55, size=1.0)) is Reason.LOW_LIQUIDITY


def test_bad_regime_blocks_and_snapshot_reports():
    eng, _ = _engine(excluded_liquidity=("LIQUIDITY_COLLAPSE",))
    ev = _eval(eng, _meta(), _book("UP", None, 0.55), _book("DOWN", 0.44, 0.45), spot=100.4)
    assert ev.reason is Reason.BAD_REGIME
    snap = eng.snapshot()
    assert "BAD_REGIME" in snap["rejections"]
    assert snap["last"]["btc-updown-5m-1"]["reason"] == "BAD_REGIME"
    assert math.isfinite(snap["last"]["btc-updown-5m-1"]["p_analytic"])


def test_sanity_halt_ignores_tail_mids_and_rolls_off():
    eng, _ = _engine(sanity_min_samples=5, sanity_window=8, sanity_min_mid=0.10)
    # a decided market: mid 0.03, model ~0. Tail diffs must not count.
    for _ in range(10):
        ev = _eval(eng, _meta(), _book("UP", 0.02, 0.04), _book("DOWN", 0.96, 0.98), spot=99.0)
        assert ev.reason is not Reason.MODEL_SANITY
    assert eng.agreement["BTC"].n == 0
    # mid-range market that persistently disagrees with the model -> halt
    for _ in range(5):
        ev = _eval(eng, _meta(), _book("UP", 0.49, 0.51), _book("DOWN", 0.49, 0.51), spot=100.4)
    assert ev.reason is Reason.MODEL_SANITY
    # agreement resumes: the disagreeing samples roll out of the window
    for _ in range(8):
        ev = _eval(eng, _meta(), _book("UP", 0.49, 0.51), _book("DOWN", 0.49, 0.51), spot=100.0)
    assert ev.reason is not Reason.MODEL_SANITY
    assert eng.agreement["BTC"].n == 8


def test_order_flow_tilts_the_probability_within_bounds():
    from troll_poly_bot.features.orderflow import OrderFlowCalibration
    books = (_book("UP", 0.49, 0.51), _book("DOWN", 0.49, 0.51))
    eng, _ = _engine(ofi_drift_bps=1.0, ofi_max_drift_bps=3.0, ofi_calibrate_live=False)
    base = _eval(eng, _meta(), *books)
    up = _eval(eng, _meta(), *books, orderflow={"ofi_score": 1.0})
    down = _eval(eng, _meta(), *books, orderflow={"ofi_score": -1.0})
    assert up.p_analytic > base.p_analytic > down.p_analytic
    assert up.ofi_drift_bps == pytest.approx(1.0) and down.ofi_drift_bps == pytest.approx(-1.0)
    assert up.features["ofi_score"] == 1.0                     # flows through to the feature dict
    # a huge score is clamped to [-1, 1] and the drift to the cap
    big = _eval(eng, _meta(), *books, orderflow={"ofi_score": 40.0})
    assert big.ofi_drift_bps == pytest.approx(1.0)
    eng2, _ = _engine(ofi_drift_bps=10.0, ofi_max_drift_bps=3.0, ofi_calibrate_live=False)
    capped = _eval(eng2, _meta(), *books, orderflow={"ofi_score": 1.0})
    assert capped.ofi_drift_bps == pytest.approx(3.0)
    # disabled, or no score: no tilt at all
    off, _ = _engine(ofi_enabled=False)
    assert _eval(off, _meta(), *books, orderflow={"ofi_score": 1.0}).p_analytic == pytest.approx(base.p_analytic)
    assert _eval(eng, _meta(), *books, orderflow={"ofi_score": math.nan}).ofi_drift_bps == 0.0
    # live calibration: nothing tilts until the measured slope is significant;
    # then the slope is used WITH its sign (a reverting tape tilts against the flow)
    eng3, _ = _engine(ofi_drift_bps=1.0, ofi_calibrate_live=True, ofi_min_calibration_n=5, ofi_horizon_s=10,
                      ofi_min_t=2.0)
    assert _eval(eng3, _meta(), *books, orderflow={"ofi_score": 1.0}).ofi_drift_bps == 0.0   # uncalibrated
    cal = OrderFlowCalibration(horizons_s=(10,), sample_interval_s=10.0)
    for i in range(40):
        cal._add(10, s=(i % 2) * 2 - 1.0, r=-2.0 * ((i % 2) * 2 - 1.0))     # slope -2 bps, exact
    eng3.ofi_calibration["BTC"] = cal
    assert _eval(eng3, _meta(), *books, orderflow={"ofi_score": 1.0}).ofi_drift_bps == pytest.approx(-2.0)
    cal2 = OrderFlowCalibration(horizons_s=(10,), sample_interval_s=10.0)
    for i in range(40):
        cal2._add(10, s=(i % 2) * 2 - 1.0, r=2.5 * ((i % 2) * 2 - 1.0))     # slope +2.5 bps
    eng3.ofi_calibration["BTC"] = cal2
    assert _eval(eng3, _meta(), *books, orderflow={"ofi_score": 0.5}).ofi_drift_bps == pytest.approx(1.25)
    # a noisy relationship (t below the bar) does not tilt
    weak = OrderFlowCalibration(horizons_s=(10,), sample_interval_s=1.0)
    for i in range(40):
        weak._add(10, s=(i % 2) * 2 - 1.0, r=0.3 * ((i % 2) * 2 - 1.0) + (1.0 if i % 3 else -2.0))
    eng3.ofi_calibration["BTC"] = weak
    assert abs(weak.t_stat(10)) < 2.0
    assert _eval(eng3, _meta(), *books, orderflow={"ofi_score": 1.0}).ofi_drift_bps == 0.0
