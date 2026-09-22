"""Hard limits and sizing. Nothing here ever increases size after a loss.

Sizing is fractional Kelly on the *net* edge, scaled by the model's confidence
and capped every way that matters:

    stake = balance * kelly_fraction * f_star * confidence
    f_star = (p - c) / (1 - c)        Kelly for a binary at price c

then clamped by per-market, per-asset, per-epoch, gross and share caps.

Correlation
-----------
All crypto 5m markets in the same 300s window are one bet: measured on the
archive, 13 of 14 multi-asset epochs traded the same direction and per-epoch
PnL correlated 0.3-0.6 across assets. So exposure is also capped per EPOCH,
and a second same-direction position in an epoch is scaled down by
``(1 - correlation)`` before the caps apply.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


@dataclass(slots=True)
class RiskConfig:
    #: Sizing is deliberately SMALL rather than the caps being LOOSE.
    #: The epoch cap divided by the per-market cap is the real limit on how
    #: many windows we can be in at once: at 30/10 that was three. Halving the
    #: per-market stake makes it six, so the bot trades roughly twice as often
    #: without a single aggregate cap moving -- same money at risk, spread over
    #: more windows. That also feeds the evidence statistic, which is clustered
    #: per epoch and needs epochs, not dollars, to ever reach |t| ~ 2.
    kelly_fraction: float = 0.15           # was 0.25: smaller bets, more of them
    #: Multiply the Kelly stake by the model confidence in [0, 1].
    confidence_scaling: bool = True
    max_position_usdc: float = 5.0         # per market (was 10.0)
    max_asset_exposure_usdc: float = 15.0  # across a single asset's open markets
    max_epoch_exposure_usdc: float = 30.0  # all assets, same 300s window -- UNCHANGED
    max_total_exposure_usdc: float = 50.0  # UNCHANGED
    max_concurrent_positions: int = 10   # more windows open at once, each smaller
    max_daily_loss_usdc: float = 20.0
    max_drawdown_usdc: float = 30.0
    min_order_usdc: float = 2.0
    max_shares_per_order: float = 100.0
    #: Assumed correlation between same-direction bets in one epoch.
    epoch_correlation: float = 0.5
    #: Never take more than this fraction of the resting depth at the limit.
    max_depth_fraction: float = 0.5
    #: Scaling in. 1 = one entry per market per window (the 5m behaviour).
    #: Above 1, a market we already hold may be bought AGAIN when every gate
    #: passes afresh -- same side only, at least ``reentry_cooldown_s`` after
    #: the previous fill, and never past ``max_position_usdc`` in total. A
    #: window closed early by a stop is never re-entered (live.py, `exited`).
    max_entries_per_market: int = 1
    reentry_cooldown_s: float = 30.0


@dataclass(slots=True)
class OpenPosition:
    slug: str
    asset: str
    epoch: int
    side: str          # "UP" | "DOWN"
    usdc: float
    shares: float
    entries: int = 1           # fills that built it
    last_fill_s: float = 0.0   # wall clock of the latest fill


@dataclass(slots=True)
class RiskVerdict:
    ok: bool
    reason: str = ""
    detail: str = ""


@dataclass
class RiskManager:
    cfg: RiskConfig = field(default_factory=RiskConfig)
    starting_balance: float = 100.0
    open: dict[str, OpenPosition] = field(default_factory=dict)
    realised_total: float = 0.0
    realised_today: float = 0.0
    peak_equity: float = 0.0
    day: int = field(default_factory=lambda: int(time.time() // 86400))
    consecutive_losses: int = 0

    def __post_init__(self) -> None:
        self.peak_equity = self.starting_balance

    # ---------------------------------------------------------- bookkeeping

    def _roll_day(self, now_s: float) -> None:
        d = int(now_s // 86400)
        if d != self.day:
            self.day, self.realised_today = d, 0.0

    def on_fill(self, slug: str, asset: str, epoch: int, side: str, usdc: float, shares: float,
                now_s: float | None = None) -> None:
        now_s = time.time() if now_s is None else now_s
        pos = self.open.get(slug)
        if pos is None:
            self.open[slug] = OpenPosition(slug, asset, epoch, side, usdc, shares, 1, now_s)
        else:
            pos.usdc += usdc
            pos.shares += shares
            pos.entries += 1
            pos.last_fill_s = now_s

    def position_headroom(self, slug: str) -> float:
        """Dollars still allowed into this market under the per-market cap."""
        pos = self.open.get(slug)
        return self.cfg.max_position_usdc - (pos.usdc if pos is not None else 0.0)

    def on_settle(self, slug: str, pnl: float, now_s: float | None = None) -> None:
        self._roll_day(time.time() if now_s is None else now_s)
        self.open.pop(slug, None)
        self.realised_total += pnl
        self.realised_today += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0
        eq = self.starting_balance + self.realised_total
        self.peak_equity = max(self.peak_equity, eq)

    # ------------------------------------------------------------- exposure

    def exposure_total(self) -> float:
        return sum(p.usdc for p in self.open.values())

    def exposure_asset(self, asset: str) -> float:
        return sum(p.usdc for p in self.open.values() if p.asset == asset)

    def exposure_epoch(self, epoch: int) -> float:
        return sum(p.usdc for p in self.open.values() if p.epoch == epoch)

    def same_direction_in_epoch(self, epoch: int, side: str, asset: str) -> float:
        return sum(p.usdc for p in self.open.values()
                   if p.epoch == epoch and p.side == side and p.asset != asset)

    @property
    def drawdown(self) -> float:
        return self.peak_equity - (self.starting_balance + self.realised_total)

    # ---------------------------------------------------------------- gates

    def halted(self, now_s: float | None = None) -> RiskVerdict:
        self._roll_day(time.time() if now_s is None else now_s)
        if self.realised_today <= -self.cfg.max_daily_loss_usdc:
            return RiskVerdict(False, "RISK_LIMIT", f"daily loss {self.realised_today:+.2f}")
        if self.drawdown >= self.cfg.max_drawdown_usdc:
            return RiskVerdict(False, "RISK_LIMIT", f"drawdown {self.drawdown:.2f}")
        return RiskVerdict(True)

    def check(self, slug: str, asset: str, epoch: int, stake: float,
              side: str = "", now_s: float | None = None) -> RiskVerdict:
        h = self.halted()
        if not h.ok:
            return h
        pos = self.open.get(slug)
        if pos is not None:
            c = self.cfg
            if pos.entries >= max(int(c.max_entries_per_market), 1):
                return RiskVerdict(False, "ALREADY_POSITIONED",
                                   f"{pos.entries} of {max(int(c.max_entries_per_market), 1)} entries")
            if side and side != pos.side:
                # buying the other side of a binary we hold is a hedge that
                # pays two fees to lock in a loss; the stop is the way out
                return RiskVerdict(False, "ALREADY_POSITIONED", f"holding {pos.side}, not {side}")
            now_s = time.time() if now_s is None else now_s
            if now_s - pos.last_fill_s < c.reentry_cooldown_s:
                return RiskVerdict(False, "ALREADY_POSITIONED",
                                   f"cooldown {c.reentry_cooldown_s - (now_s - pos.last_fill_s):.0f}s")
            if pos.usdc + stake > c.max_position_usdc + 1e-9:
                return RiskVerdict(False, "RISK_LIMIT", "position cap")
        else:
            if len(self.open) >= self.cfg.max_concurrent_positions:
                return RiskVerdict(False, "RISK_LIMIT", f"{len(self.open)} positions open")
            if stake > self.cfg.max_position_usdc + 1e-9:
                return RiskVerdict(False, "RISK_LIMIT", "position cap")
        if self.exposure_asset(asset) + stake > self.cfg.max_asset_exposure_usdc + 1e-9:
            return RiskVerdict(False, "RISK_LIMIT", f"{asset} exposure cap")
        if self.exposure_epoch(epoch) + stake > self.cfg.max_epoch_exposure_usdc + 1e-9:
            return RiskVerdict(False, "RISK_LIMIT", "epoch (correlated) exposure cap")
        if self.exposure_total() + stake > self.cfg.max_total_exposure_usdc + 1e-9:
            return RiskVerdict(False, "RISK_LIMIT", "total exposure cap")
        return RiskVerdict(True)

    # --------------------------------------------------------------- sizing

    def size(self, p: float, price: float, confidence: float, balance: float,
             asset: str, epoch: int, side: str, available_shares: float,
             min_size: float = 5.0, slug: str = "") -> tuple[float, str]:
        """Shares to buy, and why it came out that way. 0 means no trade."""
        c = self.cfg
        if price <= 0.0 or price >= 1.0 or balance <= 0.0:
            return 0.0, "bad price"
        f_star = (p - price) / (1.0 - price)
        if f_star <= 0.0:
            return 0.0, "no kelly edge"
        conf = min(max(confidence, 0.0), 1.0) if c.confidence_scaling else 1.0
        stake = balance * c.kelly_fraction * f_star * conf
        # a same-direction bet already on in this epoch is the same bet
        if self.same_direction_in_epoch(epoch, side, asset) > 0.0:
            stake *= (1.0 - c.epoch_correlation)
        caps = {
            # what is left under the per-market cap once what we already hold
            # in this market is counted (the full cap when we hold nothing)
            "position cap": self.position_headroom(slug) if slug else c.max_position_usdc,
            "asset cap": c.max_asset_exposure_usdc - self.exposure_asset(asset),
            "epoch cap": c.max_epoch_exposure_usdc - self.exposure_epoch(epoch),
            "total cap": c.max_total_exposure_usdc - self.exposure_total(),
            "balance": balance,
        }
        binding = "kelly"
        for name, cap in caps.items():
            if cap < stake:
                stake, binding = cap, name
        if stake < c.min_order_usdc:
            return 0.0, f"below min order ({binding})"
        shares = min(stake / price, c.max_shares_per_order, available_shares * c.max_depth_fraction)
        shares = math.floor(shares * 100.0) / 100.0
        if shares < min_size:
            return 0.0, f"below venue min size ({binding})"
        return shares, binding

    def snapshot(self) -> dict:
        return {
            "open_positions": len(self.open),
            "exposure_total": round(self.exposure_total(), 2),
            "exposure_by_asset": {a: round(self.exposure_asset(a), 2)
                                  for a in sorted({p.asset for p in self.open.values()})},
            "realised_today": round(self.realised_today, 2),
            "drawdown": round(self.drawdown, 2),
            "peak_equity": round(self.peak_equity, 2),
            "consecutive_losses": self.consecutive_losses,
            "halted": (lambda v: None if v.ok else v.detail)(self.halted()),
        }
