"""Execution-aware replay: fills only against a real offer, FOK, real fees."""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from troll_poly_bot.backtest.archive import ArchivedWindow, Grid
from troll_poly_bot.backtest.replay import ReplayConfig, new_policy, replay
from troll_poly_bot.signals.costs import FeeSchedule


def _window(ua, ub, da, db, outcome="UP", touch=True):
    n = len(ua)
    t = 1_000_000.0 + 1000.0 * np.arange(n)
    close = t[0] + 200_000.0            # ~200 s left: inside the trade window
    g = Grid(t=t, left=(close - t) / 1000.0, spot=np.full(n, 100.0),
             up=np.array([(a + b) / 2 if not (np.isnan(a) or np.isnan(b)) else (a if not np.isnan(a) else b)
                          for a, b in zip(ua, ub)]),
             ub=np.array(ub, float), ua=np.array(ua, float), db=np.array(db, float), da=np.array(da, float),
             has_touch=np.full(n, touch))
    return ArchivedWindow(slug="w", asset="BTC", strike=100.0, open_ts=t[0], close_ts=close,
                          outcome_recorded=outcome, outcome=outcome, outcome_source="bell",
                          n_points=n, first_left=g.left[0], last_left=g.left[-1], grid=g)


def _rows(w, p_up):
    g = w.grid
    return pd.DataFrame({
        "slug": "w", "asset": "BTC", "open_ts": w.open_ts, "t": g.t, "i": np.arange(len(g.t)),
        "left": g.left, "mkt_p": g.up, "mdl_p": p_up, "mdl_unc": 0.0,
        "up_ask": g.ua, "up_bid": g.ub, "down_ask": g.da, "down_bid": g.db,
        "one_sided": np.isnan(g.ua) | np.isnan(g.ub), "spread": g.ua - g.ub,
        "ret_1s": 0.0, "ret_5s": 0.0, "ret_10s": 0.0, "ret_30s": 0.0, "ret_60s": 0.0, "ret_120s": 0.0,
        "sigma_bps": 1.0, "vol_ratio": 1.0,
    })


NAN = float("nan")


def test_fill_at_landing_ask_with_real_fee():
    w = _window(ua=[0.40, 0.40, 0.40], ub=[0.39, 0.39, 0.39], da=[0.61, 0.61, 0.61], db=[0.60, 0.60, 0.60])
    df = _rows(w, p_up=0.70)
    tr = replay([w], df, new_policy("mdl_p", 0.05, FeeSchedule()), ReplayConfig(shares=10))
    f = tr[tr["status"] == "filled"]
    assert len(f) == 1 and f.iloc[0]["side"] == "UP"
    assert f.iloc[0]["fill"] == pytest.approx(0.40)
    assert f.iloc[0]["fee"] == pytest.approx(0.07 * 0.4 * 0.6)
    assert f.iloc[0]["pnl"] == pytest.approx(10 * (1 - 0.40 - 0.07 * 0.4 * 0.6))


def test_one_sided_book_at_landing_means_no_fill():
    w = _window(ua=[0.40, NAN, NAN], ub=[0.39, 0.39, 0.39], da=[0.61, 0.61, 0.61], db=[0.60, 0.60, 0.60])
    df = _rows(w, p_up=0.70)
    tr = replay([w], df, new_policy("mdl_p", 0.05, FeeSchedule(), require_two_sided=False), ReplayConfig(shares=10))
    assert (tr["status"] == "no_liquidity").any()
    assert not (tr["status"] == "filled").any()


def test_ask_that_moved_past_the_limit_is_fok_rejected():
    w = _window(ua=[0.40, 0.45, 0.45], ub=[0.39, 0.44, 0.44], da=[0.61, 0.56, 0.56], db=[0.60, 0.55, 0.55])
    df = _rows(w, p_up=0.70)
    tr = replay([w], df, new_policy("mdl_p", 0.05, FeeSchedule()), ReplayConfig(shares=10, cross_ticks=1))
    assert tr.iloc[0]["status"] == "fok_rejected"


def test_no_trade_when_edge_is_inside_costs():
    w = _window(ua=[0.60, 0.60, 0.60], ub=[0.59, 0.59, 0.59], da=[0.41, 0.41, 0.41], db=[0.40, 0.40, 0.40])
    df = _rows(w, p_up=0.62)          # 2c gross edge < fee + threshold
    tr = replay([w], df, new_policy("mdl_p", 0.05, FeeSchedule()), ReplayConfig(shares=10))
    assert len(tr) == 0


def test_one_position_per_window():
    w = _window(ua=[0.40] * 6, ub=[0.39] * 6, da=[0.61] * 6, db=[0.60] * 6)
    df = _rows(w, p_up=0.70)
    tr = replay([w], df, new_policy("mdl_p", 0.05, FeeSchedule()), ReplayConfig(shares=10))
    assert (tr["status"] == "filled").sum() == 1
