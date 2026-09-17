"""Execution-aware replay of a decision policy over archived windows.

The policy sees one dataset row (what the bot knew at that second) and
returns a side to buy, or None. The engine then does what the venue would:

* the order lands ``land_after_s`` later (the measured ~1 s reaction lag: the
  spot in the row is already ~0.55 s old, and the order needs ~0.45 s to
  reach the book) and is matched against the touch **as recorded at that
  second** -- a one-sided book means nobody was offering and the order dies;
* fill-or-kill at ``ask_at_decision + cross_ticks * tick``: if the ask has
  moved further than that in flight, rejected;
* the taker fee of the venue's schedule is charged on the fill price;
* one position per window -- re-firing the same signal is not a new trade.

Nothing here uses a later sample than the one the order lands on.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from ..signals.costs import FeeSchedule
from ..signals.regime import classify_regime
from .archive import TICK, ArchivedWindow, marketable_ask, time_bucket

Decision = tuple[str, float, float, float] | None   # (side, p_side, ask_seen, net_edge)
Policy = Callable[[pd.Series], Decision]


@dataclass(slots=True)
class ReplayConfig:
    land_after_s: int = 1
    cross_ticks: int = 1
    shares: float = 10.0
    fee: FeeSchedule = FeeSchedule()
    one_per_window: bool = True


def _ask_at(w: ArchivedWindow, i: int, side: str) -> float | None:
    g = w.grid
    if i >= len(g.t):
        return None
    if g.has_touch[i]:
        a = g.ua[i] if side == "UP" else g.da[i]
        return None if np.isnan(a) else float(a)
    return marketable_ask(g, i, side)


def replay(windows: list[ArchivedWindow], df: pd.DataFrame, policy: Policy,
           cfg: ReplayConfig = ReplayConfig()) -> pd.DataFrame:
    by_slug = {w.slug: w for w in windows}
    trades: list[dict] = []
    for slug, rows in df.groupby("slug", sort=False):
        w = by_slug.get(slug)
        if w is None or w.up_won is None:
            continue
        rows = rows.sort_values("t")
        for _, r in rows.iterrows():
            d = policy(r)
            if d is None:
                continue
            side, p_side, ask_seen, net_edge = d
            i_land = int(r["i"]) + cfg.land_after_s
            ask_land = _ask_at(w, i_land, side)
            limit = round(ask_seen + cfg.cross_ticks * TICK, 4)
            status = "filled"
            if ask_land is None:
                status = "no_liquidity"
            elif ask_land > limit + 1e-9:
                status = "fok_rejected"
            feats = {k: r[k] for k in r.index if isinstance(r[k], (float, int, np.floating, np.integer))}
            reg = classify_regime(feats)
            rec = {
                "slug": slug, "asset": r["asset"], "epoch": int(w.open_ts // 1000), "t": r["t"],
                "left": r["left"], "bucket": time_bucket(r["left"]), "side": side,
                "p_model": p_side, "p_market": (r["mkt_p"] if side == "UP" else 1.0 - r["mkt_p"]),
                "ask_seen": ask_seen, "net_edge": net_edge, "status": status,
                "regime_vol": reg.vol, "regime_trend": reg.trend, "regime_liq": reg.liquidity,
                "regime_extreme": reg.extreme, "sigma_bps": r.get("sigma_bps", np.nan),
                "spread": r.get("spread", np.nan), "shares": cfg.shares,
            }
            if status == "filled":
                won = (side == "UP") == w.up_won
                fee = cfg.fee.taker_fee_per_share(ask_land)
                rec.update({
                    "fill": ask_land, "slippage": ask_land - ask_seen, "fee": fee, "won": float(won),
                    "gross_pnl_per_share": (1.0 if won else 0.0) - ask_land,
                    "pnl_per_share": (1.0 if won else 0.0) - ask_land - fee,
                    "pnl": cfg.shares * ((1.0 if won else 0.0) - ask_land - fee),
                    "fees": cfg.shares * fee, "slippage_cost": cfg.shares * (ask_land - ask_seen),
                })
            trades.append(rec)
            if cfg.one_per_window and status == "filled":
                break
            if cfg.one_per_window and status != "filled":
                # a rejected order does not stop the policy from trying again later
                continue
    return pd.DataFrame(trades)


# ----------------------------------------------------------------- policies

def old_policy(min_edge: float = 0.05, shrink: float = 0.5, band: tuple[float, float] = (0.03, 0.97),
               fair_band: tuple[float, float] = (0.15, 0.85), window: tuple[float, float] = (230.0, 3.0),
               assumed_fee_rate: float = 0.02, edge_sigmas: float = 1.5) -> Policy:
    """The strategy as shipped: analytic fair shrunk to the mid, 2% fee assumed."""
    def pol(r: pd.Series) -> Decision:
        left = r["left"]
        if not (window[1] <= left <= window[0]):
            return None
        p_up, mid_up = r["mdl_p"], r["mkt_p"]
        if fair_band[0] < p_up < fair_band[1]:
            return None
        best = None
        for side in ("UP", "DOWN"):
            ask = r["up_ask"] if side == "UP" else r["down_ask"]
            if ask is None or np.isnan(ask) or not (band[0] <= ask <= band[1]):
                continue
            fair = p_up if side == "UP" else 1.0 - p_up
            mid = mid_up if side == "UP" else 1.0 - mid_up
            fair_used = (1 - shrink) * fair + shrink * mid
            net = fair_used - ask - assumed_fee_rate * min(ask, 1 - ask)
            if net < min_edge or net < edge_sigmas * max(r["mdl_unc"], 1e-6):
                continue
            if best is None or net > best[3]:
                best = (side, fair_used, float(ask), net)
        return best
    return pol


def new_policy(p_col: str, min_edge: float, fee: FeeSchedule, slippage: float = 0.0,
               unc_mult: float = 0.0, band: tuple[float, float] = (0.03, 0.97),
               window: tuple[float, float] = (295.0, 5.0), require_two_sided: bool = True,
               max_spread: float = 0.05, conf_gate_mult: float = 0.0) -> Policy:
    """Model probability vs the ask, charged the real fee curve, plus gates.

    ``unc_mult`` charges the model's error band against the edge (the engine's
    ``uncertainty_charge``); ``conf_gate_mult`` is the engine's LOW_CONFIDENCE
    gate: the edge after fee and slippage must exceed that many bands.
    """
    def pol(r: pd.Series) -> Decision:
        left = r["left"]
        if not (window[1] <= left <= window[0]):
            return None
        p_up = r[p_col]
        if p_up is None or np.isnan(p_up):
            return None
        if require_two_sided and r.get("one_sided", 0.0) >= 1.0:
            return None
        sp = r.get("spread", np.nan)
        if not np.isnan(sp) and sp > max_spread:
            return None
        best = None
        for side in ("UP", "DOWN"):
            ask = r["up_ask"] if side == "UP" else r["down_ask"]
            if ask is None or np.isnan(ask) or not (band[0] <= ask <= band[1]):
                continue
            p = p_up if side == "UP" else 1.0 - p_up
            unc = max(r["mdl_unc"], 0.0)
            after_costs = p - ask - fee.taker_fee_per_share(ask) - slippage
            net = after_costs - unc_mult * unc
            if net < min_edge or after_costs < conf_gate_mult * unc:
                continue
            if best is None or net > best[3]:
                best = (side, p, float(ask), net)
        return best
    return pol
