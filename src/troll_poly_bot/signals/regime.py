"""Short-term regime labels, computed from the same features the model sees.

Regimes are labels for *reporting and gating*, not inputs to the pricer. The
replay measures expectancy per regime; the strategy config can then exclude a
regime that measured negative. Thresholds are in units of the asset's own
smoothed sigma, so the same rule means the same thing on BTC and DOGE.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Regime:
    vol: str          # LOW_VOLATILITY | NORMAL_VOLATILITY | HIGH_VOLATILITY
    trend: str        # STRONG_UPTREND | STRONG_DOWNTREND | SIDEWAYS
    liquidity: str    # NORMAL | WIDE_SPREAD | LIQUIDITY_COLLAPSE
    extreme: bool     # EXTREME_MOVE in the last 30 s

    @property
    def label(self) -> str:
        parts = [self.vol, self.trend, self.liquidity]
        if self.extreme:
            parts.append("EXTREME_MOVE")
        return "|".join(parts)


def classify_regime(
    feats: dict[str, float],
    high_vol_ratio: float = 1.5,
    low_vol_ratio: float = 0.6,
    trend_sigmas: float = 1.5,
    extreme_sigmas: float = 3.0,
    wide_spread: float = 0.03,
) -> Regime:
    vr = feats.get("vol_ratio", math.nan)
    if math.isnan(vr):
        vol = "NORMAL_VOLATILITY"
    elif vr >= high_vol_ratio:
        vol = "HIGH_VOLATILITY"
    elif vr <= low_vol_ratio:
        vol = "LOW_VOLATILITY"
    else:
        vol = "NORMAL_VOLATILITY"

    sig = feats.get("sigma_bps", math.nan)
    r120 = feats.get("ret_120s", math.nan)
    if math.isnan(sig) or math.isnan(r120) or sig <= 0:
        trend = "SIDEWAYS"
    else:
        t = r120 / (sig * math.sqrt(120.0))
        trend = ("STRONG_UPTREND" if t >= trend_sigmas
                 else "STRONG_DOWNTREND" if t <= -trend_sigmas else "SIDEWAYS")

    if feats.get("one_sided", 0.0) >= 1.0:
        liq = "LIQUIDITY_COLLAPSE"
    elif not math.isnan(feats.get("spread", math.nan)) and feats["spread"] >= wide_spread:
        liq = "WIDE_SPREAD"
    else:
        liq = "NORMAL"

    r30 = feats.get("ret_30s", math.nan)
    extreme = (not math.isnan(r30) and not math.isnan(sig) and sig > 0
               and abs(r30) >= extreme_sigmas * sig * math.sqrt(30.0))
    return Regime(vol=vol, trend=trend, liquidity=liq, extreme=extreme)
