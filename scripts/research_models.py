"""Walk-forward comparison of probability models on the archived windows.

    python scripts/research_models.py [--step 5] [--blocks 5]

Every number printed is OUT OF SAMPLE: windows are ordered by open time, the
first third is the initial training set, and each later block is predicted by
a model fitted only on the windows before it. Differences against the market
price are bootstrapped over WINDOWS (rows inside a window share one label).

Writes data/research/oos_predictions.csv for the execution replay.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import warnings

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from troll_poly_bot.backtest.archive import load_archives, time_bucket          # noqa: E402
from troll_poly_bot.backtest.dataset import build_dataset                        # noqa: E402
from troll_poly_bot.features.engine import FEATURE_NAMES                          # noqa: E402
from troll_poly_bot.models.calibration import brier, ece, logloss, reliability_table  # noqa: E402
from troll_poly_bot.models.logistic import LogisticModel                          # noqa: E402

warnings.filterwarnings("ignore")

FULL = [f for f in FEATURE_NAMES if f not in ("mkt_p", "mdl_p", "left")]   # logits + everything else
BLEND = ["mkt_logit", "mdl_logit"]
BLEND_T = ["mkt_logit", "mdl_logit", "mkt_x_lr", "mdl_x_lr"]
ASSETS_ALL = ("BTC", "ETH", "SOL", "XRP", "DOGE")


def add_derived(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["mkt_x_lr"] = df["mkt_logit"] * df["left_ratio"]
    df["mdl_x_lr"] = df["mdl_logit"] * df["left_ratio"]
    for a in ASSETS_ALL:
        df[f"is_{a}"] = (df["asset"] == a).astype(float)
        df[f"mdl_x_{a}"] = df["mdl_logit"] * df[f"is_{a}"]
    return df


def fit_predict(spec: str, train: pd.DataFrame, test: pd.DataFrame) -> np.ndarray:
    """Return OOS predictions for ``test`` from a model fitted on ``train``."""
    if spec == "market":
        return test["mkt_p"].to_numpy()
    if spec == "analytic":
        return test["mdl_p"].to_numpy()
    if spec == "blend50":
        return 0.5 * test["mkt_p"].to_numpy() + 0.5 * test["mdl_p"].to_numpy()
    if spec == "platt_market":
        m = LogisticModel.fit(train[["mkt_logit"]].to_numpy(), train["y"].to_numpy(), ["mkt_logit"], l2=1.0)
        return m.predict_proba(test[["mkt_logit"]].to_numpy())
    if spec == "blend_lr":
        m = LogisticModel.fit(train[BLEND].to_numpy(), train["y"].to_numpy(), BLEND, l2=1.0)
        return m.predict_proba(test[BLEND].to_numpy())
    if spec == "blend_time_lr":
        m = LogisticModel.fit(train[BLEND_T].to_numpy(), train["y"].to_numpy(), BLEND_T, l2=1.0)
        return m.predict_proba(test[BLEND_T].to_numpy())
    if spec == "full_lr":
        m = LogisticModel.fit(train[FULL].to_numpy(), train["y"].to_numpy(), FULL, l2=5.0)
        return m.predict_proba(test[FULL].to_numpy())
    if spec == "blend_per_asset":          # approach B: one model per asset
        out = np.full(len(test), np.nan)
        for a in test["asset"].unique():
            tr, mask = train[train["asset"] == a], (test["asset"] == a).to_numpy()
            if len(tr) < 200:
                out[mask] = test.loc[mask, "mkt_p"]
                continue
            m = LogisticModel.fit(tr[BLEND].to_numpy(), tr["y"].to_numpy(), BLEND, l2=1.0)
            out[mask] = m.predict_proba(test.loc[mask, BLEND].to_numpy())
        return out
    if spec == "blend_hybrid":             # approach C: global + per-asset adjustment
        cols = BLEND + [f"is_{a}" for a in ASSETS_ALL] + [f"mdl_x_{a}" for a in ASSETS_ALL]
        m = LogisticModel.fit(train[cols].to_numpy(), train["y"].to_numpy(), cols, l2=5.0)
        return m.predict_proba(test[cols].to_numpy())
    if spec == "full_gbm":
        try:
            from sklearn.ensemble import HistGradientBoostingClassifier
        except ImportError:
            return np.full(len(test), np.nan)
        g = HistGradientBoostingClassifier(max_depth=3, max_iter=150, learning_rate=0.05,
                                           l2_regularization=1.0, min_samples_leaf=200, random_state=0)
        g.fit(train[FULL].to_numpy(), train["y"].to_numpy())
        return g.predict_proba(test[FULL].to_numpy())[:, 1]
    raise ValueError(spec)


SPECS = ["market", "analytic", "blend50", "platt_market", "blend_lr", "blend_time_lr",
         "full_lr", "full_gbm", "blend_per_asset", "blend_hybrid"]


def walk_forward(df: pd.DataFrame, n_blocks: int, initial_frac: float) -> pd.DataFrame:
    slugs = df.drop_duplicates("slug").sort_values("open_ts")["slug"].tolist()
    rank = {s: i for i, s in enumerate(slugs)}
    df = df.assign(rank=df["slug"].map(rank))
    n = len(slugs)
    first = int(n * initial_frac)
    block = max(1, (n - first) // n_blocks)
    preds = {s: np.full(len(df), np.nan) for s in SPECS}
    fold = np.full(len(df), -1)
    for b in range(n_blocks):
        lo = first + b * block
        hi = n if b == n_blocks - 1 else lo + block
        tr_mask = (df["rank"] < lo).to_numpy()
        te_mask = ((df["rank"] >= lo) & (df["rank"] < hi)).to_numpy()
        train, test = df[tr_mask], df[te_mask]
        if len(test) == 0:
            continue
        fold[te_mask] = b
        for s in SPECS:
            try:
                preds[s][te_mask] = fit_predict(s, train, test)
            except Exception as exc:                          # noqa: BLE001
                print(f"  [{s}] fold {b} failed: {exc}")
    out = df.copy()
    out["fold"] = fold
    for s in SPECS:
        out[f"p_{s}"] = preds[s]
    return out[out["fold"] >= 0].reset_index(drop=True)


def window_bootstrap_diff(df: pd.DataFrame, a: str, b: str, metric, n_boot: int = 400, seed: int = 0):
    """metric(a) - metric(b), resampling windows with replacement."""
    rng = np.random.default_rng(seed)
    groups = [g for _, g in df.groupby("slug")]
    idx = np.arange(len(groups))
    diffs = []
    for _ in range(n_boot):
        pick = rng.choice(idx, len(idx), replace=True)
        s = pd.concat([groups[i] for i in pick])
        diffs.append(metric(s[f"p_{a}"].to_numpy(), s["y"].to_numpy())
                     - metric(s[f"p_{b}"].to_numpy(), s["y"].to_numpy()))
    d = np.array(diffs)
    return float(d.mean()), float(np.percentile(d, 2.5)), float(np.percentile(d, 97.5))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--step", type=int, default=5)
    ap.add_argument("--blocks", type=int, default=5)
    ap.add_argument("--initial", type=float, default=0.34)
    ap.add_argument("--out", default="data/research/oos_predictions.csv")
    args = ap.parse_args()

    ws = load_archives()
    print(f"windows: {len(ws)}")
    df = add_derived(build_dataset(ws, step_s=args.step))
    print(f"decision rows: {len(df)}  (every {args.step}s, 295s -> 5s left)")
    print(f"rows with a model: {int(df['mdl_p'].notna().sum())}, with a market price: {int(df['mkt_p'].notna().sum())}")
    df = df[df["mdl_p"].notna() & df["mkt_p"].notna()].reset_index(drop=True)

    oos = walk_forward(df, args.blocks, args.initial)
    print(f"\nOUT-OF-SAMPLE rows: {len(oos)} over {oos['slug'].nunique()} windows, {args.blocks} walk-forward blocks")
    print(f"{'model':<18}{'brier':>8}{'logloss':>9}{'ece':>7}   brier-vs-market [95% CI over windows]")
    for s in SPECS:
        p = oos[f"p_{s}"].to_numpy()
        if np.isnan(p).all():
            print(f"{s:<18}   (unavailable)")
            continue
        m, lo, hi = window_bootstrap_diff(oos, s, "market", brier) if s != "market" else (0.0, 0.0, 0.0)
        flag = " *" if hi < 0 else ""
        print(f"{s:<18}{brier(p, oos['y']):>8.4f}{logloss(p, oos['y']):>9.4f}{ece(p, oos['y']):>7.3f}"
              f"   {m:+.4f} [{lo:+.4f}, {hi:+.4f}]{flag}")
    print("  * = the 95% window-bootstrap interval excludes zero (better than the market price)")

    # by time bucket for the main candidates
    oos["bucket"] = oos["left"].map(time_bucket)
    order = [f"{int(lo)}-{int(hi)}s" for hi, lo in
             ((300, 240), (240, 180), (180, 120), (120, 60), (60, 30), (30, 10), (10, 0))]
    cands = ["market", "analytic", "blend_lr", "blend_time_lr", "full_lr", "full_gbm", "blend_hybrid"]
    print(f"\nBRIER BY TIME BUCKET (OOS)\n{'bucket':>10}{'n_win':>6}" + "".join(f"{c:>15}" for c in cands))
    for b in order:
        sub = oos[oos["bucket"] == b]
        if not len(sub):
            continue
        line = f"{b:>10}{sub['slug'].nunique():>6}"
        for c in cands:
            p = sub[f"p_{c}"].to_numpy()
            line += f"{brier(p, sub['y']):>15.4f}" if not np.isnan(p).all() else f"{'-':>15}"
        print(line)

    print(f"\nBRIER BY ASSET (OOS)\n{'asset':>10}{'n_win':>6}" + "".join(f"{c:>15}" for c in cands))
    for a in sorted(oos["asset"].unique()):
        sub = oos[oos["asset"] == a]
        line = f"{a:>10}{sub['slug'].nunique():>6}"
        for c in cands:
            p = sub[f"p_{c}"].to_numpy()
            line += f"{brier(p, sub['y']):>15.4f}" if not np.isnan(p).all() else f"{'-':>15}"
        print(line)

    # blend coefficients on the full set, for the report
    m = LogisticModel.fit(df[BLEND].to_numpy(), df["y"].to_numpy(), BLEND, l2=1.0)
    print(f"\nblend_lr fitted on ALL rows (for reference, not for scoring): {m.describe()}")
    mt = LogisticModel.fit(df[BLEND_T].to_numpy(), df["y"].to_numpy(), BLEND_T, l2=1.0)
    print(f"blend_time_lr on ALL rows: {mt.describe()}")

    # calibration of the leading learned model, OOS, in the strategy buckets
    best = min((s for s in SPECS if s not in ("market",) and not np.isnan(oos[f'p_{s}']).all()),
               key=lambda s: logloss(oos[f"p_{s}"].to_numpy(), oos["y"].to_numpy()))
    print(f"\nRELIABILITY (OOS, folded to the favourite side) -- best by logloss: {best}")
    for r in reliability_table(oos[f"p_{best}"].to_numpy(), oos["y"].to_numpy()):
        print(f"   {r['bucket']:>10} n={r['n']:>5} predicted={r['predicted']:.3f} realised={r['realised']:.3f} gap={r['gap']:+.3f}")
    print("RELIABILITY of the MARKET price, same rows:")
    for r in reliability_table(oos["p_market"].to_numpy(), oos["y"].to_numpy()):
        print(f"   {r['bucket']:>10} n={r['n']:>5} predicted={r['predicted']:.3f} realised={r['realised']:.3f} gap={r['gap']:+.3f}")

    # does disagreement with the market carry information? realised - market, by model-minus-market
    print(f"\nWHEN {best} DISAGREES WITH THE MARKET (OOS): realised minus market price")
    d = oos[f"p_{best}"] - oos["p_market"]
    for lo, hi in ((-1, -0.10), (-0.10, -0.05), (-0.05, -0.02), (-0.02, 0.02), (0.02, 0.05), (0.05, 0.10), (0.10, 1)):
        m_ = (d >= lo) & (d < hi)
        if m_.sum() < 20:
            continue
        sub = oos[m_]
        print(f"   {lo:+.2f}..{hi:+.2f}  n={m_.sum():>5} windows={sub['slug'].nunique():>4}  "
              f"model-mkt={d[m_].mean():+.3f}  realised-mkt={(sub['y'] - sub['p_market']).mean():+.3f}")

    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    oos.to_csv(args.out, index=False)
    summary = {s: {"brier": brier(oos[f"p_{s}"].to_numpy(), oos["y"].to_numpy()),
                   "logloss": logloss(oos[f"p_{s}"].to_numpy(), oos["y"].to_numpy())}
               for s in SPECS if not np.isnan(oos[f"p_{s}"]).all()}
    pathlib.Path("data/research/models_summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(f"\nwritten {args.out}")


if __name__ == "__main__":
    main()
