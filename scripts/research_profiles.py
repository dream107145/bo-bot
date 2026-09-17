"""Asset profiles, market calibration, lead-lag and edge decay from the archives.

    python scripts/research_profiles.py [--assets BTC,ETH]

Everything here is descriptive and causal: at grid second s only samples at or
before s are used. Outcomes are graded from the closing book, not from the
bot's own proxy (see backtest/archive.py).
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from troll_poly_bot.backtest.archive import (          # noqa: E402
    TIME_BUCKETS, ArchivedWindow, load_archives, summary, time_bucket,
)
from troll_poly_bot.pricing.twap import TwapState, twap_fair_value      # noqa: E402
from troll_poly_bot.pricing.vol import TwoScaleVol                      # noqa: E402

INFO_LAG_S = 0.55     # measured spot p50 on this machine; the archive spot is that stale


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2)) if len(p) else float("nan")


def logloss(p: np.ndarray, y: np.ndarray) -> float:
    q = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(q) + (1 - y) * np.log(1 - q))) if len(p) else float("nan")


# ----------------------------------------------------------------- profiles

def asset_profiles(windows: list[ArchivedWindow]) -> dict:
    """Per-asset vol at several horizons, variance ratios, spreads, one-sidedness."""
    out = {}
    by: dict[str, list[ArchivedWindow]] = defaultdict(list)
    for w in windows:
        by[w.asset].append(w)
    for asset, ws in sorted(by.items()):
        # stitch the spot series across contiguous windows (they abut)
        seg = []
        for w in ws:
            g = w.grid
            m = (g.left <= 300.0) & (g.left > 0.0) & ~np.isnan(g.spot)
            seg.append(np.column_stack([g.t[m], g.spot[m]]))
        s = np.concatenate(seg)
        s = s[np.argsort(s[:, 0])]
        _, keep = np.unique(s[:, 0], return_index=True)
        s = s[keep]
        t, px = s[:, 0], np.log(s[:, 1])
        prof = {"windows": len(ws)}
        for h in (1, 5, 10, 30, 60, 300):
            # returns over h seconds where the series is contiguous
            j = np.searchsorted(t, t + h * 1000.0)
            ok = (j < len(t))
            ok[ok] &= np.abs(t[j[ok]] - (t[ok] + h * 1000.0)) < 1.0
            r = px[j[ok]] - px[ok]
            prof[f"sigma_{h}s_bps"] = float(np.sqrt(np.mean(r ** 2)) * 1e4)
            prof[f"abs_move_{h}s_p50_bps"] = float(np.median(np.abs(r)) * 1e4)
            if h == 300:
                prof["kurtosis_300s"] = float(np.mean(r ** 4) / np.mean(r ** 2) ** 2)
        # iid scaling would give sigma_60 = sigma_1 * sqrt(60)
        prof["variance_ratio_60s_vs_1s"] = float(
            (prof["sigma_60s_bps"] / (prof["sigma_1s_bps"] * math.sqrt(60))) ** 2)
        prof["variance_ratio_300s_vs_1s"] = float(
            (prof["sigma_300s_bps"] / (prof["sigma_1s_bps"] * math.sqrt(300))) ** 2)
        # market microstructure by time bucket
        spread, onesided, up_open = defaultdict(list), defaultdict(list), []
        for w in ws:
            g = w.grid
            for i in range(len(g.t)):
                if not g.has_touch[i] or g.left[i] <= 0 or g.left[i] > 300:
                    continue
                b = time_bucket(g.left[i])
                two_sided = not (np.isnan(g.ua[i]) or np.isnan(g.ub[i]))
                onesided[b].append(0.0 if two_sided else 1.0)
                if two_sided:
                    spread[b].append(g.ua[i] - g.ub[i])
            i0 = np.argmin(np.abs(g.left - 295.0))
            if not np.isnan(g.up[i0]):
                up_open.append(g.up[i0])
        prof["spread_by_bucket"] = {b: round(float(np.mean(v)), 4) for b, v in spread.items() if v}
        prof["one_sided_frac_by_bucket"] = {b: round(float(np.mean(v)), 3) for b, v in onesided.items() if v}
        prof["up_price_at_open_mean"] = float(np.mean(up_open)) if up_open else float("nan")
        prof["up_price_at_open_sd"] = float(np.std(up_open)) if up_open else float("nan")
        prof["strike_vs_open_spot_bps_p50"] = float(np.median([
            abs(math.log(w.grid.spot[np.argmin(np.abs(w.grid.left - 300.0))] / w.strike)) * 1e4
            for w in ws if not np.isnan(w.grid.spot[np.argmin(np.abs(w.grid.left - 300.0))])]))
        out[asset] = prof
    return out


# -------------------------------------------------------------- calibration

def market_calibration(windows: list[ArchivedWindow]) -> dict:
    """Is the market's own price a calibrated forecast, by time-to-expiry?"""
    rows = defaultdict(lambda: ([], []))
    per_asset = defaultdict(lambda: defaultdict(lambda: ([], [])))
    for w in windows:
        g, y = w.grid, 1.0 if w.up_won else 0.0
        for hi, lo in TIME_BUCKETS:
            # first sample inside the bucket, causal
            m = np.where((g.left <= hi) & (g.left > lo) & ~np.isnan(g.up))[0]
            if len(m) == 0:
                continue
            p = g.up[m[0]]
            b = f"{int(lo)}-{int(hi)}s"
            rows[b][0].append(p); rows[b][1].append(y)
            per_asset[w.asset][b][0].append(p); per_asset[w.asset][b][1].append(y)
    out = {"all": {}, "by_asset": {}}
    for b, (p, y) in rows.items():
        p, y = np.array(p), np.array(y)
        out["all"][b] = {"n": int(len(p)), "brier": round(brier(p, y), 4), "logloss": round(logloss(p, y), 4),
                         "mean_p": round(float(p.mean()), 3), "realised": round(float(y.mean()), 3),
                         "brier_coinflip": 0.25}
    for a, bs in per_asset.items():
        out["by_asset"][a] = {b: {"n": int(len(p)), "brier": round(brier(np.array(p), np.array(y)), 4)}
                              for b, (p, y) in bs.items()}
    # reliability table over all buckets except the final 10s (which is ~decided)
    P, Y = [], []
    for b, (p, y) in rows.items():
        if b != "0-10s":
            P += p; Y += y
    P, Y = np.array(P), np.array(Y)
    edges = [0, 0.1, 0.2, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 0.8, 0.9, 1.01]
    rel = []
    for lo, hi in zip(edges, edges[1:]):
        m = (P >= lo) & (P < hi)
        if m.sum():
            rel.append({"bin": f"{lo:.2f}-{min(hi, 1.0):.2f}", "n": int(m.sum()),
                        "mean_p": round(float(P[m].mean()), 3), "realised": round(float(Y[m].mean()), 3)})
    out["reliability_excl_final_10s"] = rel
    return out


