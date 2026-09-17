"""Digital (binary) option pricing for 5-minute up/down markets.

A 5m up/down market is a cash-or-nothing digital on an oracle print. Over a
300-second horizon the drift term is ~1e-6 against a ~1.5e-3 diffusion term, so
it is dropped:

    z = ln(S / K) / (sigma_per_sec * sqrt(T))
    P(up) = Phi(z)

Latency enters the PRICER, not just the execution simulator
-----------------------------------------------------------
This is the part people miss. Your spot observation is ``info_lag`` seconds old.
Measured from the information you actually hold, the horizon to resolution is
not ``T`` -- it is ``T + info_lag``, because the price has already been moving
for ``info_lag`` seconds in ways you cannot see:

    z_effective = ln(S_seen / K) / (sigma * sqrt(T + info_lag))

The consequence is exact and unforgiving: staleness pulls every probability
toward 0.50, and it does so hardest in the tails and hardest near expiry, which
is precisely where this strategy wants to trade. With 10 seconds left, a 250ms
information lag is a 1.2% widening of the horizon -- negligible. With 0.5
seconds left it is a 50% widening, and a "sure thing" is nothing of the kind.

A bot that prices with T and executes with T + lag will look brilliant on paper
and bleed in production. This module prices with the lag included.

Fat tails
---------
Crypto returns at 1-5 minute horizons are strongly leptokurtic, so the Gaussian
overstates confidence in exactly the region this strategy trades. The default
pricer is therefore Student-t with nu=4, standardised to unit variance.

Treat that as a PRIOR, not an answer. The real fix is ``CalibratedPricer``,
fitted to your own recorded data: bin history by (z, time remaining), measure
the realised frequency of "up", and use that. The empirical curve also absorbs
the market's favourite-longshot bias, which no parametric distribution captures.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from scipy.special import stdtr

SQRT2 = math.sqrt(2.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT2))


@dataclass(slots=True)
class FairValue:
    """A fair price plus an honest statement of how much you trust it."""
    p_up: float
    z: float
    sigma_per_sec: float
    effective_horizon_s: float
    #: 1-sigma band on p_up implied by uncertainty in the vol estimate. Trade
    #: only when your edge clears this, or you are just trading vol noise.
    uncertainty: float

    @property
    def p_down(self) -> float:
        return 1.0 - self.p_up


class Pricer(Protocol):
    def cdf(self, z: float) -> float: ...


class GaussianPricer:
    """Textbook. Understates tail risk on crypto. Here as a baseline."""

    def cdf(self, z: float) -> float:
        return norm_cdf(z)


@dataclass(slots=True)
class StudentTPricer:
    """Standardised Student-t. The sane default prior for crypto short horizons.

    Uses ``scipy.special.stdtr`` rather than ``scipy.stats.t.cdf``: the frozen
    distribution wrapper costs ~50us per call, which is unusable in a loop that
    prices every market on every tick.
    """
    nu: float = 4.0

    def cdf(self, z: float) -> float:
        if self.nu <= 2.0:
            return norm_cdf(z)
        scale = math.sqrt(self.nu / (self.nu - 2.0))
        return float(stdtr(self.nu, z * scale))


@dataclass(slots=True)
class CalibratedPricer:
    """Empirical z -> P(up) curve fitted from recorded outcomes.

    Build the table with ``research/fit_calibration.py`` once you have recorded
    a few thousand resolved markets. Until then it falls back to ``base``.

    This is the single highest-value upgrade in the whole repo: it replaces an
    assumption about the return distribution with a measurement of it, and it
    silently corrects for the market's own biases at the same time.
    """
    z_grid: np.ndarray | None = None     # bin edges, ascending
    p_grid: np.ndarray | None = None     # realised P(up) per bin
    base: Pricer = GaussianPricer()

    def cdf(self, z: float) -> float:
        if self.z_grid is None or self.p_grid is None or len(self.z_grid) < 2:
            return self.base.cdf(z)
        return float(np.interp(z, self.z_grid, self.p_grid))


DEFAULT_PRICER: Pricer = StudentTPricer(nu=4.0)


def fair_value(
    spot: float,
    strike: float,
    sigma_per_sec: float,
    seconds_to_close: float,
    info_lag_s: float = 0.0,
    pricer: Pricer | None = None,
    sigma_rel_error: float = 0.25,
    compute_uncertainty: bool = True,
) -> FairValue:
    """Fair probability that the market resolves UP.

    Args:
        spot: last spot you have SEEN (already stale by ``info_lag_s``).
        strike: the window's reference/open price.
        sigma_per_sec: per-second return stdev from EwmaVol.
        seconds_to_close: wall-clock seconds from now to resolution.
        info_lag_s: age of your spot observation, in seconds. Pass
            ``latency.md_spot / 1000`` -- see the module docstring. Passing 0
            here is the classic way to build a bot that backtests beautifully.
        sigma_rel_error: relative uncertainty in the vol estimate, used to size
            the confidence band. 25% is realistic for a 2-minute EWMA.
        compute_uncertainty: the band costs two extra CDF evaluations. Pass
            False on the hot path and call ``vol_uncertainty`` only for the few
            candidates that clear the edge gate.
    """
    p = pricer or DEFAULT_PRICER

    # Your information is this old, so the horizon you are really integrating
    # over is longer than the one on the clock.
    horizon = max(seconds_to_close + info_lag_s, 1e-6)

    if spot <= 0.0 or strike <= 0.0:
        return FairValue(0.5, 0.0, sigma_per_sec, horizon, 0.5)

    denom = sigma_per_sec * math.sqrt(horizon)
    if denom <= 0.0:
        return FairValue(0.5, 0.0, sigma_per_sec, horizon, 0.5)

    log_moneyness = math.log(spot / strike)
    z = log_moneyness / denom
    p_up = p.cdf(z)

    uncertainty = (
        vol_uncertainty(log_moneyness, sigma_per_sec, horizon, sigma_rel_error, p)
        if compute_uncertainty
        else 0.0
    )

    return FairValue(
        p_up=min(max(p_up, 0.0), 1.0),
        z=z,
        sigma_per_sec=sigma_per_sec,
        effective_horizon_s=horizon,
        uncertainty=uncertainty,
    )


def vol_uncertainty(
    log_moneyness: float,
    sigma_per_sec: float,
    horizon_s: float,
    sigma_rel_error: float,
    pricer: Pricer,
) -> float:
    """1-sigma band on p_up implied by uncertainty in the vol estimate.

    Sigma is an estimate with a fat error bar, and near the money the price is
    insensitive to it while in the tails it dominates. Requiring the edge to
    clear this band is what stops the bot from trading its own vol noise.
    """
    root = math.sqrt(horizon_s)
    lo = pricer.cdf(log_moneyness / (sigma_per_sec * (1 + sigma_rel_error) * root))
    hi = pricer.cdf(log_moneyness / (sigma_per_sec * (1 - sigma_rel_error) * root))
    return abs(hi - lo) / 2.0


def edge_bps_to_price(edge: float) -> float:
    """Convenience: an edge in probability terms IS a price edge, 1:1.

    Shares pay 0 or 1, so a 0.04 probability edge is 4 cents per share. This
    function exists only to make that identity explicit at call sites, because
    conflating the two is a common and expensive confusion.
    """
    return edge
