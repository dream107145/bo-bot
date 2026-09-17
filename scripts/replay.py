"""Replay the live strategy over REAL recorded windows, with network delay.

This is the test the synthetic simulator cannot be: the token prices are the
actual Polymarket mids the bot recorded (data/charts/*.json), the outcomes are
the venue's own resolutions, and the delay is this machine's MEASURED latency
rather than a canned profile.

The same three-clock model the live bot uses applies. The strategy is handed a
view of the book that is `md_book` ms stale; an order it sends lands
`submit` ms later and is matched against the book AS IT IS THEN; it learns the
result `ack` ms after that. Everything delay costs lives in that gap.

The pricer is fed the spot as it ARRIVES, not as it truly is. Feeding it the
true price at the true time would let the model see data it never had -- a
flattering bug, and the exact opposite of what this test is for.

Honest limits of this data, stated up front rather than discovered later:
  * n is small (a couple of dozen windows). This validates the machinery and
    shows what delay costs on real prices. It is not a profitability estimate.
  * Archives recorded before the sampling fix stop ~30-60s before close
    (sampling went quiet when one side of the book emptied), so the FINAL
    MINUTE -- where the near-expiry TWAP edge supposedly lives -- is absent
    from them. The 150s -> ~55s slice is real. "Landed after resolution"
    rejections therefore cannot occur on those windows.
  * Archives hold mids, not depth. Books are rebuilt as a 1-tick market around
    the mid (matches the live books, which were 1 tick wide) with an assumed
    DEPTH shares resting -- far more than our <=20-share orders ever ask for.
"""
from __future__ import annotations

import glob
import json
import math
import pathlib
import sys
from dataclasses import replace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from troll_poly_bot.config import BotConfig                                  # noqa: E402
from troll_poly_bot.execution.latency import (                               # noqa: E402
    COLOCATED, HOME_BROADBAND, INSTANT, VPS_NEARBY, LatencyModel,
)
from troll_poly_bot.execution.paper import FeeModel, PaperExchange           # noqa: E402
from troll_poly_bot.feeds.delayed import DelayedFeed                         # noqa: E402
from troll_poly_bot.pricing.twap import TwapState                            # noqa: E402
from troll_poly_bot.pricing.vol import TwoScaleVol                           # noqa: E402
from troll_poly_bot.strategy.taker import TakerStrategy                      # noqa: E402
from troll_poly_bot.types import BookLevel, Market, OrderBook                # noqa: E402

TICK = 0.01
DEPTH = 500.0

# This machine, as measured by the live bot (p50 / p95 ms):
#   spot 603 / 1182   book 494 / 504   order round trip 978 / 1006
# LatencyModel draws base + Gamma(2, jitter/2) + occasional spikes, so the
# mean sits at base + jitter; the fat spot p95 is carried by the spike term.
MEASURED = replace(
    HOME_BROADBAND, name="measured (this machine)",
    md_spot_base=450.0, md_spot_jitter=150.0, spike_prob=0.05, spike_ms=550.0,
    md_book_base=480.0, md_book_jitter=14.0,
    submit_base=480.0, submit_jitter=10.0,
    ack_base=480.0, ack_jitter=10.0,
    cancel_base=480.0, cancel_jitter=10.0,
)
DEGRADED = replace(
    MEASURED, name="measured x1.5",
    md_spot_base=675.0, md_spot_jitter=225.0, spike_ms=825.0,
    md_book_base=720.0, md_book_jitter=21.0,
    submit_base=720.0, submit_jitter=15.0,
    ack_base=720.0, ack_jitter=15.0,
    cancel_base=720.0, cancel_jitter=15.0,
)
PROFILES = [INSTANT, COLOCATED, VPS_NEARBY, MEASURED, DEGRADED]


def load_windows():
    out = []
    for f in sorted(glob.glob("data/charts/*.json")):
        d = json.loads(pathlib.Path(f).read_text(encoding="utf-8"))
        pts = [p for p in d.get("points") or [] if p.get("spot") is not None]
        if not d.get("strike") or not d.get("outcome") or len(pts) < 50:
            continue
        pts.sort(key=lambda p: p["t"])
        out.append((d, pts))
    return out


