"""The decision engine: one question per market per tick.

    After probability error, market price, fees, spread, slippage, latency,
    liquidity and risk, is this trade worth taking?

Pipeline (cheap gates first):

    time window -> can the order land -> data fresh & consistent -> strike &
    model ready -> model sanity vs market -> regime -> for each side: an offer
    exists, inside the price band, spread & depth acceptable, cost breakdown,
    net edge over the required edge, edge outside the model's own error band
    -> risk limits & sizing -> Intent

Every rejection carries one of ``Reason``. Rejections are counted and the
latest evaluation per market is kept so the dashboard can show *why* the bot
is not trading, which is usually the actual question.

Probability
-----------
The analytic TWAP pricer is blended with the market's own mid
(``market_blend``). On 292 recorded windows the 50/50 blend was the only
configuration whose out-of-sample Brier beat the market with an interval
excluding zero once results were clustered by 5-minute epoch; every fitted
model was worse than the market. The blend is the winner's-curse correction:
we only act when our estimate disagrees with the market, which selects our
own errors.
"""
from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum

from ..execution.latency import LatencyProfile
from ..features.engine import FeatureEngine
from ..features.orderflow import OrderFlowCalibration
from ..feeds.markets import MarketMeta
from ..feeds.spot import CompositeView
from ..pricing.digital import DEFAULT_PRICER, Pricer
from ..pricing.twap import TwapState, twap_fair_value
from ..risk.limits import RiskManager
from ..signals.costs import CostBreakdown, CostModel
from ..signals.regime import Regime, classify_regime
from ..types import Order, OrderBook, Side, TimeInForce

log = logging.getLogger(__name__)


class Reason(str, Enum):
    OUTSIDE_TIME_WINDOW = "OUTSIDE_TIME_WINDOW"
    CANNOT_LAND_IN_TIME = "CANNOT_LAND_IN_TIME"
    STALE_DATA = "STALE_DATA"
    DATA_INCONSISTENT = "DATA_INCONSISTENT"
    NO_STRIKE = "NO_STRIKE"
    MODEL_NOT_READY = "MODEL_NOT_READY"
    MODEL_SANITY = "MODEL_SANITY"
    BAD_REGIME = "BAD_REGIME"
    NO_OFFER = "NO_OFFER"
    PRICE_BAND = "PRICE_BAND"
    LOW_LIQUIDITY = "LOW_LIQUIDITY"
    HIGH_SLIPPAGE = "HIGH_SLIPPAGE"
    EDGE_TOO_SMALL = "EDGE_TOO_SMALL"
    LOW_CONFIDENCE = "LOW_CONFIDENCE"
    ALREADY_POSITIONED = "ALREADY_POSITIONED"
    RISK_LIMIT = "RISK_LIMIT"
    SIZE_ZERO = "SIZE_ZERO"


