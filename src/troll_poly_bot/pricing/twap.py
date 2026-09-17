"""TWAP-aware pricing for the 5m up/down markets.

Why this module exists
----------------------
The live markets resolve against

    https://data.chain.link/streams/btc-usd-twap-60s-streams

which is a **60-second time-weighted average price**, not a spot print. The
market resolves Up iff

    TWAP60(t_close) >= TWAP60(t_open)

Pricing that as though it were ``P(S_close > S_open)`` is wrong in two separate
ways, and both are large.

1. The market does not open at 50/50
-------------------------------------
The strike is the trailing 60s average at the window open, which is generally
NOT the spot at the window open. If price has been rising, spot already sits
above the trailing average and ``P(up) > 0.5`` the instant the window opens.
A model that assumes the strike is the opening spot starts every window with a
systematic error of the same sign as the recent trend.

2. Uncertainty collapses far faster near expiry
------------------------------------------------
Once there are fewer than 60 seconds left, part of the settling average has
already been observed and is locked in. Only the remaining sliver is random.

Writing ``u`` for seconds remaining and ``sigma`` for per-second log-return vol,
the conditional variance of the settling log-TWAP is

    u >= 60:   Var = sigma^2 * (u - 40)
    u <  60:   Var = sigma^2 * u^3 / 10800

(The two agree at ``u = 60``, both giving ``20 * sigma^2``.)

Against the naive spot-resolution variance of ``sigma^2 * u``:

    u = 300s   ->   260 vs 300        13% less uncertain
    u =  60s   ->    20 vs  60        3.0x less uncertain
    u =  30s   ->   2.5 vs  30       12x   less uncertain
    u =  10s   ->  0.09 vs  10      108x   less uncertain

So in the final minute a naive pricer is **badly underconfident**: it sees a
coin flip where the outcome is nearly locked. If the rest of the market prices
it naively too, that is a large, systematic, mechanical edge -- and unlike a
latency edge it does not require being fast, only being right.

Derivation
----------
Let ``X`` be log price, a driftless Brownian motion with per-second vol sigma
(drift is ~1e-6 against ~1.5e-3 diffusion at this horizon, so it is dropped).
The settling value is ``B = (1/60) * integral of X over [t_close-60, t_close]``.

For ``u >= 60`` the averaging window has not begun. With ``S = u - 60`` the time
until it starts, using ``Var(integral of W over [0,T]) = sigma^2 T^3/3`` and
``Cov(W_s, W_t) = sigma^2 min(s,t)``:

    Var(B) = sigma^2 * S + sigma^2 * 60/3 = sigma^2 * (u - 40)

For ``u < 60`` split the average into the observed part and the remainder:

    B = (1/60) * (I_observed + integral of X over [t, t_close])
    integral of X over [t, t_close] = u*X_t + integral of (X-X_t)
    Var = (1/3600) * sigma^2 * u^3/3 = sigma^2 * u^3 / 10800
    E[B] = (I_observed + u * X_t) / 60

which is what ``TwapState`` accumulates.

Caveats worth keeping in view
-----------------------------
* Chainlink averages *price*; this works in *log* price. Over a 60s window at
  crypto vol the two differ by a second-order term far below a 1-cent tick, but
  it is an approximation, not an identity.
* The exact averaging convention (sampling rate, weighting, whether the window
  is inclusive) is not something this module can verify. It assumes a uniform
  continuous average. Verify against the stream before trusting the tails.
* Ties resolve **Up** (the rule is ``>=``), so the boundary belongs to Up.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from .digital import DEFAULT_PRICER, FairValue, Pricer

#: The averaging window of the resolution stream, in seconds.
TWAP_WINDOW_S = 60.0


def twap_variance(seconds_left: float, sigma_per_sec: float,
                  window_s: float = TWAP_WINDOW_S) -> float:
    """Conditional variance of the settling log-TWAP, in log-price units."""
    u = max(seconds_left, 0.0)
    s2 = sigma_per_sec * sigma_per_sec
    if u >= window_s:
        # Var = sigma^2*(u - w) + sigma^2*w/3  =  sigma^2*(u - 2w/3)
        # w=60 -> sigma^2*(u - 40)
        return s2 * (u - 2.0 * window_s / 3.0)
    # sigma^2 * u^3 / (3 w^2);  w=60 -> sigma^2 * u^3 / 10800
    return s2 * (u ** 3) / (3.0 * window_s * window_s)


@dataclass(slots=True)
class TwapState:
    """Tracks the running average of the settling window.

    Feed it every oracle tick. Before the settling window opens it just holds
    the latest price; once inside, it accumulates the time-weighted integral
    that is already locked in.
    """

    window_s: float = TWAP_WINDOW_S
    #: (ts_ms, log_price) samples, trimmed to the window
    _samples: deque = field(default_factory=deque)
    _last_px: float = 0.0
    _last_ts: float = 0.0

    def update(self, price: float, ts_ms: float) -> None:
        if price <= 0.0:
            return
        self._last_px = price
        self._last_ts = ts_ms
        self._samples.append((ts_ms, math.log(price)))
        cutoff = ts_ms - self.window_s * 1000.0 * 2.0
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def ready(self) -> bool:
        return len(self._samples) >= 2

    def trailing_twap(self, now_ms: float) -> float | None:
        """Time-weighted average PRICE over the trailing window.

        This is the quantity the venue uses as the strike at the window open.
        """
        lm = self.trailing_log_twap(now_ms)
        return None if lm is None else math.exp(lm)

    def coverage_s(self, now_ms: float) -> float:
        """Seconds of the trailing window actually backed by observations.

        A strike taken from a partial window is not the venue's strike. On a
        trending market a short window sits closer to the latest price, so the
        strike comes out biased in the direction of the trend -- and stays
        biased for the whole five minutes. Callers must refuse to strike until
        this reaches the full window.
        """
        if not self._samples:
            return 0.0
        start = now_ms - self.window_s * 1000.0
        first = self._samples[0][0]
        return max(0.0, (min(now_ms, self._samples[-1][0]) - max(start, first)) / 1000.0)

    def fully_covered(self, now_ms: float, tolerance_s: float = 1.0) -> bool:
        return self.coverage_s(now_ms) >= self.window_s - tolerance_s

    def trailing_log_twap(self, now_ms: float) -> float | None:
        integral, span = self._integrate(now_ms - self.window_s * 1000.0, now_ms)
        if span <= 0.0:
            return None
        return integral / span

    def observed_range(self, start_ms: float, now_ms: float) -> tuple[float | None, float | None]:
        """Min/max observed log price in a window -- bounds for the mean."""
        vals = [v for t, v in self._samples if start_ms <= t <= now_ms]
        if not vals:
            return None, None
        return min(vals), max(vals)

    def observed_integral(self, start_ms: float, now_ms: float) -> tuple[float, float]:
        """Locked-in part of the settling average: (integral, seconds covered)."""
        return self._integrate(start_ms, now_ms)

    def _integrate(self, a_ms: float, b_ms: float) -> tuple[float, float]:
        """Trapezoidal integral of log price over [a, b], in log-price*seconds."""
        if b_ms <= a_ms or not self._samples:
            return 0.0, 0.0
        pts = list(self._samples)
        # clamp/extend with step-held values at the edges
        total = 0.0
        covered = 0.0
        for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
            lo, hi = max(t0, a_ms), min(t1, b_ms)
            if hi <= lo:
                continue
            # linear interpolation across the sample interval
            span = t1 - t0
            if span <= 0:
                continue
            f0 = v0 + (v1 - v0) * (lo - t0) / span
            f1 = v0 + (v1 - v0) * (hi - t0) / span
            total += 0.5 * (f0 + f1) * (hi - lo) / 1000.0
            covered += (hi - lo) / 1000.0
        # hold the last observed value forward to `b`
        t_last, v_last = pts[-1]
        if b_ms > max(t_last, a_ms):
            lo = max(t_last, a_ms)
            total += v_last * (b_ms - lo) / 1000.0
            covered += (b_ms - lo) / 1000.0
        return total, covered


def twap_fair_value(
    state: TwapState,
    strike: float,
    sigma_per_sec: float,
    now_ms: float,
    close_ts_ms: float,
    info_lag_s: float = 0.0,
    pricer: Pricer | None = None,
    window_s: float = TWAP_WINDOW_S,
    drift_log: float = 0.0,
) -> FairValue:
    """Fair probability that the settling TWAP finishes at or above ``strike``.

    ``drift_log`` is an expected log-price drift over the remaining horizon
    (from order flow, see features/orderflow.py). It shifts the settling mean:
    fully when the averaging window has not begun, and by the unobserved
    fraction ``u / window`` inside it. It is applied AFTER the range invariant
    so a tilt can never be mistaken for a broken accumulator.

    ``info_lag_s`` extends the horizon exactly as in ``digital.fair_value``:
    your oracle view is that stale, so the effective remaining time is longer
    than the clock says.
    """
    p = pricer or DEFAULT_PRICER
    spot = state._last_px
    if spot <= 0.0 or strike <= 0.0 or not state.ready:
        return FairValue(0.5, 0.0, sigma_per_sec, 0.0, 0.5)

    # `u` is measured from the information we actually HOLD, which is
    # `info_lag_s` old. Everything below must use the same reference point --
    # mixing them double-counts the lag.
    u = max((close_ts_ms - now_ms) / 1000.0, 0.0) + info_lag_s
    seen_until_ms = now_ms - info_lag_s * 1000.0
    log_strike = math.log(strike)
    x_t = math.log(spot)

    if u >= window_s:
        mean = x_t
    else:
        start_ms = close_ts_ms - window_s * 1000.0
        # Integrate only over what we have SEEN, i.e. up to `seen_until_ms`,
        # not up to `now_ms`. The remaining `u` seconds are the random part.
        # Integrating to now while also weighting x_t by u counts `info_lag_s`
        # seconds twice, inflating the mean by roughly
        # `info_lag_s / window_s * ln(price)`. On BTC (ln ~ 11.2) a 0.5s lag
        # inflated the mean by ~0.09 in log space -- equivalent to believing
        # price was 9% higher than it was. That produced a fair value of 1.000
        # on a token the market priced at 1 cent, and two losing trades.
        integral, _ = state.observed_integral(start_ms, seen_until_ms)
        mean = (integral + u * x_t) / window_s

        # Invariant: the settling average is a weighted mean of log prices, so
        # it cannot sit outside the range of the inputs. If it does, the
        # accounting is wrong -- refuse to price rather than emit a confident
        # nonsense number.
        lo, hi = state.observed_range(start_ms, seen_until_ms)
        if lo is not None:
            lo, hi = min(lo, x_t), max(hi, x_t)
            if not (lo - 1e-9 <= mean <= hi + 1e-9):
                return FairValue(0.5, 0.0, sigma_per_sec, u, 0.5)

    if drift_log:
        mean += drift_log * (1.0 if u >= window_s else u / window_s)

    var = twap_variance(u, sigma_per_sec, window_s)
    sd = math.sqrt(max(var, 1e-18))

    z = (mean - log_strike) / sd
    p_up = min(max(p.cdf(z), 0.0), 1.0)

    # sensitivity to a 25% error in sigma, same convention as digital.fair_value
    lo = p.cdf((mean - log_strike) / (sd * 1.25))
    hi = p.cdf((mean - log_strike) / (sd * 0.75))

    return FairValue(
        p_up=p_up,
        z=z,
        sigma_per_sec=sigma_per_sec,
        effective_horizon_s=u,
        uncertainty=abs(hi - lo) / 2.0,
    )
