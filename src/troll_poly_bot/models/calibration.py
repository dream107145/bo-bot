"""Calibration metrics and post-hoc calibrators.

A predicted 0.70 must come true about 70% of the time or every edge computed
from it is fiction. These helpers measure that (Brier, log loss, reliability
buckets, ECE) and correct it (Platt scaling on the logit; isotonic via PAVA).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

EPS = 1e-4

#: The buckets the strategy reports calibration in.
PROB_BUCKETS: tuple[tuple[float, float], ...] = (
    (0.50, 0.55), (0.55, 0.60), (0.60, 0.65), (0.65, 0.70),
    (0.70, 0.75), (0.75, 0.80), (0.80, 1.01),
)


def brier(p: np.ndarray, y: np.ndarray) -> float:
    p, y = np.asarray(p, float), np.asarray(y, float)
    return float(np.mean((p - y) ** 2)) if len(p) else math.nan


def logloss(p: np.ndarray, y: np.ndarray) -> float:
    p, y = np.clip(np.asarray(p, float), EPS, 1 - EPS), np.asarray(y, float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))) if len(p) else math.nan


def reliability_table(p: np.ndarray, y: np.ndarray, fold_to_favourite: bool = True) -> list[dict]:
    """Realised frequency per predicted-probability bucket.

    With ``fold_to_favourite`` a prediction of 0.30 for Up is scored as 0.70
    for Down, so both tails land in the same 50%+ buckets the strategy uses.
    """
    p, y = np.asarray(p, float), np.asarray(y, float)
    if fold_to_favourite:
        flip = p < 0.5
        p = np.where(flip, 1 - p, p)
        y = np.where(flip, 1 - y, y)
    out = []
    for lo, hi in PROB_BUCKETS:
        m = (p >= lo) & (p < hi)
        if m.sum() == 0:
            continue
        out.append({"bucket": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": int(m.sum()),
                    "predicted": round(float(p[m].mean()), 4), "realised": round(float(y[m].mean()), 4),
                    "gap": round(float(y[m].mean() - p[m].mean()), 4)})
    return out


def ece(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> float:
    """Expected calibration error: |realised - predicted| weighted by bin mass."""
    p, y = np.asarray(p, float), np.asarray(y, float)
    if not len(p):
        return math.nan
    edges = np.linspace(0, 1, n_bins + 1)
    total = 0.0
    for lo, hi in zip(edges, edges[1:]):
        m = (p >= lo) & (p < hi) if hi < 1 else (p >= lo) & (p <= hi)
        if m.sum():
            total += m.mean() * abs(y[m].mean() - p[m].mean())
    return float(total)


@dataclass(slots=True)
class PlattScaler:
    """p' = sigmoid(a * logit(p) + b). a < 1 shrinks toward 0.5 (overconfidence fix)."""
    a: float = 1.0
    b: float = 0.0

    @classmethod
    def fit(cls, p: np.ndarray, y: np.ndarray, l2: float = 1e-3) -> "PlattScaler":
        from .logistic import LogisticModel
        p = np.clip(np.asarray(p, float), 0.005, 0.995)
        x = np.log(p / (1 - p))[:, None]
        m = LogisticModel.fit(x, np.asarray(y, float), ["logit"], l2=l2)
        a = float(m.coef[0] / m.scale[0])
        b = float(m.intercept - a * m.mean[0])
        return cls(a=a, b=b)

    def apply(self, p: np.ndarray | float):
        p = np.clip(np.asarray(p, float), 0.005, 0.995)
        z = self.a * np.log(p / (1 - p)) + self.b
        out = 1.0 / (1.0 + np.exp(-z))
        return float(out) if out.ndim == 0 else out


def isotonic_fit(p: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Pool-adjacent-violators. Returns (x_knots, y_knots) for np.interp."""
    p, y = np.asarray(p, float), np.asarray(y, float)
    order = np.argsort(p)
    x, v = p[order], y[order]
    # blocks of (sum, count, x_lo, x_hi)
    blocks: list[list[float]] = []
    for xi, yi in zip(x, v):
        blocks.append([yi, 1.0, xi, xi])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, c, lo, hi = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
            blocks[-1][3] = hi
    xs = np.array([0.5 * (b[2] + b[3]) for b in blocks])
    ys = np.array([b[0] / b[1] for b in blocks])
    return xs, ys
