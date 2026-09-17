"""Execution-aware replay: OLD strategy vs NEW candidates on the archives.

    python scripts/research_backtest.py [--step 1]

Fills land 1 s after the decision against the recorded touch (one-sided book
= no fill), fill-or-kill one tick past the seen ask, real 7% taker fee curve.
The analytic and blend50 models have no fitted parameters, so every window is
out of sample for them; the ONLY thing chosen from data is the edge threshold,
and it is chosen on the first half of the windows (validation) and reported
separately on the second half (test).
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from troll_poly_bot.analytics.scorecard import fmt, scorecard, scorecard_by      # noqa: E402
from troll_poly_bot.backtest.archive import load_archives                       # noqa: E402
from troll_poly_bot.backtest.dataset import build_dataset                       # noqa: E402
from troll_poly_bot.backtest.replay import ReplayConfig, new_policy, old_policy, replay  # noqa: E402
from troll_poly_bot.signals.costs import FeeSchedule                             # noqa: E402

FEE = FeeSchedule()
BUCKETS = ["240-300s", "180-240s", "120-180s", "60-120s", "30-60s", "10-30s", "0-10s"]


def short(label: str, sc: dict) -> str:
    if sc.get("trades", 0) == 0:
        return f"{label:<34} sent={sc.get('sent', 0):>4}  no fills"
    return (f"{label:<34} sent={sc['sent']:>4} filled={sc['trades']:>4} win={sc['win_rate']:.2f} "
            f"fill={sc['avg_fill']:.3f} model={sc['avg_model_p']:.3f} claimed={sc['avg_net_edge']:+.3f} "
            f"realised={sc['realised_edge']:+.3f} pnl/sh={sc['pnl_per_share']:+.4f}"
            f"(se {sc['se_pnl_per_share']:.4f}) net=${sc['net_pnl']:+.2f} fees=${sc['fees']:.2f} "
            f"PF={sc['profit_factor']:.2f} dd=${sc['max_dd']:.2f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=1)
    ap.add_argument("--out", default="data/research/backtest.json")
    args = ap.parse_args()

    ws = load_archives()
    df = build_dataset(ws, step_s=args.step)
    df["p_blend50"] = 0.5 * df["mdl_p"] + 0.5 * df["mkt_p"]
    df["p_blend70"] = 0.7 * df["mdl_p"] + 0.3 * df["mkt_p"]
    print(f"windows {len(ws)}  decision rows {len(df)}  (every {args.step}s)")
    slugs = df.drop_duplicates("slug").sort_values("open_ts")["slug"].tolist()
    half = len(slugs) // 2
    val_slugs, test_slugs = set(slugs[:half]), set(slugs[half:])
    val, test = df[df["slug"].isin(val_slugs)], df[df["slug"].isin(test_slugs)]
    print(f"validation windows {len(val_slugs)}  test windows {len(test_slugs)}\n")
    cfg = ReplayConfig(fee=FEE)
    results = {}

    # ---------------------------------------------------------------- OLD
    print("OLD STRATEGY (as shipped; PnL charged the REAL fee curve)")
    old_all = replay(ws, df, old_policy(), cfg)
    results["old_all"] = scorecard(old_all)
    print(short("old  all windows", results["old_all"]))
    print(short("old  validation half", scorecard(replay(ws, val, old_policy(), cfg))))
    print(short("old  test half", scorecard(replay(ws, test, old_policy(), cfg))))
    if len(old_all[old_all["status"] == "filled"]):
        print(fmt(scorecard_by(old_all, "asset"), "asset"))
        print(fmt(scorecard_by(old_all, "bucket", BUCKETS), "bucket"))

    # ------------------------------------------------- NEW: threshold sweep
    print("\nNEW: model vs ask with the real fee curve, two-sided book required, one trade per window")
    print("threshold sweep on the VALIDATION half only:")
    sweep = {}
    for p_col in ("mdl_p", "p_blend50", "p_blend70"):
        for thr in (0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.07, 0.10):
            sc = scorecard(replay(ws, val, new_policy(p_col, thr, FEE), cfg))
            sweep[(p_col, thr)] = sc
            print(short(f"  {p_col:<9} thr={thr:.2f}", sc))
    # pick by pnl per share with a minimum number of fills, on validation
    cands = [(k, v) for k, v in sweep.items() if v.get("trades", 0) >= 20]
    best_key = max(cands, key=lambda kv: kv[1]["pnl_per_share"])[0] if cands else ("mdl_p", 0.03)
    print(f"\nchosen on validation (>=20 fills, best pnl/share): model={best_key[0]} threshold={best_key[1]:.2f}")

    print("\nTEST half with the chosen setting (never used for selection):")
    new_test = replay(ws, test, new_policy(best_key[0], best_key[1], FEE), cfg)
    results["new_test"] = scorecard(new_test)
    print(short("new  test half", results["new_test"]))
    new_all = replay(ws, df, new_policy(best_key[0], best_key[1], FEE), cfg)
    results["new_all"] = scorecard(new_all)
    print(short("new  all windows (val+test)", results["new_all"]))

    print("\nrobustness: the SAME setting on the test half across nearby thresholds and models")
    for p_col in ("mdl_p", "p_blend50", "p_blend70"):
        for thr in (0.02, 0.03, 0.04, 0.05):
            print(short(f"  {p_col:<9} thr={thr:.2f} TEST", scorecard(replay(ws, test, new_policy(p_col, thr, FEE), cfg))))

    print("\nrobustness: gates on the test half (chosen model/threshold)")
    for label, kw in (("require two-sided OFF", {"require_two_sided": False}),
                      ("uncertainty gate 1.0x", {"unc_mult": 1.0}),
                      ("price band 0.10-0.90", {"band": (0.10, 0.90)}),
                      ("price band 0.20-0.80", {"band": (0.20, 0.80)}),
                      ("window 295-120s only", {"window": (295.0, 120.0)}),
                      ("window 120-5s only", {"window": (120.0, 5.0)}),
                      ("max spread 0.02", {"max_spread": 0.02})):
        print(short(f"  {label}", scorecard(replay(ws, test, new_policy(best_key[0], best_key[1], FEE, **kw), cfg))))

    print("\nrobustness: cross width / landing delay on the test half")
    for ct in (0, 1, 2):
        for land in (1, 2, 3):
            c2 = ReplayConfig(fee=FEE, cross_ticks=ct, land_after_s=land)
            print(short(f"  cross={ct} land={land}s", scorecard(replay(ws, test, new_policy(best_key[0], best_key[1], FEE), c2))))

    f = new_all[new_all["status"] == "filled"]
    if len(f):
        print("\nNEW (all windows) BY ASSET")
        print(fmt(scorecard_by(new_all, "asset"), "asset"))
        print("\nNEW (all windows) BY TIME REMAINING")
        print(fmt(scorecard_by(new_all, "bucket", BUCKETS), "bucket"))
        print("\nNEW (all windows) BY REGIME")
        for key in ("regime_vol", "regime_trend", "regime_liq", "regime_extreme"):
            print(fmt(scorecard_by(new_all, key), key))
        f = f.copy()
        f["edge_bucket"] = pd.cut(f["net_edge"], [0, 0.03, 0.05, 0.08, 0.12, 1.0]).astype(str)
        f["price_bucket"] = pd.cut(f["fill"], [0, 0.3, 0.5, 0.7, 0.85, 1.0]).astype(str)
        f["p_bucket"] = pd.cut(f["p_model"], [0, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 1.01]).astype(str)
        print("\nNEW BY CLAIMED NET EDGE")
        print(fmt(scorecard_by(f, "edge_bucket"), "edge_bucket"))
        print("\nNEW BY ENTRY PRICE")
        print(fmt(scorecard_by(f, "price_bucket"), "price_bucket"))
        print("\nNEW BY MODEL PROBABILITY")
        print(fmt(scorecard_by(f, "p_bucket"), "p_bucket"))
        print("\nEDGE ANALYSIS (per fill, all windows): predicted vs market vs cost vs outcome")
        cols = ["slug", "left", "side", "p_model", "p_market", "ask_seen", "fill", "fee", "net_edge", "won", "pnl"]
        print(f.sort_values("t")[cols].head(25).to_string(index=False))
        # correlation: same-epoch trades across assets
        by_epoch = f.groupby("epoch").agg(n=("asset", "nunique"), same_dir=("side", lambda s: s.nunique() == 1),
                                          pnl=("pnl", "sum"))
        multi = by_epoch[by_epoch["n"] >= 2]
        print(f"\nCORRELATION: epochs with trades in >=2 assets: {len(multi)} of {len(by_epoch)}; "
              f"same direction in {int(multi['same_dir'].sum())} of them")
        per_asset_pnl = f.pivot_table(index="epoch", columns="asset", values="pnl", aggfunc="sum")
        if per_asset_pnl.shape[1] >= 2:
            print("pairwise correlation of per-epoch PnL across assets (NaN = never both traded):")
            print(per_asset_pnl.corr().round(2).to_string())
        slip = f["slippage"]
        print(f"\nSLIPPAGE on fills: mean {slip.mean():+.4f}/share, p90 {slip.quantile(0.9):+.3f}, "
              f"FOK rejects {int((new_all['status'] == 'fok_rejected').sum())}, "
              f"no-liquidity {int((new_all['status'] == 'no_liquidity').sum())} of {len(new_all)} sent")
        new_all.to_csv("data/research/new_trades.csv", index=False)
        old_all.to_csv("data/research/old_trades.csv", index=False)

    results["chosen"] = {"model": best_key[0], "threshold": best_key[1]}
    results["sweep_validation"] = {f"{k[0]}@{k[1]:.2f}": v for k, v in sweep.items()}
    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps(results, indent=1, default=float), encoding="utf-8")
    print(f"\nwritten {args.out}")


if __name__ == "__main__":
    main()
