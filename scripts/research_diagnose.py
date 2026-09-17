"""Is the replay edge real? Clustered statistics and the strike-confusion test.

    python scripts/research_diagnose.py

1. PnL is re-aggregated per 300s EPOCH (all assets move together) and the
   mean/standard error/sign test are computed over epochs, not fills.
2. Hypothesis: the crowd prices the window against the OPENING SPOT while the
   venue settles against the trailing 60s TWAP. If so, early market prices
   should track a naive spot-strike model better than the TWAP model, and the
   mispricing should be largest when price trended into the open.
"""
from __future__ import annotations

import math
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from troll_poly_bot.analytics.scorecard import scorecard                         # noqa: E402
from troll_poly_bot.backtest.archive import load_archives                       # noqa: E402
from troll_poly_bot.backtest.dataset import build_dataset                       # noqa: E402
from troll_poly_bot.models.calibration import brier                             # noqa: E402
from troll_poly_bot.pricing.digital import StudentTPricer                       # noqa: E402
from troll_poly_bot.pricing.twap import twap_variance                            # noqa: E402

PR = StudentTPricer(4.0)


def clustered(trades: pd.DataFrame, label: str) -> None:
    f = trades[trades["status"] == "filled"]
    if not len(f):
        print(f"{label}: no fills")
        return
    per_epoch = f.groupby("epoch")["pnl"].sum()
    n = len(per_epoch)
    mean, se = per_epoch.mean(), per_epoch.std(ddof=1) / math.sqrt(n) if n > 1 else float("nan")
    rng = np.random.default_rng(0)
    boots = [per_epoch.sample(n, replace=True, random_state=int(rng.integers(1 << 30))).sum() for _ in range(2000)]
    lo, hi = np.percentile(boots, [2.5, 97.5])
    pos = int((per_epoch > 0).sum())
    print(f"{label}: fills={len(f)} epochs={n} total=${per_epoch.sum():+.2f}  "
          f"per-epoch mean ${mean:+.2f} se ${se:.2f} (t={mean / se if se else float('nan'):.2f})  "
          f"bootstrap 95% of total [{lo:+.1f}, {hi:+.1f}]  epochs>0: {pos}/{n}")