def book_from_mid(token: str, mid: float, ts: float) -> OrderBook:
    bid = math.floor(mid / TICK + 1e-9) * TICK
    bid = max(TICK, round(bid, 2))
    ask = min(1.0 - TICK, round(bid + TICK, 2))
    return OrderBook(token, bids=[BookLevel(bid, DEPTH)], asks=[BookLevel(ask, DEPTH)], ts=ts)


def book_from_point(token: str, side: str, p: dict, ts: float) -> OrderBook:
    """Rebuild one token book from a recorded point.

    Points recorded after the sampler fix carry the touch (ub/ua/db/da), each
    None when that side was absent -- so a decided window rebuilds as the
    one-sided book the venue actually served, and a taker cannot buy a winner
    nobody was offering. Older points carry only the price; those rebuild as
    a 1-tick two-sided book, which is right until the window is decided and
    flattering after.
    """
    bk, ak = ("ub", "ua") if side == "up" else ("db", "da")
    if bk in p or ak in p:
        bid, ask = p.get(bk), p.get(ak)
        bids = [BookLevel(float(bid), DEPTH)] if bid is not None else []
        asks = [BookLevel(float(ask), DEPTH)] if ask is not None else []
        return OrderBook(token, bids=bids, asks=asks, ts=ts)
    return book_from_mid(token, p[side], ts)