@dataclass(slots=True)
class EngineConfig:
    # --- when to look ----------------------------------------------------
    #: Books go one-sided once a window is decided (60-70% of samples in the
    #: last 60 s, >90% in the last 30 s), so the last minute is structurally
    #: untradeable for a taker; the replay lost money there as well.
    trade_window_start_s: float = 295.0
    trade_window_end_s: float = 60.0
    latency_safety_margin_ms: float = 500.0

    # --- data quality ----------------------------------------------------
    max_spot_age_ms: float = 1500.0
    max_book_age_ms: float = 2500.0
    max_source_deviation_bps: float = 15.0
    min_spot_sources: int = 1

    # --- probability -----------------------------------------------------
    #: Weight on the market mid in the blended probability.
    market_blend: float = 0.5
    #: Model-vs-mid sanity halt: rolling window, mid-range mids only. A
    #: decided market (mid 0.03-0.07, model ~0) is the favourite-longshot
    #: premium, not a model error, and one such window fed 477 identical
    #: tail diffs into a forever-accumulating mean and halted three assets.
    sanity_max_bias: float = 0.06
    sanity_min_samples: int = 200
    sanity_window: int = 600
    sanity_min_mid: float = 0.10

    # --- costs and edge --------------------------------------------------
    #: Required edge AFTER fee and slippage. Replayed on 385 windows
    #: (docs/strategy.md, "trade frequency"): 0.02 with no uncertainty charge
    #: and a 5c spread cap took 4.5x the fills of the original 0.05 setting
    #: at +0.056/share (t 1.6 over epochs) against +0.148/share (t 1.9) --
    #: more trades, thinner and equally unproven edge per trade.
    min_net_edge: float = 0.02
    expected_slippage: float = 0.005
    #: Fraction of the model's sigma-error band subtracted from the edge.
    #: The replay found the charge removed fills without improving EV.
    uncertainty_charge: float = 0.0
    #: LOW_CONFIDENCE gate: edge after costs must exceed this many bands.
    confidence_gate_mult: float = 0.5
    min_trade_price: float = 0.05
    max_trade_price: float = 0.95
    max_spread: float = 0.05
    min_depth_shares: float = 10.0
    cross_ticks: int = 1

    # --- order flow (Strategy D) ----------------------------------------
    #: Tilt the pricer's mean by an expected drift from spot-exchange order
    #: flow: drift_bps = slope * ofi_score, score in [-1, 1]. The slope is
    #: MEASURED live per asset (features/orderflow.py) and used with its
    #: sign only once it is significant; the prior is zero because the first
    #: live minutes showed recent aggressive buying predicting a *negative*
    #: next-10s return on every asset (transient impact reverting), the
    #: opposite of the textbook sign. Nothing tilts until the tape says so.
    ofi_enabled: bool = True
    ofi_drift_bps: float = 0.0              # prior slope, used only without live calibration
    ofi_max_drift_bps: float = 3.0
    ofi_calibrate_live: bool = True
    ofi_min_calibration_n: int = 1000
    ofi_min_t: float = 2.0                  # overlap-corrected |t| the measured slope must clear
    ofi_horizon_s: int = 10

    # --- regimes ---------------------------------------------------------
    excluded_liquidity: tuple[str, ...] = ("LIQUIDITY_COLLAPSE",)
    excluded_vol: tuple[str, ...] = ()
    exclude_extreme_move: bool = True

    # --- per-asset overrides: {"DOGE": {"min_net_edge": 0.07}} ------------
    asset_overrides: dict[str, dict[str, float]] = field(default_factory=dict)

    def for_asset(self, asset: str, key: str):
        return self.asset_overrides.get(asset, {}).get(key, getattr(self, key))


@dataclass(slots=True)
class SideView:
    side: str
    token_id: str
    p_model: float
    p_market: float
    ask: float | None
    bid: float | None
    depth_at_limit: float
    costs: CostBreakdown | None
    reason: Reason | None


@dataclass(slots=True)
class Evaluation:
    slug: str
    asset: str
    seconds_left: float
    reason: Reason | None                   # None = tradeable
    detail: str = ""
    p_analytic: float | None = None
    p_used: float | None = None            # blended P(up)
    p_market: float | None = None          # Up mid
    uncertainty: float = 0.0
    regime: Regime | None = None
    sides: list[SideView] = field(default_factory=list)
    # the intent, when tradeable
    side: str = ""
    token_id: str = ""
    limit_price: float = 0.0
    expected_price: float = 0.0
    size: float = 0.0
    net_edge: float = 0.0
    confidence: float = 0.0
    size_note: str = ""
    features: dict[str, float] = field(default_factory=dict)
    ofi_score: float = math.nan
    ofi_drift_bps: float = 0.0

    @property
    def tradeable(self) -> bool:
        return self.reason is None

    def as_dict(self) -> dict:
        return {
            "slug": self.slug, "asset": self.asset, "secs_left": round(self.seconds_left, 1),
            "reason": None if self.reason is None else self.reason.value, "detail": self.detail,
            "p_analytic": _r(self.p_analytic), "p_used": _r(self.p_used), "p_market": _r(self.p_market),
            "uncertainty": _r(self.uncertainty), "regime": None if self.regime is None else self.regime.label,
            "side": self.side, "limit": _r(self.limit_price), "size": _r(self.size, 2),
            "net_edge": _r(self.net_edge), "confidence": _r(self.confidence),
            "ofi": _r(self.ofi_score, 3), "ofi_drift_bps": _r(self.ofi_drift_bps, 3),
            "sides": [{"side": s.side, "p_model": _r(s.p_model), "p_market": _r(s.p_market),
                       "ask": _r(s.ask), "bid": _r(s.bid), "depth": _r(s.depth_at_limit, 1),
                       "costs": None if s.costs is None else s.costs.as_dict(),
                       "reason": None if s.reason is None else s.reason.value} for s in self.sides],
        }


def _r(v, nd: int = 4):
    return None if v is None or (isinstance(v, float) and math.isnan(v)) else round(float(v), nd)


