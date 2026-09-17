"""Feature engine: causal, time-based, tolerant of missing inputs."""
from __future__ import annotations

import math

from troll_poly_bot.features.engine import FEATURE_NAMES, FeatureEngine, SpotHistory


def _feed(fe: FeatureEngine, t0: float, seconds: int, start: float = 100.0, drift: float = 0.0):
    px = start
    for s in range(seconds):
        px *= math.exp(drift)
        fe.update_spot("BTC", px, t0 + s * 1000.0)
    return px


def test_spot_history_returns_are_time_based():
    h = SpotHistory()
    for s in range(200):
        h.update(100.0 * math.exp(0.0001 * s), 1_000_000.0 + s * 1000.0)
    now = 1_000_000.0 + 199 * 1000.0
    assert h.logret(now, 10) is not None
    assert abs(h.logret(now, 10) - 0.001) < 1e-9
    assert h.logret(now, 500) is None                       # not enough history
    assert h.realised_sigma(now, 30.0) is not None


def test_features_are_causal():
    fe = FeatureEngine()
    t0 = 1_000_000.0
    _feed(fe, t0, 300, drift=0.0002)
    now = t0 + 299 * 1000.0
    kw = dict(asset="BTC", slug="m", strike=100.0, spot=105.0, now_ms=now, seconds_left=200.0,
              window_s=300.0, up_price=0.6, up_bid=0.59, up_ask=0.61, sigma_per_sec=1e-4,
              model_p_up=0.65, model_z=0.4)
    first = fe.features(**kw)
    # append future data, recompute at the same instant: nothing may change
    for s in range(300, 400):
        fe.update_spot("BTC", 200.0, t0 + s * 1000.0)
    again = fe.features(**kw)
    for k in FEATURE_NAMES:
        a, b = first[k], again[k]
        assert (math.isnan(a) and math.isnan(b)) or a == b, k


def test_feature_values_and_missing_inputs():
    fe = FeatureEngine()
    t0 = 1_000_000.0
    _feed(fe, t0, 200, drift=0.0001)
    now = t0 + 199 * 1000.0
    f = fe.features(asset="BTC", slug="m", strike=100.0, spot=101.0, now_ms=now, seconds_left=150.0,
                    window_s=300.0, up_price=0.55, up_bid=None, up_ask=0.56, sigma_per_sec=2e-4,
                    model_p_up=0.6, model_z=0.3)
    assert abs(f["ret_10s"] - 10.0) < 1e-6               # 10 x 1 bps
    assert f["dist_bps"] > 0
    assert f["one_sided"] == 1.0 and math.isnan(f["spread"])
    assert f["sigma_bps"] == 2.0
    assert f["left_ratio"] == 0.5
    assert f["mdl_minus_mkt"] == 0.6 - 0.55
    g = fe.features(asset="ETH", slug="x", strike=0.0, spot=0.0, now_ms=now, seconds_left=10.0,
                    window_s=300.0, up_price=None, up_bid=None, up_ask=None, sigma_per_sec=1e-4,
                    model_p_up=None, model_z=None)
    assert math.isnan(g["ret_1s"]) and math.isnan(g["mkt_p"]) and math.isnan(g["mdl_p"])
    assert g["one_sided"] == 1.0


def test_market_change_features_use_market_history():
    fe = FeatureEngine()
    t0 = 1_000_000.0
    _feed(fe, t0, 100)
    for s in range(60):
        fe.update_market("m", 0.50 + 0.001 * s, t0 + s * 1000.0)
    now = t0 + 59 * 1000.0
    f = fe.features(asset="BTC", slug="m", strike=100.0, spot=100.0, now_ms=now, seconds_left=100.0,
                    window_s=300.0, up_price=0.559, up_bid=0.55, up_ask=0.57, sigma_per_sec=1e-4,
                    model_p_up=0.5, model_z=0.0)
    assert abs(f["mkt_chg_10s"] - 0.010) < 1e-9
    assert abs(f["mkt_chg_30s"] - 0.030) < 1e-9
