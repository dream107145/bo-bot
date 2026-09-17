"""Taker-only, tail-focused strategy.

Why taker-only to start: posting two-sided quotes against faster participants
means you are filled preferentially when you are wrong. Adverse selection does
not announce itself -- it shows up as a slow bleed with a decent-looking fill
rate. Crossing the spread costs more per trade but only trades when YOU chose
to, which is the right trade-off until you have measured your own speed.

On Polymarket a binary market has two tokens. "Selling UP" without inventory is
just "buying DOWN", so this strategy only ever BUYS -- either the UP token or
the DOWN token -- and lets existing positions resolve. That avoids needing to
model share inventory for shorts.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from ..config import StrategyConfig
from ..execution.latency import LatencyProfile
from ..execution.paper import FeeModel
from ..pricing.digital import (
    DEFAULT_PRICER,
    FairValue,
    Pricer,
    fair_value,
    vol_uncertainty,
)
from ..pricing.twap import TwapState, twap_fair_value
from ..pricing.vol import EwmaVol
from ..types import Market, Order, OrderBook, Side, TimeInForce

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Intent:
    """A decision to trade, with every input that produced it recorded.

    Log these whether or not they fill. The rejected ones are the dataset that
    tells you what latency is costing.
    """
    market: Market
    token_id: str
    side: Side
    limit_price: float
    size: float
    expected_price: float
    fair: float                # after the winner's-curse shrink
    edge: float
    seconds_left: float
    spot_age_ms: float
    book_age_ms: float
    raw_fair: float = 0.0      # before the shrink, for diagnostics
    reason: str = ""


@dataclass(slots=True)
class SkipReason:
    code: str
    detail: str = ""


@dataclass(slots=True)
class TakerStrategy:
    cfg: StrategyConfig
    latency: LatencyProfile
    fees: FeeModel
    pricer: Pricer | None = None
    vol: dict[str, EwmaVol] = field(default_factory=dict)
    #: Per-asset TWAP accumulators. Required when cfg.use_twap is set --
    #: the live markets settle on a 60s TWAP, not a spot print.
    twap: dict[str, TwapState] = field(default_factory=dict)
    skips: dict[str, int] = field(default_factory=dict)

    def _skip(self, code: str) -> None:
        self.skips[code] = self.skips.get(code, 0) + 1

    # ------------------------------------------------------------------ main

    def evaluate(
        self,
        market: Market,
        book_up: OrderBook | None,
        book_down: OrderBook | None,
        spot: float | None,
        spot_age_ms: float | None,
        book_age_ms: float | None,
        now: float,
        gross_exposure: float,
        position_usdc: float,
        balance: float,
    ) -> Intent | None:
        """Decide whether to trade this market right now."""
        seconds_left = market.seconds_remaining(now)

        # --- gates that do not need a price -----------------------------
        if not (self.cfg.trade_window_end_s <= seconds_left <= self.cfg.trade_window_start_s):
            self._skip("outside_time_window")
            return None

        # The order must physically arrive before the market resolves. With a
        # 5-minute expiry and a home connection this is a live constraint, not
        # a formality: it silently removes the last second of every window.
        need_ms = self.latency.round_trip_ms() + self.cfg.latency_safety_margin_ms
        if seconds_left * 1000.0 < need_ms:
            self._skip("cannot_land_in_time")
            return None

        if spot is None or spot_age_ms is None:
            self._skip("no_spot")
            return None
        if spot_age_ms > self.cfg.max_spot_age_ms:
            self._skip("spot_too_stale")
            return None
        if book_age_ms is not None and book_age_ms > self.cfg.max_book_age_ms:
            self._skip("book_too_stale")
            return None

        vol = self.vol.get(market.asset)
        if vol is None or not vol.ready:
            self._skip("vol_not_ready")
            return None

        # --- fair value, priced with the information lag baked in -------
        # Both branches extend the horizon by the age of the observation: the
        # oracle view is stale, so the effective remaining time is longer than
        # the clock says. See pricing.digital.
        if self.cfg.use_twap:
            # The live markets settle on TWAP60(close) >= TWAP60(open), which
            # is a different random variable from the closing spot -- far less
            # uncertain near expiry. See pricing.twap.
            tw = self.twap.get(market.asset)
            if tw is None or not tw.ready:
                self._skip("twap_not_ready")
                return None
            fv = twap_fair_value(
                state=tw,
                strike=market.strike,
                sigma_per_sec=vol.sigma_per_sec,
                now_ms=now,
                close_ts_ms=market.close_ts,
                info_lag_s=spot_age_ms / 1000.0,
                pricer=self.pricer,
            )
        else:
            fv = fair_value(
                spot=spot,
                strike=market.strike,
                sigma_per_sec=vol.sigma_per_sec,
                seconds_to_close=seconds_left,
                info_lag_s=spot_age_ms / 1000.0,
                pricer=self.pricer,
                compute_uncertainty=False,
            )

        # Reject the MIDDLE, not the tails: near 0.50 the price is insensitive
        # to sigma, so there is no model edge, only variance.
        if self.cfg.min_fair_for_trade < fv.p_up < self.cfg.max_fair_for_trade:
            self._skip("fair_too_close_to_half")
            return None

        # --- compare against both books ---------------------------------
        candidates: list[Intent] = []
        uncertainty: float | None = None
        for token_id, book, fair_p, label in (
            (market.yes_token_id, book_up, fv.p_up, "UP"),
            (market.no_token_id, book_down, fv.p_down, "DOWN"),
        ):
            if book is None or book.best_ask is None:
                continue
            ask = book.best_ask
            if ask <= 0.0 or ask >= 1.0:
                continue
            # Stay out of the price extremes -- see StrategyConfig.min_trade_price.
            if not (self.cfg.min_trade_price <= ask <= self.cfg.max_trade_price):
                self._skip("price_outside_tradeable_band")
                continue

            # Winner's-curse correction: blend our estimate toward the
            # market's own mid before measuring edge. Skipping this is how an
            # 80% win rate still loses money -- see StrategyConfig.market_shrink.
            mid = book.mid
            if mid is not None and self.cfg.market_shrink > 0.0:
                k = self.cfg.market_shrink
                fair_used = (1.0 - k) * fair_p + k * mid
            else:
                fair_used = fair_p

            gross_edge = fair_used - ask
            fee = self.fees.charge(ask, 1.0)          # per share
            net_edge = gross_edge - fee

            if net_edge < self.cfg.min_edge:
                continue
            # Do not trade your own vol-estimate noise. Computed lazily: only
            # the few candidates that get this far are worth the extra CDFs.
            if uncertainty is None:
                uncertainty = fv.uncertainty if self.cfg.use_twap else vol_uncertainty(
                    log_moneyness=math.log(spot / market.strike),
                    sigma_per_sec=fv.sigma_per_sec,
                    horizon_s=fv.effective_horizon_s,
                    sigma_rel_error=0.25,
                    pricer=self.pricer or DEFAULT_PRICER,
                )
            if net_edge < self.cfg.min_edge_sigmas * max(uncertainty, 1e-6):
                self._skip("edge_inside_model_uncertainty")
                continue

            size = self._size(
                fair_used, ask, net_edge, book, balance, gross_exposure, position_usdc
            )
            if size <= 0.0:
                self._skip("size_zero")
                continue

            candidates.append(
                Intent(
                    market=market,
                    token_id=token_id,
                    side=Side.BUY,
                    # Cross by one tick: we want the fill, not a resting order
                    # that becomes a free option for someone faster.
                    limit_price=min(1.0 - market.tick_size,
                                    ask + self.cfg.cross_ticks * market.tick_size),
                    size=size,
                    expected_price=ask,
                    fair=fair_used,
                    raw_fair=fair_p,
                    edge=net_edge,
                    seconds_left=seconds_left,
                    spot_age_ms=spot_age_ms,
                    book_age_ms=book_age_ms or 0.0,
                    reason=f"{label} fair={fair_p:.3f} ask={ask:.3f} edge={net_edge:.3f}",
                )
            )

        if not candidates:
            self._skip("no_edge")
            return None
        return max(candidates, key=lambda c: c.edge * c.size)

    # ---------------------------------------------------------------- sizing

    def _size(
        self,
        p: float,
        price: float,
        net_edge: float,
        book: OrderBook,
        balance: float,
        gross_exposure: float,
        position_usdc: float,
    ) -> float:
        """Fractional Kelly, capped every way that matters.

        Kelly for a binary at price c with true probability p:
            f* = (p - c) / (1 - c)
        Full Kelly assumes p is known. It is not -- it is a model output with a
        fat error bar -- so this uses a fraction of it. Full Kelly on a
        mismeasured p is how accounts die.
        """
        if price >= 1.0 or price <= 0.0:
            return 0.0
        f_star = (p - price) / (1.0 - price)
        if f_star <= 0.0:
            return 0.0
        stake = balance * self.cfg.kelly_fraction * f_star

        # Floor the stake so a win nets roughly target_win_usdc, when Kelly's
        # (deliberately conservative) stake would leave less on the table.
        # Every cap below still applies -- this raises the ask, it never
        # bypasses the ceiling. See StrategyConfig.target_win_usdc for the
        # honest read on what this can and cannot do.
        if self.cfg.target_win_usdc > 0.0:
            profit_per_share = 1.0 - price
            if profit_per_share > 1e-6:
                target_stake = (self.cfg.target_win_usdc / profit_per_share) * price
                stake = max(stake, target_stake)

        stake = min(stake, self.cfg.max_position_usdc - position_usdc)
        stake = min(stake, self.cfg.max_gross_usdc - gross_exposure)
        stake = min(stake, balance)
        if stake < self.cfg.min_order_usdc:
            return 0.0

        shares = stake / price
        shares = min(shares, self.cfg.max_shares_per_order)

        # Never ask for more than is actually resting, minus what faster
        # participants will have taken by the time we land.
        available = sum(
            lvl.size for lvl in book.asks if lvl.price <= price + 1e-9
        ) * (1.0 - self.latency.contention)
        shares = min(shares, available)

        # Floor, not round: every cap above is a ceiling on dollar risk, and
        # rounding shares UP by half a cent can push the reconstructed stake
        # fractionally past it. Flooring keeps "never exceed" exactly true.
        return max(0.0, math.floor(shares * 100.0) / 100.0)

    # ----------------------------------------------------------------- order

    def to_order(self, intent: Intent, fair_at_decision: float) -> Order:
        return Order(
            token_id=intent.token_id,
            side=intent.side,
            price=intent.limit_price,
            size=intent.size,
            # FOK: if the book moved while we were on the wire we want nothing,
            # not a partial position at a price we never agreed to.
            tif=TimeInForce.FOK,
            expected_price=intent.expected_price,
            fair_at_decision=fair_at_decision,
            tag=intent.reason,
        )
