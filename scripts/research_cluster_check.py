"""Model-vs-market Brier difference, bootstrapped over 300s EPOCHS (not windows).

All assets in the same epoch share the same crypto move, so a window-level
bootstrap overstates the evidence. Also prints the sample size a 2-sigma
detection of a given per-share edge would need.
"""
from __future__ import annotations

import math
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from troll_poly_bot.models.calibration import brier   # noqa: E402

oos = pd.read_csv("data/research/oos_predictions.csv")
oos["epoch"] = (oos["open_ts"] // 1000).astype(int)
groups = [g for _, g in oos.groupby("epoch")]
print(f"OOS rows {len(oos)}, windows {oos['slug'].nunique()}, epochs {len(groups)}")
rng = np.random.default_rng(0)
for spec in ("analytic", "blend50", "blend_lr", "full_gbm"):
    diffs = []
    for _ in range(1000):
        pick = rng.choice(len(groups), len(groups), replace=True)
        s = pd.concat([groups[i] for i in pick])
        diffs.append(brier(s[f"p_{spec}"], s["y"]) - brier(s["p_market"], s["y"]))
    d = np.array(diffs)
    print(f"{spec:<10} brier - market: {d.mean():+.4f}  95% [{np.percentile(d, 2.5):+.4f}, {np.percentile(d, 97.5):+.4f}]"
          f"  {'*' if np.percentile(d, 97.5) < 0 else ''}")

print("\nSAMPLE SIZE: independent bets needed to show a per-share edge at 2 sigma")
print("(binary payout; sd per share ~ sqrt(p(1-p)) at the fill price)")
for p in (0.4, 0.6):
    sd = math.sqrt(p * (1 - p))
    for edge in (0.02, 0.03, 0.05, 0.08):
        n = (2 * sd / edge) ** 2
        print(f"  fill {p:.1f}  edge {edge:.2f}/share  ->  n = {n:,.0f} independent epochs traded")
print("  the archive holds ~71 epochs over 25 hours; a strategy trading 30% of epochs sees ~20 bets/day")
