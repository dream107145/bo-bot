"""Probability model and calibration utilities."""
from __future__ import annotations

import math

import numpy as np
import pytest

from troll_poly_bot.models.calibration import (
    PlattScaler, brier, ece, isotonic_fit, logloss, reliability_table,
)
from troll_poly_bot.models.logistic import LogisticModel


def _synthetic(n=4000, seed=0):
    rng = np.random.default_rng(seed)
    x = rng.normal(size=(n, 2))
    eta = 1.5 * x[:, 0] - 0.5 * x[:, 1] + 0.2
    p = 1 / (1 + np.exp(-eta))
    y = (rng.random(n) < p).astype(float)
    return x, y, p


def test_logistic_recovers_relationship():
    x, y, p = _synthetic()
    m = LogisticModel.fit(x, y, ["a", "b"], l2=0.1)
    pred = m.predict_proba(x)
    assert brier(pred, y) < brier(np.full_like(y, y.mean()), y)
    assert abs(brier(pred, y) - brier(p, y)) < 0.01
    # signs of the standardised coefficients match the generator
    assert m.coef[0] > 0 and m.coef[1] < 0


def test_logistic_drops_nan_rows_when_fitting_and_imputes_when_predicting():
    x, y, _ = _synthetic(500)
    x[:10, 0] = np.nan
    m = LogisticModel.fit(x, y, ["a", "b"])
    assert m.n_train == 490
    p_nan = m.predict_one({"a": math.nan, "b": 0.0})
    p_mean = m.predict_one({"a": float(m.mean[0]), "b": 0.0})
    assert p_nan == pytest.approx(p_mean)


def test_logistic_roundtrip():
    x, y, _ = _synthetic(300)
    m = LogisticModel.fit(x, y, ["a", "b"])
    m2 = LogisticModel.from_dict(m.to_dict())
    assert np.allclose(m.predict_proba(x), m2.predict_proba(x))


def test_platt_shrinks_overconfident_predictions():
    rng = np.random.default_rng(1)
    true_p = rng.uniform(0.05, 0.95, 5000)
    y = (rng.random(5000) < true_p).astype(float)
    over = 1 / (1 + np.exp(-2.0 * np.log(true_p / (1 - true_p))))     # too extreme
    sc = PlattScaler.fit(over, y)
    assert sc.a < 1.0
    assert logloss(sc.apply(over), y) < logloss(over, y)


def test_isotonic_is_monotone_and_improves_fit():
    rng = np.random.default_rng(2)
    p = rng.uniform(0, 1, 2000)
    y = (rng.random(2000) < p ** 2).astype(float)          # miscalibrated
    xs, ys = isotonic_fit(p, y)
    assert np.all(np.diff(ys) >= -1e-12)
    cal = np.interp(p, xs, ys)
    assert brier(cal, y) < brier(p, y)


def test_reliability_folds_to_favourite_and_ece_zero_when_perfect():
    p = np.array([0.2, 0.8, 0.9, 0.1])
    y = np.array([0.0, 1.0, 1.0, 0.0])
    rows = reliability_table(p, y)
    assert all(r["predicted"] >= 0.5 for r in rows)
    assert sum(r["n"] for r in rows) == 4
    assert ece(np.array([0.5, 0.5]), np.array([1.0, 0.0])) == pytest.approx(0.0)