def main() -> None:
    ws = load_archives()
    by_slug = {w.slug: w for w in ws}
    new = pd.read_csv("data/research/new_trades.csv")
    old = pd.read_csv("data/research/old_trades.csv")
    print("EPOCH-CLUSTERED RESULTS (a 300s epoch is one bet, whatever the asset count)")
    clustered(old, "OLD as shipped, real fees")
    clustered(new, "NEW blend50 thr 0.10")
    slugs = sorted(by_slug, key=lambda s: by_slug[s].open_ts)
    half = set(slugs[len(slugs) // 2:])
    clustered(new[new["slug"].isin(half)], "NEW test half only")
    sc = scorecard(old)
    print(f"\nOLD headline: sent={sc['sent']} filled={sc['trades']} win={sc['win_rate']:.2f} fill={sc['avg_fill']:.3f} "
          f"model={sc['avg_model_p']:.3f} realised_edge={sc['realised_edge']:+.3f} net=${sc['net_pnl']:+.2f} "
          f"fees=${sc['fees']:.2f} PF={sc['profit_factor']:.2f} dd=${sc['max_dd']:.2f}")

    # ------------------------------------------------ strike confusion test
    print("\nSTRIKE-CONFUSION TEST")
    df = build_dataset(ws, step_s=5, first_left=295.0, last_left=5.0)
    rows = []
    for w in ws:
        g = w.grid
        i0 = int(np.argmin(np.abs(g.left - 300.0)))
        if np.isnan(g.spot[i0]):
            continue
        s_open = float(g.spot[i0])
        rows.append({"slug": w.slug, "s_open": s_open, "d_open_bps": math.log(s_open / w.strike) * 1e4})
    meta = pd.DataFrame(rows).set_index("slug")
    df = df.join(meta, on="slug")
    df = df[df["s_open"].notna()].copy()
    # naive model: same vol and TWAP variance, but the strike is the opening spot
    df["p_naive"] = [
        PR.cdf(math.log(sp / so) / math.sqrt(max(twap_variance(lf + 0.55, sg * 1e-4), 1e-18)))
        for sp, so, lf, sg in zip(df["t"].map(lambda _: 0) + 0, df["s_open"], df["left"], df["sigma_bps"])
    ] if False else [
        PR.cdf(math.log(sp / so) / math.sqrt(max(twap_variance(lf + 0.55, sg * 1e-4), 1e-18)))
        for sp, so, lf, sg in zip(df["spot_px"] if "spot_px" in df else np.exp(df["dist_bps"] / 1e4) * 0 + 1, df["s_open"], df["left"], df["sigma_bps"])
    ] if False else None
    # spot at row time is not stored in the dataset; reconstruct from dist_bps and strike
    strike = df["slug"].map(lambda s: by_slug[s].strike)
    spot = strike * np.exp(df["dist_bps"] / 1e4)
    df["p_naive"] = [
        PR.cdf(math.log(sp / so) / math.sqrt(max(twap_variance(lf + 0.55, sg * 1e-4), 1e-18)))
        for sp, so, lf, sg in zip(spot, df["s_open"], df["left"], df["sigma_bps"])
    ]
    df["y"] = df["y"].astype(float)
    print("which model does the MARKET price track? corr(market, model) by seconds since open")
    print(f"{'since open':>12}{'n':>6}{'corr(mkt,twap)':>16}{'corr(mkt,naive)':>17}{'brier mkt':>11}{'brier twap':>12}{'brier naive':>13}")
    for lo, hi in ((0, 15), (15, 30), (30, 60), (60, 120), (120, 180), (180, 240), (240, 295)):
        sub = df[(300 - df["left"] >= lo) & (300 - df["left"] < hi)]
        if len(sub) < 50:
            continue
        c1 = np.corrcoef(sub["mkt_p"], sub["mdl_p"])[0, 1]
        c2 = np.corrcoef(sub["mkt_p"], sub["p_naive"])[0, 1]
        print(f"{f'{lo}-{hi}s':>12}{len(sub):>6}{c1:>16.3f}{c2:>17.3f}{brier(sub['mkt_p'], sub['y']):>11.4f}"
              f"{brier(sub['mdl_p'], sub['y']):>12.4f}{brier(sub['p_naive'], sub['y']):>13.4f}")

    print("\nwindows bucketed by how far the OPENING SPOT sat from the TWAP strike (pre-open trend)")
    print("  market vs model vs realised P(up), taken 10 s after the open")
    early = df[(df["left"] >= 285) & (df["left"] <= 295)].groupby("slug").first()
    early["d"] = meta.loc[early.index, "d_open_bps"]
    for lo, hi in ((-99, -8), (-8, -3), (-3, 3), (3, 8), (8, 99)):
        sub = early[(early["d"] >= lo) & (early["d"] < hi)]
        if len(sub) < 3:
            continue
        print(f"  d in [{lo:>3},{hi:>3}) bps  n={len(sub):>3}  market={sub['mkt_p'].mean():.3f}  "
              f"twap model={sub['mdl_p'].mean():.3f}  naive={sub['p_naive'].mean():.3f}  realised={sub['y'].mean():.3f}")

    print("\nthe 240-300s fills of the NEW policy, with the pre-open trend of each window")
    f = new[(new["status"] == "filled") & (new["bucket"] == "240-300s")].copy()
    f["d_open_bps"] = f["slug"].map(meta["d_open_bps"])
    print(f[["slug", "left", "side", "p_model", "p_market", "fill", "won", "pnl", "d_open_bps"]]
          .sort_values("slug").to_string(index=False))

    print("\nmodel minus market by seconds since open (all rows): does the disagreement shrink as the crowd learns?")
    for lo, hi in ((0, 15), (15, 30), (30, 60), (60, 120), (120, 240), (240, 295)):
        sub = df[(300 - df["left"] >= lo) & (300 - df["left"] < hi)]
        d = sub["mdl_p"] - sub["mkt_p"]
        print(f"  {lo:>3}-{hi:<3}s  n={len(sub):>5}  mean|model-mkt|={d.abs().mean():.3f}  "
              f"realised-mkt where model>mkt+5c: {(sub['y'] - sub['mkt_p'])[d > 0.05].mean():+.3f} (n={(d > 0.05).sum()})  "
              f"where model<mkt-5c: {(sub['y'] - sub['mkt_p'])[d < -0.05].mean():+.3f} (n={(d < -0.05).sum()})")


if __name__ == "__main__":
    main()