# --------------------------------------------------------- model vs market

def model_series(windows: list[ArchivedWindow]) -> dict[str, np.ndarray]:
    """Analytic TWAP fair value at every grid second, causal, per window.

    Vol comes from a TwoScaleVol fed the stitched per-asset spot series in
    time order, exactly as the live bot warms it. Returns slug -> p_up array.
    """
    by: dict[str, list[ArchivedWindow]] = defaultdict(list)
    for w in windows:
        by[w.asset].append(w)
    fair: dict[str, np.ndarray] = {}
    for asset, ws in by.items():
        vol, tw = TwoScaleVol(), TwapState()
        for w in ws:
            g = w.grid
            f = np.full(len(g.t), np.nan)
            for i in range(len(g.t)):
                if np.isnan(g.spot[i]):
                    continue
                vol.update(g.spot[i], g.t[i])
                tw.update(g.spot[i], g.t[i])
                if g.left[i] <= 0 or g.left[i] > 300 or not vol.ready or not tw.ready:
                    continue
                fv = twap_fair_value(state=tw, strike=w.strike, sigma_per_sec=vol.sigma_per_sec,
                                     now_ms=g.t[i], close_ts_ms=w.close_ts, info_lag_s=INFO_LAG_S)
                f[i] = fv.p_up
            fair[w.slug] = f
    return fair