def run(profile, windows, seed=7, cross_ticks=1, market_shrink=None, min_edge=None):
    cfg = BotConfig()
    cfg.starting_balance = 100.0
    s = cfg.strategy
    # identical to what `python -m troll_poly_bot --balance 100` sets
    s.target_win_usdc = 1.0
    s.max_position_usdc, s.max_gross_usdc = 15.0, 45.0
    s.max_shares_per_order, s.daily_loss_limit_usdc = 150.0, 30.0
    s.cross_ticks = cross_ticks
    if market_shrink is not None:
        s.market_shrink = market_shrink
    if min_edge is not None:
        s.min_edge = min_edge

    # Mirror the live bot: its staleness gates widen to 2.5x the measured p95
    # so a slow-but-healthy feed is not rejected as a dead one. Probe a
    # separate model instance so the fill simulation RNG stream is untouched.
    probe = LatencyModel(profile, seed=seed + 1000)
    spot_p95 = sorted(probe.md_spot() for _ in range(2000))[1900]
    book_p95 = sorted(probe.md_book() for _ in range(2000))[1900]
    s.max_spot_age_ms = max(s.max_spot_age_ms, spot_p95 * 2.5)
    s.max_book_age_ms = max(s.max_book_age_ms, book_p95 * 2.5)

    lat = LatencyModel(profile, seed=seed)
    fees = FeeModel(rate=cfg.fees.rate)
    ex = PaperExchange(lat, fees, starting_balance=100.0)
    vol, twap = {}, {}
    strat = TakerStrategy(cfg=s, latency=profile, fees=fees, vol=vol, twap=twap)

    fills, per_window = [], []
    fair_by_order = {}

    for d, pts in windows:
        asset, slug = d["asset"], d["slug"]
        vol.setdefault(asset, TwoScaleVol())
        twap.setdefault(asset, TwapState())
        m = Market(condition_id=slug, asset=asset,
                   yes_token_id=slug + "-UP", no_token_id=slug + "-DOWN",
                   strike=float(d["strike"]), open_ts=float(d["open_ts"]),
                   close_ts=float(d["close_ts"]), tick_size=TICK)
        went_up = d["outcome"] == "UP"
        spot_feed = DelayedFeed(lat.md_spot, maxlen=8192)
        up_feed = DelayedFeed(lat.md_book, maxlen=4096)
        dn_feed = DelayedFeed(lat.md_book, maxlen=4096)
        true = {}
        pending = []               # results whose win/loss is known only at settle
        before = ex.latency_report()["orders_submitted"]
        last_seen_spot = None

        def step(now):
            for res in ex.step(now, lambda t: true.get(t), lambda t: m.close_ts):
                pending.append(res)

        for p in pts:
            now = float(p["t"])
            # truth is published into the delayed feeds...
            spot_feed.publish(float(p["spot"]), now)
            ub = book_from_point(m.yes_token_id, "up", p, now)
            db = book_from_point(m.no_token_id, "down", p, now)
            true[m.yes_token_id], true[m.no_token_id] = ub, db
            up_feed.publish(ub, now)
            dn_feed.publish(db, now)

            # ...and the engine fills against the truth as of arrival time
            step(now)

            # ...while the strategy and its pricer see only what has ARRIVED
            seen_spot, sage = spot_feed.view(now), spot_feed.view_age_ms(now)
            su, sd = up_feed.view(now), dn_feed.view(now)
            if seen_spot is None or su is None or sd is None:
                continue
            if seen_spot != last_seen_spot:
                vol[asset].update(seen_spot, now)
                twap[asset].update(seen_spot, now)
                last_seen_spot = seen_spot
            bage = max(up_feed.view_age_ms(now) or 0.0, dn_feed.view_age_ms(now) or 0.0)
            gross = sum(abs(x.shares) * 0.5 for x in ex.positions.values())
            pos = sum(abs(ex.positions[t].shares) * 0.5
                      for t in (m.yes_token_id, m.no_token_id) if t in ex.positions)
            intent = strat.evaluate(
                market=m, book_up=su, book_down=sd, spot=seen_spot, spot_age_ms=sage,
                book_age_ms=bage, now=now, gross_exposure=gross, position_usdc=pos,
                balance=max(ex.balance, 0.0))
            if intent is not None:
                o = strat.to_order(intent, intent.fair)
                fair_by_order[o.order_id] = intent
                ex.submit(o, now)
                # A zero-latency order must match against the book NOW, not at
                # the next recorded sample ~500ms on. For any real latency the
                # order arrives later than now, so this is a no-op for it.
                step(now)

        # let anything still on the wire land against the last known book
        now = float(pts[-1]["t"])
        for _ in range(40):
            now += 100.0
            step(now)

        for res in pending:
            if res.is_rejected:
                continue
            it = fair_by_order.get(res.order.order_id)
            side_up = res.order.token_id == m.yes_token_id
            won = went_up if side_up else not went_up
            for f in res.fills:
                fills.append({"fair": it.fair if it else float("nan"), "price": f.price,
                              "size": f.size, "slip": f.slippage, "rtt": f.round_trip_ms,
                              "won": won, "slug": slug})

        pnl = ex.settle_market(m.yes_token_id, won=went_up)
        pnl += ex.settle_market(m.no_token_id, won=not went_up)
        sent = ex.latency_report()["orders_submitted"] - before
        per_window.append((slug, d["outcome"], int(sent), pnl, ex.balance))

    return ex, strat, fills, per_window


def _agg(fills):
    """Size-weighted: what we paid, what the model claimed, what happened."""
    n = len(fills)
    if not n:
        return float("nan"), float("nan"), float("nan"), 0
    w = sum(f["size"] for f in fills) or 1.0
    pred = sum(f["fair"] * f["size"] for f in fills if f["fair"] == f["fair"]) / w
    real = sum(f["won"] * f["size"] for f in fills) / w
    paid = sum(f["price"] * f["size"] for f in fills) / w
    return pred, real, paid, n


def _row(label, react, ex, fills):
    r = ex.latency_report()
    pred, real, paid, n = _agg(fills)
    return (f"{label:<24}{react:>7.0f}{ex.balance - 100.0:>+9.2f}{int(r['orders_submitted']):>6}"
            f"{int(r['orders_filled']):>7}{r['fill_rate']:>7.1%}"
            f"{int(r['rejected_fok_unfillable']):>8}"
            f"{r['total_slippage_cost']:>7.2f}{r['total_fees']:>7.2f}"
            f"{paid:>7.3f}{pred:>7.3f}{real:>7.3f}")


