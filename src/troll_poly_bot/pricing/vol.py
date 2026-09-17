"""Per-second realised volatility.

Everything is in per-second return stdev, never annualised. Annualising and
de-annualising a 5-minute horizon is a pointless round trip through a number
with an arbitrary day-count convention, and it is where unit bugs breed.

Reference points for sanity-checking your estimate:
    BTC at ~50% annualised  ->  ~0.9 bps/second
    BTC at ~80% annualised  ->  ~1.4 bps/second

Returns are sampled on a fixed time grid rather than tick-by-tick. Tick returns
over sub-millisecond gaps are dominated by bid-ask bounce, and dividing them by
a near-zero ``sqrt(dt)`` produces a vol estimate that explodes on quiet markets.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(slots=True)
class EwmaVol:
    """EWMA of squared per-second returns, sampled on a fixed grid.

    ``halflife_s`` trades off responsiveness against stability. Short half-lives
    track vol regime changes fast but make the tail probabilities jumpy, and the
    tails are where this strategy makes its money -- so do not set it too low.
    """

    grid_ms: float = 1000.0
    halflife_s: float = 120.0
    #: Absolute floor. Without it a stale/frozen feed reports zero vol, which
    #: makes every market look like a 0.0 or 1.0 certainty. That single missing
    #: line is a portfolio-ending bug.
    floor_per_sec: float = 2e-5          # 0.2 bps/s
    ceil_per_sec: float = 5e-3           # 50 bps/s -- reject absurd estimates
    #: Use a plain mean over this many samples before switching to the EWMA.
    #: An EWMA seeded from ONE return keeps that return's weight for a whole
    #: half-life: a single start-up glitch (a composite feed joining a second
    #: source) read as 6-9 bps/s on BTC for minutes, where 0.9 is right.
    warmup_samples: int = 30

    _var: float = field(default=0.0, init=False)
    _n: int = field(default=0, init=False)
    _last_px: float | None = field(default=None, init=False)
    _last_grid_ts: float = field(default=0.0, init=False)

    @property
    def _alpha(self) -> float:
        # decay per grid step
        steps_per_halflife = (self.halflife_s * 1000.0) / self.grid_ms
        return 1.0 - 0.5 ** (1.0 / max(steps_per_halflife, 1e-9))

    def update(self, price: float, ts: float) -> None:
        if price <= 0.0:
            return
        if self._last_px is None:
            self._last_px = price
            self._last_grid_ts = ts
            return

        elapsed = ts - self._last_grid_ts
        if elapsed < self.grid_ms:
            return

        # A long gap (reconnect, outage) must not be treated as one huge return.
        n_steps = max(1, int(elapsed / self.grid_ms))
        if n_steps > 60:
            # feed was out for a minute+; resync rather than poison the estimate
            self._last_px = price
            self._last_grid_ts = ts
            return

        r = math.log(price / self._last_px)
        dt_s = elapsed / 1000.0
        r_per_sec_sq = (r * r) / dt_s

        a = self._alpha
        if self._n < self.warmup_samples:
            self._var = (self._var * self._n + r_per_sec_sq) / (self._n + 1)
        else:
            self._var = (1.0 - a) * self._var + a * r_per_sec_sq
        self._n += 1
        self._last_px = price
        self._last_grid_ts = ts

    @property
    def ready(self) -> bool:
        """Do not trade off an unwarmed vol estimate."""
        return self._n >= max(30, int(self.halflife_s / (self.grid_ms / 1000.0) / 4))

    @property
    def sigma_per_sec(self) -> float:
        if self._n == 0:
            return self.floor_per_sec
        s = math.sqrt(max(self._var, 0.0))
        return min(max(s, self.floor_per_sec), self.ceil_per_sec)

    def sigma_over(self, seconds: float) -> float:
        """Return stdev over a horizon -- the denominator of the z-score."""
        return self.sigma_per_sec * math.sqrt(max(seconds, 1e-6))

    def annualised(self) -> float:
        """Only for human-readable logging. Never feed this back into pricing."""
        return self.sigma_per_sec * math.sqrt(365.25 * 24 * 3600)


@dataclass(slots=True)
class TwoScaleVol:
    """Volatility estimated at the horizon actually being priced.

    Why this exists
    ---------------
    Scaling 1-second returns by sqrt(t) assumes returns are iid. Crypto returns
    are not: measured on live BTC, the variance ratio between 60s and 1s
    sampling was **2.45** (ETH ~1.4). So a 1s-sampled sigma understates the
    5-minute vol this strategy prices by ~1.6x on BTC.

    That is not a rounding error. Sigma sits in the denominator of z, so a
    sigma 1.6x too small makes every probability far too extreme -- and in a
    live run it produced a systematic 7-10 cent disagreement with a liquid
    market, always in the same direction.

    How
    ---
    Two estimators: a fast one (1s grid, short half-life) that reacts to vol
    regime changes, and a slow one (30s grid, long half-life) that sees the
    trending component. Their ratio is smoothed heavily and applied as a
    horizon correction to the fast estimate, so the result stays responsive
    without inheriting the coarse grid's noise.
    """

    fast: EwmaVol = field(default_factory=lambda: EwmaVol(grid_ms=1000.0, halflife_s=120.0))
    slow: EwmaVol = field(default_factory=lambda: EwmaVol(grid_ms=30_000.0, halflife_s=3600.0))

    #: Until the slow estimator is warm, assume the 1s estimate is this much
    #: too low. 1.4 is between the measured BTC (1.57) and ETH (1.17) figures;
    #: it is a prior, and the measured ratio replaces it as soon as it exists.
    default_ratio: float = 1.4
    min_ratio: float = 0.8
    max_ratio: float = 3.0
    ratio_alpha: float = 0.02          # heavy smoothing; this moves slowly

    _ratio: float = field(default=0.0, init=False)

    def update(self, price: float, ts: float) -> None:
        self.fast.update(price, ts)
        self.slow.update(price, ts)
        if self.slow.ready and self.fast.ready:
            f = self.fast.sigma_per_sec
            if f > 0:
                obs = min(max(self.slow.sigma_per_sec / f, self.min_ratio), self.max_ratio)
                self._ratio = obs if self._ratio <= 0 else (
                    (1 - self.ratio_alpha) * self._ratio + self.ratio_alpha * obs
                )

    @property
    def ready(self) -> bool:
        return self.fast.ready

    @property
    def variance_ratio(self) -> float:
        """Measured (slow/fast)^2. 1.0 would mean returns really are iid."""
        r = self._ratio if self._ratio > 0 else self.default_ratio
        return r * r

    @property
    def sigma_per_sec(self) -> float:
        r = self._ratio if self._ratio > 0 else self.default_ratio
        s = self.fast.sigma_per_sec * r
        return min(max(s, self.fast.floor_per_sec), self.fast.ceil_per_sec)

    def sigma_over(self, seconds: float) -> float:
        return self.sigma_per_sec * math.sqrt(max(seconds, 1e-6))

    def annualised(self) -> float:
        return self.sigma_per_sec * math.sqrt(365.25 * 24 * 3600)
