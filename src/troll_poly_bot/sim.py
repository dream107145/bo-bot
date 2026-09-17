"""Synthetic market simulator.

Lets you run the whole stack today, with no API keys, and measure how much of
the strategy is edge and how much is just being fast.

What is being modelled
----------------------
A GBM spot path, 5-minute windows struck at the spot when the window opens, and
a synthetic order book quoted by an opposing "maker" who has:

  * their own information lag (``maker_lag_ms``) -- they are stale too
  * a favourite-longshot bias (``maker_bias``) -- they shrink extreme
    probabilities toward 0.50, which is the best-documented bias in real
    prediction markets and the main structural edge available to a slow player
  * a half-spread and finite depth

This matters: your edge in the sim comes from two distinguishable sources, and
you should always know which one you are harvesting.

    speed edge      = maker_lag_ms - your reaction lag   (evaporates if slower)
    structural edge = maker_bias                         (survives being slow)

Run ``compare_profiles()`` to split them. If your PnL vanishes at COLOCATED ->
HOME_BROADBAND, you were harvesting speed and you need infrastructure. If it
survives, you were harvesting the bias and you have a real strategy.

This is a MODEL, not the market. It cannot tell you that the strategy works --
only that the plumbing is correct and how sensitive it is to latency. The real
test is the recorded-data calibration in research/.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Callable

import numpy as np

from .config import BotConfig
from .execution.latency import LatencyModel, LatencyProfile
from .execution.paper import FeeModel, PaperExchange
from .feeds.delayed import DelayedFeed
from .pricing.digital import GaussianPricer, fair_value
from .pricing.vol import EwmaVol
from .strategy.taker import TakerStrategy
from .types import BookLevel, Market, OrderBook

_MAKER_PRICER = GaussianPricer()


@dataclass(slots=True)
class SimConfig:
    n_windows: int = 120
    window_s: float = 300.0
    dt_ms: float = 100.0
    sigma_per_sec: float = 9e-5       # ~0.9 bps/s, BTC at ~50% annualised
    start_price: float = 100_000.0
    asset: str = "BTC"

    #: How stale the opposing maker's own view is. The gap between this and
    #: YOUR reaction lag is the speed component of the edge.
    maker_lag_ms: float = 600.0
    #: Favourite-longshot bias: how far the maker shrinks extreme probabilities
    #: toward 0.50. 0.08 is a plausible order of magnitude for retail-facing
    #: books; measure your own from recorded data before believing it.
    maker_bias: float = 0.08
    half_spread: float = 0.015
    depth: float = 400.0
    tick: float = 0.01
    seed: int = 0


@dataclass(slots=True)
class SimResult:
    profile_name: str
    reaction_lag_ms: float
    final_equity: float
    pnl: float
    n_trades: int
    n_windows_traded: int
    latency: dict[str, float] = field(default_factory=dict)
    skips: dict[str, int] = field(default_factory=dict)
    #: one row per fill: fair at decision, fill price, size, and whether it won.
    #: This is what separates "the model is wrong" from "we got unlucky".
    trades: list[dict] = field(default_factory=list)
    #: one row per window: equity after settlement, so the UI can plot the
    #: path rather than just the endpoint. A final number hides a drawdown
    #: that would have stopped you out in real life.
    equity_curve: list[dict] = field(default_factory=list)

    def max_drawdown(self) -> float:
        """Worst peak-to-trough on the equity path, in USDC."""
        peak = float("-inf")
        worst = 0.0
        for row in self.equity_curve:
            peak = max(peak, row["equity"])
            worst = min(worst, row["equity"] - peak)
        return worst

    def calibration(self) -> dict[str, float]:
        """Did the model's probabilities come true?

        If ``predicted`` and ``realised`` diverge, the pricer is miscalibrated
        and no amount of execution tuning will save the strategy. If they agree
        but PnL is negative, the edge is real and something downstream --
        fees, slippage, adverse selection -- is eating it.
        """
        # A fill whose originating order was not tracked carries a NaN fair.
        # It cannot be compared against a prediction, and averaging it in turns
        # every metric here into NaN -- which then escapes as invalid JSON and
        # takes the whole dashboard down. Drop those rows, and report how many
        # were dropped rather than hiding it.
        rows = [t for t in self.trades if t["fair"] == t["fair"]]
        untracked = len(self.trades) - len(rows)
        if not rows:
            return {}
        n = len(rows)
        w = sum(t["size"] for t in rows)
        if w <= 0:
            return {}
        predicted = sum(t["fair"] * t["size"] for t in rows) / w
        realised = sum(t["won"] * t["size"] for t in rows) / w
        avg_price = sum(t["price"] * t["size"] for t in rows) / w
        pnl_per_share = sum(
            (t["won"] - t["price"]) * t["size"] for t in rows
        ) / w
        return {
            "n_fills": float(n),
            "n_untracked": float(untracked),
            "shares": w,
            "predicted_win_rate": predicted,
            "realised_win_rate": realised,
            "avg_fill_price": avg_price,
            "edge_at_decision": predicted - avg_price,
            "realised_pnl_per_share": pnl_per_share,
        }

    def summary(self) -> str:
        lr = self.latency
        return (
            f"{self.profile_name:<16} "
            f"lag={self.reaction_lag_ms:>6.0f}ms  "
            f"pnl={self.pnl:>9.2f}  "
            f"trades={self.n_trades:>4}  "
            f"fill={lr.get('fill_rate', 0):>5.1%}  "
            f"slip={lr.get('total_slippage_cost', 0):>8.2f}  "
            f"fees={lr.get('total_fees', 0):>7.2f}  "
            f"late={int(lr.get('rejected_market_closed', 0)):>3}  "
            f"fok_rej={int(lr.get('rejected_fok_unfillable', 0)):>4}"
        )


def _quote_book(
    token_id: str,
    p: float,
    cfg: SimConfig,
    ts: float,
) -> OrderBook:
    """Build a two-sided book around the maker's (biased, stale) probability."""
    p = min(max(p, 0.0), 1.0)
    bid = max(cfg.tick, round((p - cfg.half_spread) / cfg.tick) * cfg.tick)
    ask = min(1.0 - cfg.tick, round((p + cfg.half_spread) / cfg.tick) * cfg.tick)
    if ask <= bid:
        ask = min(1.0 - cfg.tick, bid + cfg.tick)
    return OrderBook(
        token_id=token_id,
        bids=[BookLevel(bid, cfg.depth), BookLevel(round(bid - cfg.tick, 4), cfg.depth * 2)],
        asks=[BookLevel(ask, cfg.depth), BookLevel(round(ask + cfg.tick, 4), cfg.depth * 2)],
        ts=ts,
    )


