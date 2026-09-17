"""A start-up glitch must not own the vol estimate for a half-life."""
from __future__ import annotations

import math

from troll_poly_bot.pricing.vol import EwmaVol


def _run(returns_bps):
    v = EwmaVol(grid_ms=1000.0, halflife_s=120.0)
    px, t = 100.0, 0.0
    v.update(px, t)
    for r in returns_bps:
        t += 1000.0
        px *= math.exp(r * 1e-4)
        v.update(px, t)
    return v


def test_single_early_glitch_is_averaged_not_seeded():
    glitch_first = _run([9.0] + [1.0] * 29)
    steady = _run([1.0] * 30)
    # the mean of squares over the warm-up: (81 + 29) / 30 -> ~1.9 bps, not ~9
    assert glitch_first.sigma_per_sec * 1e4 < 2.2
    assert glitch_first.sigma_per_sec > steady.sigma_per_sec
    assert glitch_first.ready and steady.ready


def test_after_warmup_the_ewma_takes_over():
    v = _run([1.0] * 200)
    assert abs(v.sigma_per_sec * 1e4 - 1.0) < 1e-6
