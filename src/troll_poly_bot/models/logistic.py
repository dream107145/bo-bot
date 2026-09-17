"""L2-regularised logistic regression on standardised features.

Dependency-free (numpy + scipy) so the live bot can load fitted coefficients
without scikit-learn. Rows with a NaN feature are dropped when fitting and
imputed with the training mean when predicting -- the same rule in both
places, so a missing feature live behaves exactly as it did in research.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import minimize


@dataclass(slots=True)
class LogisticModel:
    features: list[str]
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray
    intercept: float
    l2: float = 1.0
    n_train: int = 0
    meta: dict = field(default_factory=dict)

    # ------------------------------------------------------------- fitting

    @classmethod
    def fit(
        cls,
        X: np.ndarray,
        y: np.ndarray,
        features: list[str],
        l2: float = 1.0,
        sample_weight: np.ndarray | None = None,
    ) -> "LogisticModel":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=float)
        ok = ~np.isnan(X).any(axis=1) & ~np.isnan(y)
        X, y = X[ok], y[ok]
        w = np.ones(len(y)) if sample_weight is None else np.asarray(sample_weight, float)[ok]
        if len(y) < 10:
            raise ValueError("too few rows to fit")
        mean = X.mean(axis=0)
        scale = X.std(axis=0)
        scale[scale < 1e-12] = 1.0
        Z = (X - mean) / scale
        n, k = Z.shape
        wsum = w.sum()

        def objective(theta: np.ndarray):
            b, c = theta[0], theta[1:]
            eta = b + Z @ c
            # stable log-loss
            ll = np.logaddexp(0.0, -eta) * y + np.logaddexp(0.0, eta) * (1.0 - y)
            loss = float((w * ll).sum() / wsum + 0.5 * l2 * (c @ c) / n)
            p = 1.0 / (1.0 + np.exp(-eta))
            g = (w * (p - y)) / wsum
            grad = np.concatenate([[g.sum()], Z.T @ g + l2 * c / n])
            return loss, grad

        res = minimize(objective, np.zeros(k + 1), jac=True, method="L-BFGS-B")
        return cls(features=list(features), mean=mean, scale=scale,
                   coef=res.x[1:], intercept=float(res.x[0]), l2=l2, n_train=int(n))

    # ---------------------------------------------------------- predicting

    def decision(self, X: np.ndarray) -> np.ndarray:
        X = np.asarray(X, dtype=float)
        X = np.where(np.isnan(X), self.mean, X)
        return self.intercept + ((X - self.mean) / self.scale) @ self.coef

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return 1.0 / (1.0 + np.exp(-self.decision(X)))

    def predict_one(self, feats: dict[str, float]) -> float:
        x = np.array([feats.get(f, math.nan) for f in self.features], dtype=float)
        return float(self.predict_proba(x[None, :])[0])

    # ---------------------------------------------------------- persistence

    def to_dict(self) -> dict:
        return {
            "features": self.features, "mean": self.mean.tolist(), "scale": self.scale.tolist(),
            "coef": self.coef.tolist(), "intercept": self.intercept, "l2": self.l2,
            "n_train": self.n_train, "meta": self.meta,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "LogisticModel":
        return cls(features=list(d["features"]), mean=np.array(d["mean"], float),
                   scale=np.array(d["scale"], float), coef=np.array(d["coef"], float),
                   intercept=float(d["intercept"]), l2=float(d.get("l2", 1.0)),
                   n_train=int(d.get("n_train", 0)), meta=dict(d.get("meta") or {}))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=1)

    @classmethod
    def load(cls, path: str) -> "LogisticModel":
        with open(path, encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def describe(self) -> str:
        rows = sorted(zip(self.features, self.coef), key=lambda kv: -abs(kv[1]))
        return "  ".join(f"{f}={c:+.3f}" for f, c in rows) + f"  b={self.intercept:+.3f}"