@dataclass
class ModelAgreement:
    """Rolling fair-vs-mid bias per asset. A persistent one-directional gap is
    a bug in OUR model, not free money (see README)."""
    window: int = 600
    diffs: deque = field(default_factory=deque)

    def add(self, fair: float, mid: float) -> None:
        self.diffs.append(fair - mid)
        while len(self.diffs) > self.window:
            self.diffs.popleft()

    @property
    def n(self) -> int:
        return len(self.diffs)

    @property
    def bias(self) -> float:
        return sum(self.diffs) / len(self.diffs) if self.diffs else 0.0

    @property
    def mean_abs(self) -> float:
        return sum(abs(d) for d in self.diffs) / len(self.diffs) if self.diffs else 0.0


class StrategyEngine:
    def __init__(self, cfg: EngineConfig, risk: RiskManager, features: FeatureEngine | None = None,
                 pricer: Pricer | None = None, info_lag_floor_s: float = 0.0) -> None:
        self.cfg = cfg
        self.risk = risk
        self.features = features or FeatureEngine()
        self.pricer = pricer or DEFAULT_PRICER
        self.info_lag_floor_s = info_lag_floor_s
        self.rejections: dict[str, int] = {}
        self.last: dict[str, Evaluation] = {}
        self.agreement: dict[str, ModelAgreement] = {}
        self.regimes: dict[str, str] = {}
        self.ofi_calibration: dict[str, OrderFlowCalibration] = {}

    # ------------------------------------------------------------ order flow

    def _ofi_drift(self, asset: str, score: float) -> float:
        """Expected drift in bps from the order-flow score, prior or measured."""
        cfg = self.cfg
        if not cfg.ofi_enabled or score is None or math.isnan(score):
            return 0.0
        slope = cfg.ofi_drift_bps
        if cfg.ofi_calibrate_live:
            slope = 0.0
            cal = self.ofi_calibration.get(asset)
            if (cal is not None and cal.n(cfg.ofi_horizon_s) >= cfg.ofi_min_calibration_n
                    and abs(cal.t_stat(cfg.ofi_horizon_s)) >= cfg.ofi_min_t):
                # the measured slope, with its measured sign
                slope = cal.slope_bps(cfg.ofi_horizon_s) or 0.0
        return max(-cfg.ofi_max_drift_bps, min(cfg.ofi_max_drift_bps, slope * max(-1.0, min(1.0, score))))

    # --------------------------------------------------------------- helpers

    def _reject(self, ev: Evaluation, reason: Reason, detail: str = "") -> Evaluation:
        ev.reason, ev.detail = reason, detail
        self.rejections[reason.value] = self.rejections.get(reason.value, 0) + 1
        self.last[ev.slug] = ev
        return ev

    @staticmethod
    def _depth(book: OrderBook, limit: float) -> float:
        return sum(l.size for l in book.asks if l.price <= limit + 1e-9)

    # ------------------------------------------------------------- evaluate

    def evaluate(
        self,
        meta: MarketMeta,
        book_up: OrderBook | None,
        book_down: OrderBook | None,
        spot: CompositeView | None,
        spot_age_ms: float | None,
        book_age_ms: float | None,
        now: float,
        vol,                                   # EwmaVol | TwoScaleVol
        twap: TwapState,
        latency: LatencyProfile,
        balance: float,
        orderflow: dict[str, float] | None = None,
    ) -> Evaluation:
        cfg, m = self.cfg, meta.market
        left = m.seconds_remaining(now)
        ev = Evaluation(slug=m.slug, asset=m.asset, seconds_left=left, reason=None)

        if not (cfg.trade_window_end_s <= left <= cfg.trade_window_start_s):
            return self._reject(ev, Reason.OUTSIDE_TIME_WINDOW)
        need_ms = latency.round_trip_ms() + cfg.latency_safety_margin_ms
        if left * 1000.0 < need_ms:
            return self._reject(ev, Reason.CANNOT_LAND_IN_TIME)
        if m.strike <= 0.0:
            return self._reject(ev, Reason.NO_STRIKE)
        if spot is None or spot_age_ms is None:
            return self._reject(ev, Reason.STALE_DATA, "no spot")
        if spot_age_ms > cfg.max_spot_age_ms:
            return self._reject(ev, Reason.STALE_DATA, f"spot {spot_age_ms:.0f}ms")
        if book_age_ms is not None and book_age_ms > cfg.max_book_age_ms:
            return self._reject(ev, Reason.STALE_DATA, f"book {book_age_ms:.0f}ms")
        if spot.n_sources < cfg.min_spot_sources:
            return self._reject(ev, Reason.DATA_INCONSISTENT, f"{spot.n_sources} sources")
        if spot.n_sources >= 2 and spot.deviation_bps > cfg.max_source_deviation_bps:
            return self._reject(ev, Reason.DATA_INCONSISTENT, f"sources differ {spot.deviation_bps:.1f} bps")
        if vol is None or not getattr(vol, "ready", False) or not twap.ready:
            return self._reject(ev, Reason.MODEL_NOT_READY)

        # --- probability --------------------------------------------------
        of = orderflow or {}
        ev.ofi_score = of.get("ofi_score", math.nan)
        ev.ofi_drift_bps = self._ofi_drift(m.asset, ev.ofi_score)
        info_lag_s = max(spot_age_ms / 1000.0, self.info_lag_floor_s)
        fv = twap_fair_value(state=twap, strike=m.strike, sigma_per_sec=vol.sigma_per_sec,
                             now_ms=now, close_ts_ms=m.close_ts, info_lag_s=info_lag_s, pricer=self.pricer,
                             drift_log=ev.ofi_drift_bps * 1e-4)
        ev.p_analytic, ev.uncertainty = fv.p_up, fv.uncertainty
        mid_up = book_up.mid if book_up is not None else None
        if mid_up is None and book_down is not None and book_down.mid is not None:
            mid_up = 1.0 - book_down.mid
        ev.p_market = mid_up
        if mid_up is not None:
            ag = self.agreement.setdefault(m.asset, ModelAgreement(window=cfg.sanity_window))
            if cfg.sanity_min_mid <= mid_up <= 1.0 - cfg.sanity_min_mid:
                ag.add(fv.p_up, mid_up)
            if ag.n >= cfg.sanity_min_samples and abs(ag.bias) > cfg.sanity_max_bias:
                return self._reject(ev, Reason.MODEL_SANITY, f"bias {ag.bias:+.3f} over {ag.n}")
            p_used = (1.0 - cfg.market_blend) * fv.p_up + cfg.market_blend * mid_up
        else:
            p_used = fv.p_up
        ev.p_used = p_used

        # --- features & regime --------------------------------------------
        ub = book_up.best_bid if book_up is not None else None
        ua = book_up.best_ask if book_up is not None else None
        feats = self.features.features(
            asset=m.asset, slug=m.slug, strike=m.strike, spot=spot.price, now_ms=now,
            seconds_left=left, window_s=(m.close_ts - m.open_ts) / 1000.0,
            up_price=mid_up, up_bid=ub, up_ask=ua, sigma_per_sec=vol.sigma_per_sec,
            model_p_up=fv.p_up, model_z=fv.z, extra=of,
        )
        ev.features = feats
        reg = classify_regime(feats)
        ev.regime = reg
        self.regimes[m.asset] = reg.label
        if reg.liquidity in cfg.excluded_liquidity or reg.vol in cfg.excluded_vol \
                or (cfg.exclude_extreme_move and reg.extreme):
            return self._reject(ev, Reason.BAD_REGIME, reg.label)

        # --- each side ------------------------------------------------------
        cost_model = CostModel(fee=meta.fee, expected_slippage=cfg.for_asset(m.asset, "expected_slippage"),
                               uncertainty_charge=cfg.uncertainty_charge)
        min_edge = cfg.for_asset(m.asset, "min_net_edge")
        best: SideView | None = None
        for side, token, book, p in (("UP", m.yes_token_id, book_up, p_used),
                                     ("DOWN", m.no_token_id, book_down, 1.0 - p_used)):
            sv = SideView(side=side, token_id=token, p_model=p,
                          p_market=(mid_up if side == "UP" else 1.0 - mid_up) if mid_up is not None else math.nan,
                          ask=None, bid=None, depth_at_limit=0.0, costs=None, reason=None)
            ev.sides.append(sv)
            if book is None or book.best_ask is None:
                sv.reason = Reason.NO_OFFER
                continue
            ask, bid = book.best_ask, book.best_bid
            sv.ask, sv.bid = ask, bid
            if not (cfg.min_trade_price <= ask <= cfg.max_trade_price):
                sv.reason = Reason.PRICE_BAND
                continue
            if bid is not None and ask - bid > cfg.max_spread:
                sv.reason = Reason.LOW_LIQUIDITY
                continue
            limit = min(1.0 - m.tick_size, round(ask + cfg.cross_ticks * m.tick_size, 4))
            sv.depth_at_limit = self._depth(book, limit)
            if sv.depth_at_limit < cfg.min_depth_shares:
                sv.reason = Reason.LOW_LIQUIDITY
                continue
            cb = cost_model.evaluate(p, ask, bid, fv.uncertainty)
            sv.costs = cb
            if cb.net_edge < min_edge:
                sv.reason = Reason.EDGE_TOO_SMALL
                continue
            # the edge must clear the model's own error band, or we are
            # trading our vol-estimate noise
            if cb.gross_edge - cb.fee - cb.slippage < cfg.confidence_gate_mult * fv.uncertainty:
                sv.reason = Reason.LOW_CONFIDENCE
                continue
            if best is None or cb.net_edge > best.costs.net_edge:
                best = sv

        if best is None:
            reasons = [s.reason for s in ev.sides if s.reason is not None]
            # report the most informative reason: an edge that was measured
            # beats a side that never had an offer
            pick = (Reason.EDGE_TOO_SMALL if Reason.EDGE_TOO_SMALL in reasons else
                    Reason.LOW_CONFIDENCE if Reason.LOW_CONFIDENCE in reasons else
                    reasons[0] if reasons else Reason.NO_OFFER)
            return self._reject(ev, pick)

        # --- risk and size ----------------------------------------------------
        cb = best.costs
        confidence = cb.net_edge / (cb.net_edge + fv.uncertainty) if cb.net_edge + fv.uncertainty > 0 else 0.0
        epoch = int(m.open_ts // 1000)
        limit = min(1.0 - m.tick_size, round(best.ask + cfg.cross_ticks * m.tick_size, 4))
        shares, note = self.risk.size(
            p=best.p_model, price=best.ask, confidence=confidence, balance=balance,
            asset=m.asset, epoch=epoch, side=best.side, available_shares=best.depth_at_limit,
            min_size=m.min_size,
        )
        if shares <= 0.0:
            return self._reject(ev, Reason.SIZE_ZERO, note)
        verdict = self.risk.check(m.slug, m.asset, epoch, shares * best.ask)
        if not verdict.ok:
            return self._reject(ev, Reason(verdict.reason), verdict.detail)

        ev.side, ev.token_id, ev.limit_price, ev.expected_price = best.side, best.token_id, limit, best.ask
        ev.size, ev.net_edge, ev.confidence, ev.size_note = shares, cb.net_edge, confidence, note
        self.last[m.slug] = ev
        return ev

    # ------------------------------------------------------------ revalidate

    def revalidate(self, ev: Evaluation, book: OrderBook | None, min_edge: float | None = None) -> Reason | None:
        """Right before submitting: is the offer still there at a price that
        keeps the edge? Never chase: if the ask moved past the limit, drop it."""
        if book is None or book.best_ask is None:
            return Reason.NO_OFFER
        if book.best_ask > ev.limit_price + 1e-9:
            return Reason.HIGH_SLIPPAGE
        moved = book.best_ask - ev.expected_price
        if ev.net_edge - max(0.0, moved) < (self.cfg.min_net_edge if min_edge is None else min_edge):
            return Reason.EDGE_TOO_SMALL
        if self._depth(book, ev.limit_price) < ev.size:
            return Reason.LOW_LIQUIDITY
        return None

    def to_order(self, ev: Evaluation) -> Order:
        return Order(
            token_id=ev.token_id, side=Side.BUY, price=ev.limit_price, size=ev.size,
            tif=TimeInForce.FOK, expected_price=ev.expected_price, fair_at_decision=ev.p_used,
            tag=f"{ev.side} p={ev.p_used:.3f} edge={ev.net_edge:+.3f} conf={ev.confidence:.2f} "
                f"ofi={0.0 if math.isnan(ev.ofi_score) else ev.ofi_score:+.2f}",
        )

    def snapshot(self) -> dict:
        return {
            "rejections": dict(sorted(self.rejections.items(), key=lambda kv: -kv[1])),
            "regime": dict(self.regimes),
            "model_vs_market": {a: {"n": g.n, "bias": round(g.bias, 4), "mean_abs": round(g.mean_abs, 4)}
                                for a, g in self.agreement.items()},
            "orderflow": {a: c.report() for a, c in self.ofi_calibration.items()},
            "last": {slug: e.as_dict() for slug, e in self.last.items()},
        }
