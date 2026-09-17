"""Turn archived windows into a decision-point dataset with no look-ahead.

For every window, every ``step_s`` seconds of its life, the row holds what the
bot would have known at that second -- spot history, vol, TWAP state, the
market's own price, the analytic fair value -- and the label the window
eventually resolved to. The per-asset estimators are fed the stitched series
in time order, so the first windows of each asset carry a cold vol estimate,
exactly as a freshly started live bot would.
"""
from __future__ import annotations

from collections import defaultdict

import numpy as np
import pandas as pd

from ..features.engine import FEATURE_NAMES, FeatureEngine
from ..pricing.twap import TwapState, twap_fair_value
from ..pricing.vol import TwoScaleVol
from .archive import ArchivedWindow, TICK, marketable_ask

WINDOW_S = 300.0


def build_dataset(
    windows: list[ArchivedWindow],
    step_s: int = 5,
    info_lag_s: float = 0.55,
    first_left: float = 295.0,
    last_left: float = 5.0,
) -> pd.DataFrame:
    by: dict[str, list[ArchivedWindow]] = defaultdict(list)
    for w in windows:
        by[w.asset].append(w)
    rows: list[dict] = []
    for asset, ws in by.items():
        vol, tw, fe = TwoScaleVol(), TwapState(), FeatureEngine()
        for w in sorted(ws, key=lambda x: x.open_ts):
            g = w.grid
            y = 1.0 if w.up_won else 0.0
            for i in range(len(g.t)):
                if np.isnan(g.spot[i]):
                    continue
                vol.update(g.spot[i], g.t[i])
                tw.update(g.spot[i], g.t[i])
                fe.update_spot(asset, g.spot[i], g.t[i])
                if not np.isnan(g.up[i]):
                    fe.update_market(w.slug, g.up[i], g.t[i])
                left = g.left[i]
                if left <= 0 or left > WINDOW_S or not vol.ready or not tw.ready:
                    continue
                if left > first_left or left < last_left or int(round(left)) % step_s != 0:
                    continue
                fv = twap_fair_value(state=tw, strike=w.strike, sigma_per_sec=vol.sigma_per_sec,
                                     now_ms=g.t[i], close_ts_ms=w.close_ts, info_lag_s=info_lag_s)
                up_bid = None if np.isnan(g.ub[i]) else float(g.ub[i])
                up_ask = None if np.isnan(g.ua[i]) else float(g.ua[i])
                if not g.has_touch[i] and not np.isnan(g.up[i]):
                    # no touch recorded: 1-tick market around the price (see archive.py)
                    up_ask = marketable_ask(g, i, "UP")
                    up_bid = None if up_ask is None else round(up_ask - TICK, 2)
                feats = fe.features(
                    asset=asset, slug=w.slug, strike=w.strike, spot=float(g.spot[i]),
                    now_ms=float(g.t[i]), seconds_left=float(left), window_s=WINDOW_S,
                    up_price=None if np.isnan(g.up[i]) else float(g.up[i]),
                    up_bid=up_bid, up_ask=up_ask, sigma_per_sec=vol.sigma_per_sec,
                    model_p_up=fv.p_up, model_z=fv.z,
                )
                rows.append({
                    "slug": w.slug, "asset": asset, "open_ts": w.open_ts, "t": float(g.t[i]),
                    "i": i, "y": y, "has_touch": bool(g.has_touch[i]),
                    "up_ask": np.nan if up_ask is None else up_ask,
                    "up_bid": np.nan if up_bid is None else up_bid,
                    "down_ask": marketable_ask(g, i, "DOWN") if True else np.nan,
                    "down_bid": np.nan if np.isnan(g.db[i]) else float(g.db[i]),
                    "mdl_unc": fv.uncertainty,
                    **feats,
                })
            fe.forget_market(w.slug)
    df = pd.DataFrame(rows)
    if len(df):
        df["down_ask"] = df["down_ask"].astype(float)
        df = df.sort_values(["open_ts", "asset", "t"]).reset_index(drop=True)
    return df


NUMERIC_FEATURES: tuple[str, ...] = tuple(f for f in FEATURE_NAMES)
