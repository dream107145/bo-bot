"""Configuration. Every magic number in the system lives here.

Two generations coexist:

* ``StrategyConfig`` / ``FeeConfig`` -- the ORIGINAL taker strategy, kept
  only for the synthetic simulator (``sim.py``), its dashboard schema and the
  old/new comparison in ``scripts/research_backtest.py``. The live bot no
  longer uses them.
* ``EngineConfig`` / ``RiskConfig`` / ``FeedConfig`` -- the redesigned
  multi-asset engine (``strategy.engine``, ``risk.limits``, ``feeds.spot``).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from .execution.latency import PROFILES, LatencyProfile
from .market.discovery import CANDIDATE_ASSETS
from .risk.limits import RiskConfig
from .strategy.engine import EngineConfig


@dataclass(slots=True)
class StrategyConfig:
    """The original strategy's knobs. Used by sim.py and the legacy replay only."""
    assets: tuple[str, ...] = ("BTC", "ETH")
    min_fair_for_trade: float = 0.15
    max_fair_for_trade: float = 0.85
    trade_window_start_s: float = 230.0
    trade_window_end_s: float = 3.0
    min_edge: float = 0.05
    min_edge_sigmas: float = 1.5
    market_shrink: float = 0.5
    min_trade_price: float = 0.03
    max_trade_price: float = 0.97
    cross_ticks: int = 1
    use_twap: bool = True
    latency_safety_margin_ms: float = 400.0
    max_book_age_ms: float = 1500.0
    max_spot_age_ms: float = 800.0
    kelly_fraction: float = 0.25
    max_position_usdc: float = 50.0
    max_gross_usdc: float = 200.0
    target_win_usdc: float = 0.0
    max_shares_per_order: float = 200.0
    min_order_usdc: float = 2.0
    max_correlated_delta_usdc: float = 120.0
    sanity_max_bias: float = 0.06
    sanity_min_samples: int = 200
    daily_loss_limit_usdc: float = 100.0
    kill_switch_consecutive_losses: int = 12


@dataclass(slots=True)
class VolConfig:
    grid_ms: float = 1000.0
    halflife_s: float = 120.0
    floor_per_sec: float = 2e-5
    ceil_per_sec: float = 5e-3
    #: Estimate vol at the horizon actually being priced. Measured on the
    #: archive the 60s/1s variance ratio was 1.34 on BTC and ~1.0 on ETH, SOL
    #: and XRP (an earlier day measured 2.45 on BTC); the slow estimator
    #: measures it live rather than assuming it. See pricing.vol.TwoScaleVol.
    two_scale: bool = True
    slow_grid_ms: float = 30_000.0
    slow_halflife_s: float = 3600.0
    #: Prior sigma ratio until the slow estimator is warm. 1.4 was the old
    #: default and overstates ETH/SOL/XRP vol by ~40% for the first 15 min;
    #: 1.15 is the archive median across assets.
    default_ratio: float = 1.15


@dataclass(slots=True)
class FeeConfig:
    """LEGACY flat-rate fee for the synthetic sim. The live bot reads the
    venue's own schedule per market (signals.costs.FeeSchedule)."""
    rate: float = 0.02


@dataclass(slots=True)
class FeedConfig:
    exchanges: tuple[str, ...] = ("binance", "bybit", "coinbase")
    candidate_assets: tuple[str, ...] = CANDIDATE_ASSETS
    reprobe_s: float = 1800.0
    max_quote_age_ms: float = 3000.0
    #: Feed the estimators at most this often per asset; three exchanges push
    #: far more ticks than a 1 s vol grid or a 60 s TWAP can use.
    model_update_interval_ms: float = 100.0


@dataclass(slots=True)
class BotConfig:
    mode: str = "paper"                   # paper | live (live is not wired)
    latency_profile: str = "home_broadband"
    latency_seed: int = 7
    starting_balance: float = 100.0

    strategy: StrategyConfig = field(default_factory=StrategyConfig)   # legacy sim
    fees: FeeConfig = field(default_factory=FeeConfig)                 # legacy sim
    engine: EngineConfig = field(default_factory=EngineConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    vol: VolConfig = field(default_factory=VolConfig)
    feeds: FeedConfig = field(default_factory=FeedConfig)

    record_dir: str = "data/recordings"
    chart_dir: str = "data/charts"
    log_level: str = "INFO"

    @property
    def latency(self) -> LatencyProfile:
        if self.latency_profile not in PROFILES:
            raise ValueError(
                f"unknown latency profile {self.latency_profile!r}; "
                f"choose from {sorted(PROFILES)}"
            )
        return PROFILES[self.latency_profile]

    def scale_risk_to_balance(self, balance: float, reference: float = 100.0) -> None:
        """Risk caps are fractions of the account. Defaults assume $100."""
        k = max(balance, 1.0) / reference
        r = self.risk
        r.max_position_usdc *= k
        r.max_asset_exposure_usdc *= k
        r.max_epoch_exposure_usdc *= k
        r.max_total_exposure_usdc *= k
        r.max_daily_loss_usdc *= k
        r.max_drawdown_usdc *= k
        r.max_shares_per_order *= k
        self.starting_balance = balance

    @classmethod
    def from_env(cls) -> BotConfig:
        cfg = cls()
        cfg.mode = os.getenv("TPB_MODE", cfg.mode)
        cfg.latency_profile = os.getenv("TPB_LATENCY_PROFILE", cfg.latency_profile)
        cfg.latency_seed = int(os.getenv("TPB_LATENCY_SEED", cfg.latency_seed))
        cfg.starting_balance = float(
            os.getenv("TPB_STARTING_BALANCE", cfg.starting_balance)
        )
        cfg.log_level = os.getenv("TPB_LOG_LEVEL", cfg.log_level)
        if cfg.mode != "paper":
            raise SystemExit(
                "live mode is not implemented. Run paper until the evidence "
                "statistic in the dashboard shows edge net of fees."
            )
        return cfg