def model_vs_market(windows: list[ArchivedWindow], fair: dict[str, np.ndarray]) -> dict:
    out = {}
    for hi, lo in TIME_BUCKETS:
        b = f"{int(lo)}-{int(hi)}s"
        pm, pf, y, diff = [], [], [], []
        for w in windows:
            g = w.grid
            f = fair.get(w.slug)
            if f is None:
                continue
            m = np.where((g.left <= hi) & (g.left > lo) & ~np.isnan(g.up) & ~np.isnan(f))[0]
            if len(m) == 0:
                continue
            i = m[0]
            pm.append(g.up[i]); pf.append(f[i]); y.append(1.0 if w.up_won else 0.0)
            diff += list(f[m] - g.up[m])
        pm, pf, y, diff = map(np.array, (pm, pf, y, diff))
        if len(y) == 0:
            continue
        out[b] = {
            "n": int(len(y)),
            "brier_market": round(brier(pm, y), 4), "brier_model": round(brier(pf, y), 4),
            "brier_blend50": round(brier(0.5 * pm + 0.5 * pf, y), 4),
            "logloss_market": round(logloss(pm, y), 4), "logloss_model": round(logloss(pf, y), 4),
            "bias_model_minus_market": round(float(diff.mean()), 4),
            "mean_abs_diff": round(float(np.abs(diff).mean()), 4),
            "frac_abs_diff_gt_5c": round(float((np.abs(diff) > 0.05).mean()), 3),
        }
    return out


# ----------------------------------------------------------- lead / lag

