"""Strategy scorecards from a replay (or live) trade table."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

COLS = ["trades", "sent", "fill_rate", "win_rate", "avg_fill", "avg_model_p", "avg_mkt_p",
        "avg_net_edge", "realised_edge", "gross_pnl", "fees", "slippage", "net_pnl",
        "pnl_per_share", "profit_factor", "max_dd"]


def equity_stats(pnl: np.ndarray) -> tuple[float, float]:
    """(profit factor, max drawdown) on the trade sequence."""
    wins, losses = pnl[pnl > 0].sum(), -pnl[pnl < 0].sum()
    pf = wins / losses if losses > 0 else (math.inf if wins > 0 else math.nan)
    eq = np.cumsum(pnl)
    peak = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    dd = float((eq - peak).min()) if len(eq) else 0.0
    return float(pf), dd


def scorecard(trades: pd.DataFrame) -> dict:
    sent = len(trades)
    f = trades[trades["status"] == "filled"] if sent else trades
    n = len(f)
    if n == 0:
        return {"trades": 0, "sent": sent, "fill_rate": 0.0}
    pnl = f["pnl"].to_numpy()
    pf, dd = equity_stats(pnl)
    return {
        "trades": n, "sent": sent, "fill_rate": n / sent if sent else 0.0,
        "win_rate": float(f["won"].mean()),
        "avg_fill": float(f["fill"].mean()),
        "avg_model_p": float(f["p_model"].mean()),
        "avg_mkt_p": float(f["p_market"].mean()),
        "avg_net_edge": float(f["net_edge"].mean()),
        # what actually happened minus what was paid, per share, before fees
        "realised_edge": float((f["won"] - f["fill"]).mean()),
        "gross_pnl": float((f["shares"] * f["gross_pnl_per_share"]).sum()),
        "fees": float(f["fees"].sum()),
        "slippage": float(f["slippage_cost"].sum()),
        "net_pnl": float(pnl.sum()),
        "pnl_per_share": float(f["pnl_per_share"].mean()),
        "profit_factor": pf, "max_dd": dd,
        "avg_trade": float(pnl.mean()),
        "se_pnl_per_share": float(f["pnl_per_share"].std(ddof=1) / math.sqrt(n)) if n > 1 else math.nan,
    }


def scorecard_by(trades: pd.DataFrame, key: str, order: list | None = None) -> pd.DataFrame:
    rows = []
    keys = order or sorted(trades[key].dropna().unique().tolist())
    for k in keys:
        sub = trades[trades[key] == k]
        if not len(sub):
            continue
        rows.append({key: k, **scorecard(sub)})
    return pd.DataFrame(rows)


def fmt(df: pd.DataFrame, key: str) -> str:
    if not len(df):
        return "  (no trades)"
    cols = [key, "trades", "sent", "win_rate", "avg_fill", "avg_model_p", "avg_net_edge",
            "realised_edge", "net_pnl", "fees", "pnl_per_share", "profit_factor", "max_dd"]
    cols = [c for c in cols if c in df.columns]
    out = df[cols].copy()
    for c in cols[1:]:
        if c in ("trades", "sent"):
            out[c] = out[c].astype(int)
        else:
            out[c] = out[c].map(lambda v: f"{v:.3f}" if isinstance(v, float) and math.isfinite(v) else str(v))
    return out.to_string(index=False)
