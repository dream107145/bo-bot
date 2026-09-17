"""Order-flow state, features and live calibration."""
from __future__ import annotations

import math

import pytest

from troll_poly_bot.features.orderflow import OrderFlowCalibration, OrderFlowState

T0 = 1_000_000.0


def test_trade_flow_windows_and_bounds():
    of = OrderFlowState()
    # 20 s of buying, then 5 s of heavier selling
    for s in range(20):
        of.on_trade(100.0, True, T0 + s * 1000.0)
    for s in range(20, 25):
        of.on_trade(300.0, False, T0 + s * 1000.0)
    now = T0 + 24_500.0
    f = of.features(now)
    assert -1.0 <= f["tfi_5s"] <= 1.0 and f["tfi_5s"] < 0          # recent sells dominate
    assert f["tfi_30s"] > f["tfi_5s"]                                # the longer window remembers the buying
    assert f["trade_rate_30s"] == pytest.approx(25 / 30)
    assert f["notional_30s"] == pytest.approx(20 * 100 + 5 * 300)


def test_book_imbalance_sums_fresh_sources_only():
    of = OrderFlowState(quote_max_age_ms=1000)
    of.on_quote("binance", 10.0, 2.0, T0)
    of.on_quote("bybit", 1.0, 1.0, T0)
    of.on_quote("coinbase", 0.5, 20.0, T0 - 5000.0)                 # stale, ignored
    assert of.book_imbalance(T0 + 100) == pytest.approx((11 - 3) / 14)
    of.on_quote("binance", None, 2.0, T0 + 200)                     # one-sided quote ignored
    assert of.book_imbalance(T0 + 300) == pytest.approx((11 - 3) / 14)


def test_features_nan_when_empty_and_score_averages():
    of = OrderFlowState()
    f = of.features(T0)
    assert math.isnan(f["tfi_15s"]) and math.isnan(f["obi"]) and math.isnan(f["ofi_score"])
    of.on_trade(50.0, True, T0)
    of.on_quote("binance", 1.0, 3.0, T0)
    f = of.features(T0 + 10)
    assert f["tfi_15s"] == 1.0 and f["obi"] == pytest.approx(-0.5)
    assert f["ofi_score"] == pytest.approx(0.25)


def test_calibration_recovers_a_planted_slope():
    cal = OrderFlowCalibration(horizons_s=(10, 30))
    x = 0.0
    # the score is held for 30 s blocks and the price drifts 2 bps per unit
    # of score per 10 s while it holds, one tick per second
    scores = [(((i // 30) % 7) - 3) / 3.0 for i in range(600)]
    for i, s in enumerate(scores):
        t = T0 + i * 1000.0
        cal.resolve(t, x)                     # settle whatever is 10 s / 30 s old
        cal.record(s, t, x)
        x += 2e-4 * s / 10.0                  # the next second's move
    for k in range(1, 40):
        cal.resolve(T0 + (600 + k) * 1000.0, x)
    st = cal.stats(10)
    assert st["n"] > 500
    assert st["n_eff"] == int(st["n"] * 1.0 / 10)          # overlapping 1 s samples, 10 s horizon
    # block boundaries dilute the slope a little; the sign and size are clear
    assert st["slope_bps"] == pytest.approx(2.0, rel=0.35)
    assert st["corr"] > 0.8
    assert cal.slope_bps(10) == st["slope_bps"]
    assert "10s" in cal.report() and "30s" in cal.report()


def test_calibration_ignores_nan_scores():
    cal = OrderFlowCalibration()
    cal.record(math.nan, T0, 0.0)
    cal.resolve(T0 + 60_000.0, 0.0)
    assert cal.n(10) == 0