def lead_lag(windows: list[ArchivedWindow], fair: dict[str, np.ndarray]) -> dict:
    """corr(1s spot return at t, change in Up price over [t, t+k]) and edge decay."""
    lags = list(range(-3, 11))
    acc = {k: ([], []) for k in lags}
    decay = defaultdict(list)          # horizon -> |fair - mid| after, given > 5c now
    decay_signed = defaultdict(list)   # horizon -> (mid_after - mid_now) * sign(fair - mid)
    for w in windows:
        g = w.grid
        f = fair.get(w.slug)
        ok = (g.left > 15) & (g.left <= 300) & ~np.isnan(g.spot) & ~np.isnan(g.up)
        if f is not None:
            ok &= ~np.isnan(f)
        idx = np.where(ok)[0]
        if len(idx) < 30:
            continue
        r = np.diff(np.log(g.spot))          # r[i] = return from i to i+1
        for i in idx[:-12]:
            if i + 1 >= len(g.up) or np.isnan(r[i]):
                continue
            for k in lags:
                j0, j1 = (i + 1 + k, i + 1) if k < 0 else (i + 1, i + 1 + k)
                if j0 < 0 or j1 >= len(g.up) or k == 0:
                    continue
                d = g.up[j1] - g.up[j0]
                if not np.isnan(d):
                    acc[k][0].append(r[i]); acc[k][1].append(d)
            if f is not None and abs(f[i] - g.up[i]) > 0.05:
                sgn = 1.0 if f[i] > g.up[i] else -1.0
                for h in (1, 2, 5, 10):
                    if i + h < len(g.up) and not np.isnan(g.up[i + h]) and not np.isnan(f[i + h]):
                        decay[h].append(abs(f[i + h] - g.up[i + h]))
                        decay_signed[h].append(sgn * (g.up[i + h] - g.up[i]))
                decay[0].append(abs(f[i] - g.up[i]))
    corr = {}
    for k in lags:
        x, yv = np.array(acc[k][0]), np.array(acc[k][1])
        if len(x) > 100 and x.std() > 0 and yv.std() > 0:
            corr[k] = round(float(np.corrcoef(x, yv)[0, 1]), 4)
    return {
        "corr_spot_return_vs_up_change_by_lag_s": corr,
        "edge_decay_abs_given_gt_5c": {h: round(float(np.mean(v)), 4) for h, v in sorted(decay.items())},
        "market_moves_toward_model_after_s": {
            h: round(float(np.mean(v)), 4) for h, v in sorted(decay_signed.items())},
        "n_mispricing_events": len(decay.get(0, [])),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assets", default="")
    ap.add_argument("--out", default="data/research/profiles.json")
    args = ap.parse_args()
    assets = tuple(a.strip().upper() for a in args.assets.split(",") if a.strip()) or None
    ws = load_archives(assets=assets, require_outcome=False)
    print("ARCHIVES (before dropping undecidable outcomes)")
    print(summary(ws))
    ws = [w for w in ws if w.outcome is not None]
    print(f"\nusable windows: {len(ws)}  "
          f"(disagreements recorded-vs-bell: {sum(w.outcome_source == 'bell' and w.outcome != w.outcome_recorded for w in ws)})\n")

    prof = asset_profiles(ws)
    print("ASSET PROFILES (from the Binance proxy spot the bot held)")
    hdr = f"{'asset':6}{'n':>4}{'s1s':>7}{'s5s':>7}{'s60s':>8}{'s300s':>8}{'VR60':>6}{'VR300':>7}{'kurt':>6}{'|mv300|':>8}{'up@open':>8}{'K-S0 bps':>9}"
    print(hdr)
    for a, p in prof.items():
        print(f"{a:6}{p['windows']:>4}{p['sigma_1s_bps']:>7.2f}{p['sigma_5s_bps']:>7.2f}{p['sigma_60s_bps']:>8.1f}"
              f"{p['sigma_300s_bps']:>8.1f}{p['variance_ratio_60s_vs_1s']:>6.2f}{p['variance_ratio_300s_vs_1s']:>7.2f}"
              f"{p['kurtosis_300s']:>6.1f}{p['abs_move_300s_p50_bps']:>8.1f}{p['up_price_at_open_mean']:>8.3f}"
              f"{p['strike_vs_open_spot_bps_p50']:>9.1f}")
    print("  sigma in bps per horizon (not per second); VR = variance ratio vs iid sqrt(t) scaling")
    print("\n  spread (Up ask - Up bid) and one-sided fraction by time bucket:")
    for a, p in prof.items():
        print(f"  {a:5} spread {p['spread_by_bucket']}")
        print(f"  {a:5} 1-side {p['one_sided_frac_by_bucket']}")

    cal = market_calibration(ws)
    print("\nMARKET CALIBRATION (the venue's own Up price as a forecast; coin flip Brier = 0.25)")
    print(f"{'bucket':>10}{'n':>5}{'mean_p':>8}{'realised':>9}{'brier':>8}{'logloss':>9}")
    for b, v in cal["all"].items():
        print(f"{b:>10}{v['n']:>5}{v['mean_p']:>8.3f}{v['realised']:>9.3f}{v['brier']:>8.4f}{v['logloss']:>9.4f}")
    print("  reliability (all buckets except final 10s):")
    for r in cal["reliability_excl_final_10s"]:
        print(f"    {r['bin']:>10} n={r['n']:>4} mean_p={r['mean_p']:.3f} realised={r['realised']:.3f}")

    fair = model_series(ws)
    mvm = model_vs_market(ws, fair)
    print("\nANALYTIC TWAP MODEL vs MARKET (same windows, causal vol, 0.55s info lag)")
    print(f"{'bucket':>10}{'n':>5}{'brier_mkt':>10}{'brier_mdl':>10}{'blend':>7}{'bias':>8}{'|diff|':>8}{'>5c':>6}")
    for b, v in mvm.items():
        print(f"{b:>10}{v['n']:>5}{v['brier_market']:>10.4f}{v['brier_model']:>10.4f}{v['brier_blend50']:>7.4f}"
              f"{v['bias_model_minus_market']:>+8.3f}{v['mean_abs_diff']:>8.3f}{v['frac_abs_diff_gt_5c']:>6.2f}")

    ll = lead_lag(ws, fair)
    print("\nLEAD/LAG: corr(spot 1s return at t, change in Up price over the next k seconds)")
    print("  ", ll["corr_spot_return_vs_up_change_by_lag_s"])
    print(f"EDGE DECAY given |model - market| > 5c at t  (n={ll['n_mispricing_events']})")
    print("   mean |model - market| after h s:", ll["edge_decay_abs_given_gt_5c"])
    print("   market moved toward model by   :", ll["market_moves_toward_model_after_s"])

    pathlib.Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    pathlib.Path(args.out).write_text(json.dumps({
        "profiles": prof, "market_calibration": cal, "model_vs_market": mvm, "lead_lag": ll,
        "n_windows": len(ws),
    }, indent=1, default=float), encoding="utf-8")
    print(f"\nwritten {args.out}")


if __name__ == "__main__":
    main()
