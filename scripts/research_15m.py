"""Parameter study for the 15-minute windows.

    python scripts/research_15m.py [--spread 0.02] [--out data/research/15m/report.json]

Reads what scripts/fetch_15m_history.py pulled and answers, per asset and
pooled, the questions each 15m parameter depends on:

  vol       does sqrt(t) carry the 1-minute vol out to 15 minutes (variance
            ratio), how fat are 15m tails (Student-t df), how large is a 15m
            move in bps, and how early is a window "decided" -- the fraction
            of the final move realised by minute k
  market    calibration (Brier) of the UP mid by time-left bucket, how often
            the book is saturated (outside 0.05..0.95) by bucket, volume per
            window by asset
  model     the bot's OWN pricer (Student-t(4), TWAP variance) driven by
            candle-aligned spot vs the market mid: Brier by bucket for the
            model, the market and blends; then the paper rule "buy the side
            the blend prices >= e above the ask, hold to settlement" for
            several e, VALIDATION (first half of the days) and TEST (second
            half) reported separately, t-stat clustered by 900s epoch
  exits     from each simulated entry, the best bid seen afterwards and the
            worst: do the 5m take-profit / stop-loss deltas still make sense

Everything is causal: at minute k only candles at or before k are used and the
market point is the last one at or before k.

Strike is the mean of the 1-minute candle ending at the open (the venue
settles on a 60s TWAP) -- a proxy, and one reason every number here is a
guide to the ORDER of a parameter, not its third decimal.
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from troll_poly_bot.pricing.digital import DEFAULT_PRICER          # noqa: E402
from troll_poly_bot.pricing.twap import twap_variance             # noqa: E402
from troll_poly_bot.signals.costs import FeeSchedule              # noqa: E402

D = pathlib.Path("data/research/15m")
W = 900
FEE = FeeSchedule()
BUCKETS = [(900, 720), (720, 540), (540, 360), (360, 180), (180, 60), (60, 0)]


def bucket(left: float) -> str:
    for hi, lo in BUCKETS:
        if lo < left <= hi:
            return f"{lo}-{hi}"
    return "closed"


def tstat_by_epoch(df: pd.DataFrame, col: str = "pnl") -> tuple[float, float, int]:
    """Mean per-epoch PnL, t over epochs, number of epochs."""
    if df.empty:
        return 0.0, 0.0, 0
    per = df.groupby("epoch")[col].sum()
    n = len(per)
    if n < 2:
        return float(per.mean()), 0.0, n
    return float(per.mean()), float(per.mean() / (per.std(ddof=1) / math.sqrt(n))), n


# ───────────────────────────────── vol ───────────────────────────────────

def fit_t_df(x: np.ndarray) -> float:
    from scipy import stats
    x = x[np.isfinite(x)]
    if len(x) < 200:
        return float("nan")
    df, _, _ = stats.t.fit(x / x.std(), floc=0.0)
    return float(df)


def vol_section(spot: dict[str, pd.DataFrame]) -> dict:
    out = {}
    for a, df in spot.items():
        c = df.set_index("t")["close"]
        lr = np.log(c).diff().dropna()
        r1 = lr.values
        v1 = np.var(r1)
        row = {"n_min": int(len(r1)), "sigma_1m_bps": float(np.sqrt(v1) * 1e4)}
        for h in (5, 15, 30):
            rh = np.log(c).diff(h).dropna().values
            row[f"vr_{h}"] = float(np.var(rh) / (h * v1))          # 1.0 = iid
            row[f"kurt_{h}"] = float(pd.Series(rh).kurt())
        row["vr_15_over_5"] = row["vr_15"] / row["vr_5"]
        r15 = np.log(c).diff(15).dropna().values
        row["kurt_1"] = float(pd.Series(r1).kurt())
        row["t_df_15m"] = fit_t_df(r15)
        row["t_df_1m"] = fit_t_df(r1)
        q = np.quantile(np.abs(r15) * 1e4, [0.5, 0.9, 0.99])
        row["abs_r15_bps_p50_p90_p99"] = [float(x) for x in q]
        # how early is a window decided: fraction of the final 15m move realised
        # by minute k, on windows aligned to 900s boundaries
        t = c.index.values
        aligned = t[(t % W) == 0]
        fr = {k: [] for k in (3, 5, 8, 10, 12, 14)}
        for open_t in aligned:
            try:
                o = c.at[open_t - 60]                          # candle ending at open
                f = c.at[open_t + 14 * 60]                     # last full candle
            except KeyError:
                continue
            final = math.log(f / o)
            if abs(final) < 1e-6:
                continue
            for k in fr:
                try:
                    fr[k].append(math.log(c.at[open_t + (k - 1) * 60] / o) / final)
                except KeyError:
                    pass
        row["move_realised_by_min"] = {str(k): float(np.median(v)) for k, v in fr.items() if v}
        row["hour_sigma_bps"] = {
            str(h): float(np.sqrt(np.var(lr[(pd.to_datetime(lr.index, unit="s").hour == h)])) * 1e4)
            for h in range(0, 24, 3)}
        out[a] = row
    return out


# ──────────────────────────────── market ─────────────────────────────────

def market_section(mk: pd.DataFrame, pts: pd.DataFrame) -> dict:
    pts = pts.merge(mk[["slug", "outcome_up"]], on="slug")
    pts["left"] = pts["epoch"] + W - pts["t"]
    inw = pts[(pts.left > 0) & (pts.left <= W)].copy()
    inw["bucket"] = inw.left.map(bucket)
    inw["sat"] = (inw.p < 0.05) | (inw.p > 0.95)
    inw["sq"] = (inw.p - inw.outcome_up) ** 2
    by = inw.groupby("bucket").agg(n=("p", "size"), mean_abs_dev=("p", lambda s: float((s - 0.5).abs().mean())),
                                    saturated=("sat", "mean"), brier_market=("sq", "mean"))
    by["brier_coin"] = 0.25
    order = [f"{lo}-{hi}" for hi, lo in BUCKETS]
    by = by.reindex([b for b in order if b in by.index])
    first = inw.sort_values("t").groupby("slug").first()
    vol = mk.groupby("asset")["volume"].quantile([0.1, 0.5, 0.9]).unstack()
    vol.columns = ["p10", "p50", "p90"]
    return {
        "by_bucket": {k: {c: float(v[c]) for c in by.columns} for k, v in by.iterrows()},
        "first_in_window_mid": {"mean": float(first.p.mean()), "p10": float(first.p.quantile(.1)),
                                "p90": float(first.p.quantile(.9))},
        "volume_by_asset": {a: {c: float(r[c]) for c in vol.columns} for a, r in vol.iterrows()},
        "windows": int(len(mk)), "windows_by_asset": mk.groupby("asset").size().to_dict(),
        "base_rate_up": float(mk.outcome_up.mean()),
    }


# ───────────────────────────── model vs market ───────────────────────────

def ewma_sigma_per_sec(close: pd.Series, halflife_min: float = 120.0) -> pd.Series:
    """Per-second sigma from an EWMA of squared 1-minute log returns. Shifted
    by one so the estimate at minute k uses returns up to k-1 only."""
    lr = np.log(close).diff()
    var = (lr ** 2).ewm(halflife=halflife_min, min_periods=60).mean().shift(1)
    return np.sqrt(var / 60.0)


def build_panel(mk: pd.DataFrame, pts: pd.DataFrame, spot: dict[str, pd.DataFrame],
                vr15: dict[str, float]) -> pd.DataFrame:
    """One row per in-window MARKET observation: the model is handed only the
    last COMPLETED 1-minute candle at that instant.

    The first version did the opposite -- walked candle minutes and took the
    market point at or before each -- which let the model see spot up to 90s
    newer than the book it was scored against. That is not an edge, it is a
    leak, and it produced +0.2/share at t=15. Here the spot the model sees is
    0..60s OLDER than what the market has already priced, so the comparison
    is biased AGAINST the model. An edge that survives that is real; one that
    does not never existed.
    """
    rows = []
    pts = pts.sort_values("t")
    for a, sdf in spot.items():
        c = sdf.set_index("t")["close"]
        ohlc = sdf.set_index("t")[["open", "high", "low", "close"]].mean(axis=1)
        sig = ewma_sigma_per_sec(c)
        m_a = mk[mk.asset == a]
        p_by = {s: g for s, g in pts[pts.asset == a].groupby("slug")}
        for r in m_a.itertuples():
            ep = r.epoch
            if (ep - 60) not in ohlc.index or r.outcome_up is None or pd.isna(r.outcome_up):
                continue
            strike = float(ohlc.at[ep - 60])
            g = p_by.get(r.slug)
            if g is None or g.empty:
                continue
            for t_obs, mid in zip(g.t.values, g.p.values):
                left = ep + W - t_obs
                if not (0 < left <= W):
                    continue
                ct = (int(t_obs) // 60) * 60 - 60           # last candle fully closed by t_obs
                if ct not in c.index:
                    continue
                sg = sig.get(ct, np.nan)
                if not np.isfinite(sg):
                    continue
                s_px = float(c.at[ct]); sg = float(sg)
                lag = t_obs - (ct + 60)                       # how stale the spot is, 0..59s
                var = twap_variance(left + lag, sg, 60.0)     # info_lag: the horizon is longer
                z = math.log(s_px / strike) / math.sqrt(max(var, 1e-18))
                z_vr = z / math.sqrt(vr15.get(a, 1.0))
                rows.append((r.slug, a, ep, int(t_obs), float(left), s_px, strike, sg, float(mid),
                             int(r.outcome_up), DEFAULT_PRICER.cdf(z), DEFAULT_PRICER.cdf(z_vr), float(lag)))
    return pd.DataFrame(rows, columns=["slug", "asset", "epoch", "t", "left", "spot", "strike", "sigma",
                                       "mid", "y", "p_model", "p_model_vr", "spot_lag_s"])


def simulate(panel: pd.DataFrame, p_col: str, blend: float, e: float, spread: float,
             left_min: float, left_max: float) -> pd.DataFrame:
    """First qualifying minute per window; buy that side at mid + spread/2 + one
    tick through; PnL/share = outcome - price - fee, held to settlement."""
    df = panel[(panel.left >= left_min) & (panel.left <= left_max)].copy()
    df["p_used"] = blend * df.mid + (1 - blend) * df[p_col]
    ask_up = np.minimum(df.mid + spread / 2 + 0.01, 0.99)
    ask_dn = np.minimum(1 - df.mid + spread / 2 + 0.01, 0.99)
    fee_up = np.vectorize(FEE.taker_fee_per_share)(ask_up)
    fee_dn = np.vectorize(FEE.taker_fee_per_share)(ask_dn)
    edge_up = df.p_used - ask_up - fee_up
    edge_dn = (1 - df.p_used) - ask_dn - fee_dn
    band_up = (ask_up >= 0.05) & (ask_up <= 0.95)
    band_dn = (ask_dn >= 0.05) & (ask_dn <= 0.95)
    df["side"] = np.where((edge_up >= e) & band_up, "UP", np.where((edge_dn >= e) & band_dn, "DOWN", ""))
    df["price"] = np.where(df.side == "UP", ask_up, ask_dn)
    df["fee"] = np.where(df.side == "UP", fee_up, fee_dn)
    df["edge"] = np.where(df.side == "UP", edge_up, edge_dn)
    tr = df[df.side != ""].sort_values("t").groupby("slug").first().reset_index()
    win = np.where(tr.side == "UP", tr.y == 1, tr.y == 0)
    tr["pnl"] = np.where(win, 1.0, 0.0) - tr.price - tr.fee
    return tr


def exits_section(panel: pd.DataFrame, pts: pd.DataFrame, trades: pd.DataFrame) -> dict:
    """Best and worst bid seen after each entry, and rule variants."""
    if trades.empty:
        return {}
    pts = pts.sort_values("t")
    by = {s: g for s, g in pts.groupby("slug")}
    rec = []
    for tr in trades.itertuples():
        g = by.get(tr.slug)
        if g is None:
            continue
        t_entry = tr.t
        after = g[(g.t > t_entry) & (g.t < tr.epoch + W - 10)]
        if after.empty:
            continue
        p_side = after.p.values if tr.side == "UP" else 1 - after.p.values
        bid = p_side - 0.01
        rec.append((tr.slug, tr.epoch, tr.side, tr.price, tr.fee, tr.pnl, float(bid.max()), float(bid.min())))
    ex = pd.DataFrame(rec, columns=["slug", "epoch", "side", "price", "fee", "hold_pnl", "best_bid", "worst_bid"])
    out = {"n": int(len(ex)),
           "best_bid_minus_entry_p50_p90": [float(x) for x in np.quantile(ex.best_bid - ex.price, [.5, .9])],
           "worst_bid_minus_entry_p10_p50": [float(x) for x in np.quantile(ex.worst_bid - ex.price, [.1, .5])],
           "winners_touching_tp": {}, "losers_touching_sl": {}, "rules": {}}
    won = ex.hold_pnl > 0
    for tp in (0.05, 0.08, 0.12):
        out["winners_touching_tp"][str(tp)] = float(((ex.best_bid - ex.price) >= tp)[won].mean())
    for sl in (0.10, 0.20, 0.30):
        out["losers_touching_sl"][str(sl)] = float(((ex.price - ex.worst_bid) >= sl)[~won].mean())
    fee = np.vectorize(FEE.taker_fee_per_share)
    for tp in (0.0, 0.05, 0.08, 0.12):
        for sl in (0.0, 0.10, 0.20, 0.30):
            hit_tp = (tp > 0) & ((ex.best_bid - ex.price) >= tp)
            hit_sl = (sl > 0) & ((ex.price - ex.worst_bid) >= sl)
            # a stop that is hit is assumed hit first when both are: the
            # conservative reading (the adverse path usually comes first for a
            # position that later recovers only to be stopped -- unknowable at
            # 1-minute fidelity, so take the pessimistic branch)
            exit_px = np.where(hit_sl, ex.price - sl, np.where(hit_tp, ex.price + tp, np.nan))
            pnl = np.where(np.isnan(exit_px), ex.hold_pnl, exit_px - ex.price - ex.fee - fee(np.nan_to_num(exit_px, nan=0.5)))
            per = pd.DataFrame({"epoch": ex.epoch, "pnl": pnl})
            m, t, n = tstat_by_epoch(per)
            out["rules"][f"tp{tp}_sl{sl}"] = {"mean_pnl_per_share": float(np.mean(pnl)), "t_epoch": t,
                                              "exits": int(np.sum(~np.isnan(exit_px)))}
    return out


# ──────────────────────────────────  main ────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spread", type=float, default=0.02, help="assumed full spread on the token book")
    ap.add_argument("--out", default=str(D / "report.json"))
    args = ap.parse_args()

    spot = {p.stem.split("_")[1]: pd.read_parquet(p) for p in sorted(D.glob("spot_*.parquet"))}
    mk = pd.read_parquet(D / "markets.parquet")
    pts = pd.read_parquet(D / "points.parquet")
    mk = mk[mk.resolved & mk.outcome_up.notna()].copy()
    mk["outcome_up"] = mk.outcome_up.astype(int)
    print(f"windows {len(mk)}  points {len(pts)}  assets {sorted(spot)}\n")

    rep: dict = {"spread_assumed": args.spread}

    # 1. vol -----------------------------------------------------------------
    rep["vol"] = vol_section(spot)
    print("== VOL: does sqrt(t) hold from 1m to 15m?  (vr=1 means iid; t_df: Student-t fit)")
    print(f"{'asset':6s} {'s1m bps':>8s} {'vr5':>6s} {'vr15':>6s} {'vr15/5':>7s} {'kurt15':>7s} {'tdf15':>6s} {'|r15| p50/p90/p99 bps':>24s}  realised by min 5/10/12/14")
    for a, r in rep["vol"].items():
        q = r["abs_r15_bps_p50_p90_p99"]; mv = r["move_realised_by_min"]
        print(f"{a:6s} {r['sigma_1m_bps']:8.2f} {r['vr_5']:6.2f} {r['vr_15']:6.2f} {r['vr_15_over_5']:7.2f} "
              f"{r['kurt_15']:7.1f} {r['t_df_15m']:6.1f} {q[0]:7.1f}/{q[1]:6.1f}/{q[2]:6.1f}      "
              f"{mv.get('5',float('nan')):.2f}/{mv.get('10',float('nan')):.2f}/{mv.get('12',float('nan')):.2f}/{mv.get('14',float('nan')):.2f}")

    # 2. market --------------------------------------------------------------
    rep["market"] = market_section(mk, pts)
    print(f"\n== MARKET: {rep['market']['windows']} windows, base rate up {rep['market']['base_rate_up']:.3f}, "
          f"first in-window mid {rep['market']['first_in_window_mid']['mean']:.3f}")
    print(f"{'left (s)':10s} {'n':>7s} {'|mid-.5|':>9s} {'saturated':>10s} {'brier mkt':>10s}")
    for b, r in rep["market"]["by_bucket"].items():
        print(f"{b:10s} {int(r['n']):7d} {r['mean_abs_dev']:9.3f} {r['saturated']:10.1%} {r['brier_market']:10.4f}")
    print("volume/window p10/p50/p90: " + "  ".join(
        f"{a} {v['p10']:.0f}/{v['p50']:.0f}/{v['p90']:.0f}" for a, v in rep["market"]["volume_by_asset"].items()))

    # 3. model vs market -----------------------------------------------------
    vr15 = {a: r["vr_15"] for a, r in rep["vol"].items()}
    panel = build_panel(mk, pts, spot, vr15)
    panel["bucket"] = panel.left.map(bucket)
    days = sorted(set(panel.epoch // 86400))
    split = days[len(days) // 2]
    panel["half"] = np.where(panel.epoch // 86400 < split, "valid", "test")
    print(f"\n== MODEL vs MARKET: {len(panel)} aligned minutes over {panel.slug.nunique()} windows; "
          f"valid = first {len(days)//2} days, test = last {len(days) - len(days)//2}")
    cal = {}
    for b in [f"{lo}-{hi}" for hi, lo in BUCKETS]:
        d = panel[panel.bucket == b]
        if d.empty:
            continue
        row = {"n": int(len(d)), "brier_market": float(((d.mid - d.y) ** 2).mean()),
               "brier_model": float(((d.p_model - d.y) ** 2).mean()),
               "brier_model_vr": float(((d.p_model_vr - d.y) ** 2).mean()),
               "mean_model_minus_mid": float((d.p_model - d.mid).mean()),
               "mean_abs_model_minus_mid": float((d.p_model - d.mid).abs().mean())}
        for bl in (0.3, 0.5, 0.7):
            row[f"brier_blend{bl}"] = float(((bl * d.mid + (1 - bl) * d.p_model - d.y) ** 2).mean())
        cal[b] = row
    rep["calibration"] = cal
    print(f"{'left (s)':10s} {'n':>6s} {'market':>7s} {'model':>7s} {'model·vr':>8s} {'blend.3':>8s} {'blend.5':>8s} {'blend.7':>8s} {'bias':>7s} {'|diff|':>7s}")
    for b, r in cal.items():
        print(f"{b:10s} {r['n']:6d} {r['brier_market']:7.4f} {r['brier_model']:7.4f} {r['brier_model_vr']:8.4f} "
              f"{r['brier_blend0.3']:8.4f} {r['brier_blend0.5']:8.4f} {r['brier_blend0.7']:8.4f} "
              f"{r['mean_model_minus_mid']:+7.3f} {r['mean_abs_model_minus_mid']:7.3f}")

    # the paper rule
    grid = {}
    print(f"\n== PAPER RULE: buy where blend >= ask + fee + e (spread {args.spread}, 1 tick through), hold to settle")
    print(f"{'p_col':10s} {'blend':>5s} {'e':>5s} {'window':>9s} | {'VALID n':>7s} {'pnl/sh':>7s} {'t_ep':>5s} | {'TEST n':>6s} {'pnl/sh':>7s} {'t_ep':>5s}")
    for p_col in ("p_model", "p_model_vr"):
        for bl in (0.5, 0.7):
            for e in (0.02, 0.03, 0.05, 0.08):
                for (lmin, lmax, tag) in ((60, 840, "60-840"), (120, 840, "120-840"), (180, 720, "180-720")):
                    res = {}
                    for half in ("valid", "test"):
                        tr = simulate(panel[panel.half == half], p_col, bl, e, args.spread, lmin, lmax)
                        m, t, n = tstat_by_epoch(tr)
                        res[half] = {"n": int(len(tr)), "pnl_per_share": float(tr.pnl.mean()) if len(tr) else 0.0,
                                     "t_epoch": t, "epochs": n,
                                     "by_asset": {a: float(g.pnl.mean()) for a, g in tr.groupby("asset")} if len(tr) else {}}
                    key = f"{p_col}|blend{bl}|e{e}|{tag}"
                    grid[key] = res
                    v, tt = res["valid"], res["test"]
                    print(f"{p_col:10s} {bl:5.1f} {e:5.2f} {tag:>9s} | {v['n']:7d} {v['pnl_per_share']:+7.3f} {v['t_epoch']:5.1f} | "
                          f"{tt['n']:6d} {tt['pnl_per_share']:+7.3f} {tt['t_epoch']:5.1f}")
    rep["rule_grid"] = grid

    # does the edge depend on how stale the spot the model saw was? The live
    # bot's spot is ~1s old; this study's is 0..59s old. If PnL rises as the
    # lag falls, the handicap is what is hiding the edge; if it does not, the
    # edge was never there.
    lag_tab = {}
    print("\n== PnL by SPOT STALENESS at entry (the live bot sits at the left edge of this table)")
    print(f"{'rule':22s} " + " ".join(f"{'lag '+str(lo)+'-'+str(hi)+'s':>14s}" for lo, hi in ((0,10),(10,20),(20,40),(40,60))))
    for (p_col, bl, e) in (("p_model", 0.5, 0.02), ("p_model", 0.7, 0.03), ("p_model", 0.7, 0.05)):
        cells = []
        row = {}
        for lo, hi in ((0, 10), (10, 20), (20, 40), (40, 60)):
            sub = panel[(panel.spot_lag_s >= lo) & (panel.spot_lag_s < hi)]
            tr = simulate(sub, p_col, bl, e, args.spread, 60, 840)
            m, t, n = tstat_by_epoch(tr)
            row[f"{lo}-{hi}"] = {"n": int(len(tr)), "pnl_per_share": float(tr.pnl.mean()) if len(tr) else 0.0, "t_epoch": t}
            cells.append(f"{len(tr):5d} {tr.pnl.mean() if len(tr) else 0:+.3f} t{t:4.1f}")
        lag_tab[f"{p_col}|blend{bl}|e{e}"] = row
        print(f"{p_col+' b'+str(bl)+' e'+str(e):22s} " + " ".join(f"{c:>14s}" for c in cells))
    rep["rule_by_spot_lag"] = lag_tab

    # the current 5m configuration, verbatim, on the whole month, by asset
    tr_all = simulate(panel, "p_model", 0.5, 0.02, args.spread, 60, 840)
    m, t, n = tstat_by_epoch(tr_all)
    rep["current_5m_rule_all"] = {"n": int(len(tr_all)), "pnl_per_share": float(tr_all.pnl.mean()), "t_epoch": t, "epochs": n,
                                  "by_asset": {a: {"n": int(len(g)), "pnl": float(g.pnl.mean())} for a, g in tr_all.groupby("asset")},
                                  "by_bucket": {b: {"n": int(len(g)), "pnl": float(g.pnl.mean())} for b, g in tr_all.groupby("bucket")}}
    print("\n== the 5m rule as-is (blend .5, e .02, 60..840s) on the whole month, by asset / by entry bucket")
    for a, r in rep["current_5m_rule_all"]["by_asset"].items():
        print(f"  {a:5s} n={r['n']:4d} pnl/sh {r['pnl']:+.3f}")
    for b, r in rep["current_5m_rule_all"]["by_bucket"].items():
        print(f"  entry {b:8s} n={r['n']:4d} pnl/sh {r['pnl']:+.3f}")

    # 4. exits ---------------------------------------------------------------
    rep["exits"] = exits_section(panel, pts, tr_all)
    ex = rep["exits"]
    if ex:
        print(f"\n== EXITS from {ex['n']} entries: best bid-entry p50/p90 {ex['best_bid_minus_entry_p50_p90']}, "
              f"worst bid-entry p10/p50 {ex['worst_bid_minus_entry_p10_p50']}")
        print("  winners touching TP:", ex["winners_touching_tp"], " losers touching SL:", ex["losers_touching_sl"])
        for k, r in ex["rules"].items():
            print(f"  {k:14s} pnl/sh {r['mean_pnl_per_share']:+.4f}  t {r['t_epoch']:5.1f}  exits {r['exits']}")

    pathlib.Path(args.out).write_text(json.dumps(rep, indent=1, default=float))
    print(f"\nreport -> {args.out}")


if __name__ == "__main__":
    sys.exit(main())