def run_sim(
    sim_cfg: SimConfig,
    bot_cfg: BotConfig,
    profile: LatencyProfile,
    progress: Callable[[int, int], bool] | None = None,
) -> SimResult:
    """Run one profile.

    ``progress(window_index, total)`` is called after each window. Return
    False from it to abort the run early -- the UI uses this to cancel.
    """
    rng = np.random.default_rng(sim_cfg.seed)
    lat = LatencyModel(profile, seed=bot_cfg.latency_seed)
    fees = FeeModel(rate=bot_cfg.fees.rate)
    ex = PaperExchange(lat, fees, starting_balance=bot_cfg.starting_balance)

    vol = EwmaVol(
        grid_ms=bot_cfg.vol.grid_ms,
        halflife_s=bot_cfg.vol.halflife_s,
        floor_per_sec=bot_cfg.vol.floor_per_sec,
        ceil_per_sec=bot_cfg.vol.ceil_per_sec,
    )
    # The synthetic market resolves on the CLOSING SPOT, so the strategy is
    # priced that way here. The live venue settles on a 60s TWAP instead -- see
    # pricing.twap -- which this simulator does not model. Do not read sim PnL
    # as evidence about the TWAP edge; it is evidence about plumbing and
    # latency sensitivity only.
    sim_strategy_cfg = replace(bot_cfg.strategy, use_twap=False)
    strat = TakerStrategy(
        cfg=sim_strategy_cfg,
        latency=profile,
        fees=fees,
        vol={sim_cfg.asset: vol},
    )

    spot_feed: DelayedFeed[float] = DelayedFeed(lat.md_spot, maxlen=8192)
    book_feeds: dict[str, DelayedFeed[OrderBook]] = {}

    dt = sim_cfg.dt_ms
    step_sigma = sim_cfg.sigma_per_sec * math.sqrt(dt / 1000.0)

    now = 0.0
    px = sim_cfg.start_price
    px_history: list[tuple[float, float]] = [(now, px)]

    n_trades = 0
    windows_traded = 0
    all_trades: list[dict] = []
    equity_curve: list[dict] = []

    # warm the vol estimator before the first window so we are not trading off
    # an unconverged sigma
    for _ in range(int(400 * 1000 / dt)):
        px *= math.exp(step_sigma * rng.standard_normal())
        now += dt
        px_history.append((now, px))
        vol.update(px, now)

    # Simulated time only ever moves forward, so the index of the maker's
    # stale price does too. A binary search over a list that grows to millions
    # of entries turns the inner loop quadratic-ish; a monotonic cursor is O(1)
    # amortised and keeps long runs usable.
    lag_cursor = 0

    def lagged_price(at: float) -> float:
        """The maker's stale view of spot."""
        nonlocal lag_cursor
        target = at - sim_cfg.maker_lag_ms
        n = len(px_history)
        while lag_cursor + 1 < n and px_history[lag_cursor + 1][0] <= target:
            lag_cursor += 1
        return px_history[lag_cursor][1]

    for w in range(sim_cfg.n_windows):
        strike = px
        open_ts = now
        close_ts = now + sim_cfg.window_s * 1000.0
        market = Market(
            condition_id=f"sim-{w}",
            asset=sim_cfg.asset,
            yes_token_id=f"sim-{w}-UP",
            no_token_id=f"sim-{w}-DOWN",
            strike=strike,
            open_ts=open_ts,
            close_ts=close_ts,
            tick_size=sim_cfg.tick,
        )
        for tid in (market.yes_token_id, market.no_token_id):
            book_feeds[tid] = DelayedFeed(lat.md_book, maxlen=4096)

        true_books: dict[str, OrderBook] = {}
        traded_this_window = False
        fair_by_order: dict[str, float] = {}
        window_fills: list[dict] = []

        while now < close_ts:
            px *= math.exp(step_sigma * rng.standard_normal())
            now += dt
            px_history.append((now, px))
            vol.update(px, now)
            spot_feed.publish(px, now)

            secs_left = max(0.0, (close_ts - now) / 1000.0)

            # --- the opposing maker requotes off their own stale view -----
            maker_fv = fair_value(
                spot=lagged_price(now),
                strike=strike,
                sigma_per_sec=sim_cfg.sigma_per_sec,
                seconds_to_close=secs_left,
                pricer=_MAKER_PRICER,
            )
            # favourite-longshot bias: shrink toward 0.50
            p_up_quoted = maker_fv.p_up + sim_cfg.maker_bias * (0.5 - maker_fv.p_up)
            up_book = _quote_book(market.yes_token_id, p_up_quoted, sim_cfg, now)
            down_book = _quote_book(market.no_token_id, 1.0 - p_up_quoted, sim_cfg, now)
            true_books[market.yes_token_id] = up_book
            true_books[market.no_token_id] = down_book
            book_feeds[market.yes_token_id].publish(up_book, now)
            book_feeds[market.no_token_id].publish(down_book, now)

            # --- exchange advances against the TRUE book -----------------
            for res in ex.step(now, lambda tid: true_books.get(tid), lambda tid: close_ts):
                for f in res.fills:
                    window_fills.append({
                        "token_id": f.token_id,
                        "fair": fair_by_order.get(f.order_id, float("nan")),
                        "price": f.price,
                        "size": f.size,
                        "secs_left": (close_ts - f.exchange_ts) / 1000.0,
                    })

            # --- strategy sees only delayed views ------------------------
            seen_spot = spot_feed.view(now)
            spot_age = spot_feed.view_age_ms(now)
            seen_up = book_feeds[market.yes_token_id].view(now)
            seen_down = book_feeds[market.no_token_id].view(now)
            book_age = book_feeds[market.yes_token_id].view_age_ms(now)

            gross = sum(
                abs(p.shares) * 0.5 for p in ex.positions.values() if abs(p.shares) > 0
            )
            pos_usdc = sum(
                abs(ex.positions[t].shares) * 0.5
                for t in (market.yes_token_id, market.no_token_id)
                if t in ex.positions
            )

            intent = strat.evaluate(
                market=market,
                book_up=seen_up,
                book_down=seen_down,
                spot=seen_spot,
                spot_age_ms=spot_age,
                book_age_ms=book_age,
                now=now,
                gross_exposure=gross,
                position_usdc=pos_usdc,
                balance=max(ex.balance, 0.0),
            )
            if intent is not None:
                order = strat.to_order(intent, intent.fair)
                fair_by_order[order.order_id] = intent.fair
                ex.submit(order, now)
                n_trades += 1
                traded_this_window = True

        # --- resolve ----------------------------------------------------
        # drain anything still on the wire before settling
        for _ in range(40):
            now += dt
            for res in ex.step(now, lambda tid: true_books.get(tid), lambda tid: close_ts):
                for f in res.fills:
                    window_fills.append({
                        "token_id": f.token_id,
                        "fair": fair_by_order.get(f.order_id, float("nan")),
                        "price": f.price,
                        "size": f.size,
                        "secs_left": (close_ts - f.exchange_ts) / 1000.0,
                    })

        went_up = px > strike
        for t in window_fills:
            won = went_up if t["token_id"] == market.yes_token_id else not went_up
            t["won"] = 1.0 if won else 0.0
            all_trades.append(t)
        ex.settle_market(market.yes_token_id, won=went_up)
        ex.settle_market(market.no_token_id, won=not went_up)
        if traded_this_window:
            windows_traded += 1

        equity_curve.append({
            "window": w,
            "equity": round(ex.balance, 4),
            "pnl": round(ex.balance - bot_cfg.starting_balance, 4),
            "trades": n_trades,
            "fills": len(all_trades),
        })
        if progress is not None and not progress(w + 1, sim_cfg.n_windows):
            break

    return SimResult(
        profile_name=profile.name,
        reaction_lag_ms=profile.reaction_lag_ms(),
        final_equity=ex.balance,
        pnl=ex.balance - bot_cfg.starting_balance,
        n_trades=n_trades,
        n_windows_traded=windows_traded,
        latency=ex.latency_report(),
        skips=dict(strat.skips),
        trades=all_trades,
        equity_curve=equity_curve,
    )


def compare_profiles(
    sim_cfg: SimConfig | None = None,
    bot_cfg: BotConfig | None = None,
    profiles: list[LatencyProfile] | None = None,
    progress: Callable[[int, int], bool] | None = None,
) -> list[SimResult]:
    """Run the same strategy, same price path, different network links.

    The spread between the rows is the part of your PnL that is infrastructure
    rather than insight.
    """
    from .execution.latency import COLOCATED, HOME_BROADBAND, HOME_WIFI_POOR, INSTANT, VPS_NEARBY

    sim_cfg = sim_cfg or SimConfig()
    bot_cfg = bot_cfg or BotConfig()
    profiles = profiles or [INSTANT, COLOCATED, VPS_NEARBY, HOME_BROADBAND, HOME_WIFI_POOR]
    out: list[SimResult] = []
    total = len(profiles)
    for i, prof in enumerate(profiles):
        def step(done: int, n: int, _i=i) -> bool:
            if progress is None:
                return True
            return progress(int((_i + done / max(n, 1)) * 100 / total), 100)
        out.append(run_sim(sim_cfg, bot_cfg, prof, progress=step))
    return out