HDR = (f"{'profile':<24}{'react':>7}{'pnl':>9}{'sent':>6}{'filled':>7}{'fill%':>7}"
       f"{'FOK-rej':>8}{'slip$':>7}{'fees$':>7}{'paid':>7}{'model':>7}{'real':>7}")


def main():
    windows = load_windows()
    print(f"REAL recorded windows with strike + outcome + spot: {len(windows)}\n")
    if not windows:
        print("nothing to replay")
        return

    print("DELAY SWEEP: same strategy, same real prices, only the network differs")
    print(HDR)
    print("-" * len(HDR))
    gates, detail = [], None
    for prof in PROFILES:
        ex, strat, fills, per_window = run(prof, windows)
        print(_row(prof.name, prof.reaction_lag_ms(), ex, fills))
        gates.append((prof.name, strat.cfg.max_spot_age_ms, strat.cfg.max_book_age_ms))
        if prof is MEASURED:
            detail = (per_window, strat.skips, fills)

    print()
    print("staleness gates in force (spot / book ms), widened to 2.5x p95 as the live bot does:")
    for name, sa, ba in gates:
        print(f"  {name:<26}{sa:>7.0f} / {ba:>5.0f}")

    print()
    print("CROSS-WIDTH SWEEP at measured delay: does crossing wider than 1 tick turn")
    print("FOK rejections into fills, and is it still worth it at the worse price?")
    print(HDR.replace("profile", "cross  ").replace("react", "     "))
    print("-" * len(HDR))
    for ct in (1, 2, 3, 4):
        ex2, _s2, fills2, _pw = run(MEASURED, windows, cross_ticks=ct)
        print(_row(f"{ct} tick{'s' if ct > 1 else ''}", MEASURED.reaction_lag_ms(), ex2, fills2))

    print()
    print("CONSERVATISM SWEEP: does deferring more to the market (higher shrink) or")
    print("demanding more edge turn the loss around, or just trade less? With ~10 fills")
    print("this shows the SHAPE of the loss; it cannot validate a setting.")
    print(HDR.replace("profile", "shrink/edge  prof").replace("react", "     "))
    print("-" * len(HDR))
    for shrink, edge in ((0.5, 0.05), (0.65, 0.05), (0.8, 0.05), (0.5, 0.08), (0.65, 0.08)):
        for prof in (INSTANT, MEASURED):
            ex3, _s3, fills3, _pw3 = run(prof, windows, market_shrink=shrink, min_edge=edge)
            print(_row(f"{shrink:.2f}/{edge:.2f} {prof.name[:8]}", prof.reaction_lag_ms(), ex3, fills3))

    per_window, skips, fills = detail
    print("\nPER WINDOW at measured delay, 1-tick cross  (win target $1, $15/market cap)")
    print(f"{'window':<28}{'outcome':>8}{'sent':>6}{'pnl':>9}{'balance':>9}")
    for slug, out, sent, pnl, bal in per_window:
        print(f"{slug:<28}{out:>8}{sent:>6}{pnl:>+9.2f}{bal:>9.2f}")

    print("\nFILLS at measured delay (what a real fill cost vs the decision-time price):")
    if fills:
        print(f"{'window':<28}{'paid':>7}{'fair':>7}{'slip':>7}{'rtt ms':>8}{'won':>5}")
        for f in fills:
            print(f"{f.get('slug', ''):<28}{f['price']:>7.3f}{f['fair']:>7.3f}"
                  f"{f['slip']:>+7.3f}{f['rtt']:>8.0f}{'Y' if f['won'] else 'n':>5}")
    else:
        print("  (none)")

    tot = sum(skips.values()) or 1
    print("\nWHY IT DID NOT TRADE MORE (measured delay), top gates:")
    for k, v in sorted(skips.items(), key=lambda kv: -kv[1])[:7]:
        print(f"  {k:<34}{v:>8}  {100 * v / tot:>5.1f}%")


if __name__ == "__main__":
    main()
