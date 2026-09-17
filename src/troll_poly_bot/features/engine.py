"""Feature engine shared by the live bot and the historical replay.

One code path for both on purpose: a feature computed one way in research
and another way live is the most common source of a backtest that does not
survive contact with production. Every feature here is causal -- it uses only
samples at or before ``now`` -- and every lookback is time-based, so a feed
that ticks faster or slower produces the same number.

Feature groups (see ``FEATURE_NAMES``):

* price:        returns over 1/5/10/30/60/120 s in bps, momentum, acceleration,
                distance from strike
* volatility:   smoothed sigma (bps/s), short realised sigma, their ratio
* market:       Up price, its logit, spread, one-sidedness, its own 10/30 s change
* model:        analytic TWAP fair value, its logit, the z-score behind it
* time:         seconds left, fraction of the window left
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

NAN = float("nan")

FEATURE_NAMES: tuple[str, ...] = (
    "ret_1s", "ret_5s", "ret_10s", "ret_30s", "ret_60s", "ret_120s",
    "momentum", "acceleration", "dist_bps",
    "sigma_bps", "rv_30s_bps", "vol_ratio",
    "mkt_p", "mkt_logit", "spread", "one_sided", "mkt_chg_10s", "mkt_chg_30s",
    "mdl_p", "mdl_logit", "z", "mdl_minus_mkt",
    "left", "left_ratio",
)


def logit(p: float, eps: float = 0.005) -> float:
    p = min(max(p, eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


@dataclass(slots=True)
class SpotHistory:
    """Time-indexed log-price history, sampled no finer than ``min_gap_ms``."""
    keep_s: float = 420.0
    min_gap_ms: float = 200.0
    _t: list[float] = field(default_factory=list)
    _lp: list[float] = field(default_factory=list)

    def update(self, price: float, ts_ms: float) -> None:
        if price <= 0.0:
            return
        if self._t and ts_ms - self._t[-1] < self.min_gap_ms:
            return
        if self._t and ts_ms < self._t[-1]:
            return                                  # out of order: ignore
        self._t.append(ts_ms)
        self._lp.append(math.log(price))
        cutoff = ts_ms - self.keep_s * 1000.0
        if self._t and self._t[0] < cutoff - 60_000.0:
            k = bisect.bisect_left(self._t, cutoff)
            del self._t[:k]
            del self._lp[:k]

    def at(self, ts_ms: float, max_gap_ms: float = 5_000.0) -> float | None:
        """Log price as of ``ts_ms`` (last observation at or before it)."""
        k = bisect.bisect_right(self._t, ts_ms) - 1
        if k < 0:
            return None
        if ts_ms - self._t[k] > max_gap_ms:
            return None
        return self._lp[k]

    def logret(self, now_ms: float, lookback_s: float) -> float | None:
        a = self.at(now_ms - lookback_s * 1000.0)
        b = self.at(now_ms)
        if a is None or b is None:
            return None
        return b - a

    def realised_sigma(self, now_ms: float, window_s: float, step_s: float = 1.0) -> float | None:
        """Per-second sigma from ``step_s`` returns over the trailing window."""
        n = int(window_s / step_s)
        if n < 5:
            return None
        acc, cnt = 0.0, 0
        prev = self.at(now_ms - window_s * 1000.0)
        for i in range(1, n + 1):
            cur = self.at(now_ms - (window_s - i * step_s) * 1000.0)
            if prev is not None and cur is not None:
                acc += (cur - prev) ** 2
                cnt += 1
            prev = cur
        if cnt < max(5, n // 2):
            return None
        return math.sqrt(acc / cnt / step_s)


@dataclass(slots=True)
class _MarketHistory:
    t: list[float] = field(default_factory=list)
    p: list[float] = field(default_factory=list)

    def update(self, price: float, ts_ms: float) -> None:
        if self.t and ts_ms <= self.t[-1]:
            return
        self.t.append(ts_ms)
        self.p.append(price)
        if len(self.t) > 4000:
            del self.t[:1000]
            del self.p[:1000]

    def at(self, ts_ms: float) -> float | None:
        k = bisect.bisect_right(self.t, ts_ms) - 1
        return self.p[k] if k >= 0 else None


@dataclass(slots=True)
class FeatureEngine:
    spot: dict[str, SpotHistory] = field(default_factory=dict)
    market: dict[str, _MarketHistory] = field(default_factory=dict)

    def update_spot(self, asset: str, price: float, ts_ms: float) -> None:
        self.spot.setdefault(asset, SpotHistory()).update(price, ts_ms)

    def update_market(self, slug: str, up_price: float, ts_ms: float) -> None:
        self.market.setdefault(slug, _MarketHistory()).update(up_price, ts_ms)

    def forget_market(self, slug: str) -> None:
        self.market.pop(slug, None)

    def features(
        self,
        asset: str,
        slug: str,
        strike: float,
        spot: float,
        now_ms: float,
        seconds_left: float,
        window_s: float,
        up_price: float | None,
        up_bid: float | None,
        up_ask: float | None,
        sigma_per_sec: float,
        model_p_up: float | None,
        model_z: float | None,
        extra: dict[str, float] | None = None,
    ) -> dict[str, float]:
        """Compute every feature at ``now_ms``. Missing inputs give NaN.

        ``extra`` carries features computed elsewhere (order flow) so the
        regime classifier and the logs see one flat dict.
        """
        h = self.spot.get(asset)
        f: dict[str, float] = {k: NAN for k in FEATURE_NAMES}

        def bps(v: float | None) -> float:
            return NAN if v is None else v * 1e4

        if h is not None:
            for lb in (1, 5, 10, 30, 60, 120):
                f[f"ret_{lb}s"] = bps(h.logret(now_ms, lb))
            r5, r10, r30 = f["ret_5s"], f["ret_10s"], f["ret_30s"]
            f["momentum"] = r30
            # the last 5 s of return against the 5 s before it
            f["acceleration"] = r5 - (r10 - r5) if not (math.isnan(r5) or math.isnan(r10)) else NAN
            rv = h.realised_sigma(now_ms, 30.0, 1.0)
            f["rv_30s_bps"] = bps(rv)
        if spot > 0 and strike > 0:
            f["dist_bps"] = math.log(spot / strike) * 1e4
        f["sigma_bps"] = sigma_per_sec * 1e4
        if not math.isnan(f["rv_30s_bps"]) and f["sigma_bps"] > 0:
            f["vol_ratio"] = f["rv_30s_bps"] / f["sigma_bps"]

        if up_price is not None and not math.isnan(up_price):
            f["mkt_p"] = up_price
            f["mkt_logit"] = logit(up_price)
            mh = self.market.get(slug)
            if mh is not None:
                for lb in (10, 30):
                    prev = mh.at(now_ms - lb * 1000.0)
                    f[f"mkt_chg_{lb}s"] = NAN if prev is None else up_price - prev
        two_sided = up_bid is not None and up_ask is not None and not (
            math.isnan(up_bid) or math.isnan(up_ask))
        f["one_sided"] = 0.0 if two_sided else 1.0
        f["spread"] = (up_ask - up_bid) if two_sided else NAN

        if model_p_up is not None and not math.isnan(model_p_up):
            f["mdl_p"] = model_p_up
            f["mdl_logit"] = logit(model_p_up)
            if not math.isnan(f["mkt_p"]):
                f["mdl_minus_mkt"] = model_p_up - f["mkt_p"]
        if model_z is not None:
            f["z"] = max(-8.0, min(8.0, model_z))
        f["left"] = seconds_left
        f["left_ratio"] = seconds_left / window_s if window_s > 0 else NAN
        if extra:
            for k, v in extra.items():
                f[k] = NAN if v is None else float(v)
        return f
