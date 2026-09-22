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
from .market.discovery import CANDIDATE_ASSETS, parse_durations
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
    #: Window lengths to trade, in minutes, primary first. The venue lists 5m
    #: and 15m (``TPB_WINDOW_MINUTES`` in .env). The default is 5 so an
    #: unconfigured run behaves exactly as it always has.
    durations_min: tuple[int, ...] = (5,)
    reprobe_s: float = 1800.0
    max_quote_age_ms: float = 3000.0
    #: Feed the estimators at most this often per asset; three exchanges push
    #: far more ticks than a 1 s vol grid or a 60 s TWAP can use.
    model_update_interval_ms: float = 100.0


@dataclass(slots=True)
class LiveConfig:
    """Real-money switches. Credentials are NOT here: they are read from the
    environment by execution/polymarket.py and never stored in a config."""
    armed: bool = False                   # False = dry run: sign, never post
    max_order_usdc: float = 5.0
    max_open_usdc: float = 25.0
    max_daily_loss_usdc: float = 10.0
    max_orders_per_hour: int = 60
    kill_file: str = "data/KILL"


#: Parameter overrides per window length, keyed by dotted path. Every default
#: in this module was tuned on 5-minute windows; a 15-minute window is a
#: different market and these are the values that a month of its history
#: supports (scripts/research_15m.py, docs/strategy-15m.md, 2026-08-22 ..
#: 2026-09-22, 10,560 windows, seven assets). Applied ONCE at startup for the
#: primary duration, before CLI flags and before the dashboard's control file,
#: so anything the operator sets still wins. What is deliberately NOT here:
#: the vol estimator (the 5m->15m variance ratio measured 0.93..1.00, so
#: sqrt(t) carries), the pricer's tails (t(3) vs t(4) differ in the fourth
#: decimal of the Brier score), Kelly and the loss limits.
DURATION_PROFILES: dict[int, dict[str, object]] = {
    5: {},
    15: {
        # Lean harder on the market. The blend beat both the model and the
        # market at every time bucket (calibration), and in the paper rule
        # 0.7 was positive in BOTH validation and test halves where 0.5 was
        # positive only in validation.
        # Seconds-left when trading may start, in the 15m window's own clock:
        # 5 s after the open, the same delay as 295 of 300 at 5m.
        "engine.trade_window_start_s": 895.0,
        "engine.market_blend": 0.7,
        # 0.02 (the 5m value) lost money in the test half at 15m; 0.05 is the
        # smallest required edge that was positive in both halves, and its
        # PnL rose as the spot handicap shrank toward live conditions.
        "engine.min_net_edge": 0.05,
        # 53% of books are already one-sided with 60..180s left and 78% in
        # the last minute; the last two minutes are structurally untradeable
        # for a taker.
        "engine.trade_window_end_s": 120.0,
        # Taking profit at +0.05 gave away ~0.08/share (t = -22): 89% of
        # eventual winners touch +0.05 on their way to 1.0, and the exit pays
        # a second fee. Measured, as the 5m note asked. Off.
        "engine.take_profit_enabled": False,
        # A 15m token book that has not printed for four seconds is quiet,
        # not stale; at 2.5s the live run rejected most evaluations as
        # STALE_DATA. The FOK limit protects against a moved price.
        "engine.max_book_age_ms": 5000.0,
        # Never try to sell into the last two minutes for the same reason as
        # the window end above.
        "engine.take_profit_min_secs_left": 120.0,
        # Scale in. A 15m window is long enough for the edge to be there more
        # than once; three same-side entries at least 45 s apart, inside the
        # unchanged per-market cap. Judgment, not measured: the study cannot
        # see fills. 5m stays at one entry.
        "risk.max_entries_per_market": 3,
        "risk.reentry_cooldown_s": 45.0,
    },
}


def _set_path(root, path: str, value) -> None:
    holder, attr = root, path
    if "." in path:
        prefix, attr = path.rsplit(".", 1)
        for part in prefix.split("."):
            holder = getattr(holder, part)
    if not hasattr(holder, attr):
        raise AttributeError(path)
    setattr(holder, attr, value)


@dataclass(slots=True)
class BotConfig:
    mode: str = "paper"                   # paper | live
    latency_profile: str = "home_broadband"
    latency_seed: int = 7
    starting_balance: float = 100.0

    strategy: StrategyConfig = field(default_factory=StrategyConfig)   # legacy sim
    fees: FeeConfig = field(default_factory=FeeConfig)                 # legacy sim
    engine: EngineConfig = field(default_factory=EngineConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    vol: VolConfig = field(default_factory=VolConfig)
    feeds: FeedConfig = field(default_factory=FeedConfig)
    live: LiveConfig = field(default_factory=LiveConfig)

    record_dir: str = "data/recordings"
    chart_dir: str = "data/charts"
    log_level: str = "INFO"
    #: what apply_duration_profile changed, for the startup log
    profile_applied: list[str] = field(default_factory=list)

    def apply_durations(self, durations: tuple[int, ...]) -> None:
        """Set the window lengths to trade and everything derived from them.

        Only two things downstream care about the duration, because the pricer
        already takes the horizon from the market itself
        (``window_s = close_ts - open_ts``):

        * ``feeds.durations_min`` -- which slugs discovery constructs.
        * ``engine.correlation_bucket_s`` -- the epoch cap groups positions
          that are ONE bet. Two assets in the same window always were; with
          both durations live, a 15m window and the three 5m windows inside it
          are the same bet on the same spot path, so they must share a bucket
          or the correlated-exposure cap is silently three times looser than
          it reads. Bucketing at the LONGEST duration does that, and for a
          single duration it is a 1:1 relabelling of today's epoch key --
          identical grouping, so 5m-only behaviour does not move.
        """
        if not durations:
            return
        self.feeds.durations_min = tuple(durations)
        self.engine.correlation_bucket_s = float(max(durations) * 60)
        # the timing knobs are read in seconds-left of the PRIMARY window, so
        # the dashboard shows 895 of 900 at 15m rather than a 5m-referenced
        # number nobody can relate to the clock; a second duration inherits
        # the same delay-after-open (engine.window_start_s)
        self.engine.window_reference_s = float(durations[0] * 60)

    def apply_duration_profile(self, durations: tuple[int, ...] | None = None) -> list[str]:
        """Overlay ``DURATION_PROFILES`` for the PRIMARY (first) duration.

        One engine config serves every market, so with ``5,15`` the profile of
        whichever is listed first applies to both -- list the one you care
        about first. Returns the changes as ``path: old -> new`` for the log.
        """
        durations = tuple(durations or self.feeds.durations_min)
        if not durations:
            return []
        changes: list[str] = []
        for path, value in DURATION_PROFILES.get(int(durations[0]), {}).items():
            holder, attr = self, path
            if "." in path:
                prefix, attr = path.rsplit(".", 1)
                for part in prefix.split("."):
                    holder = getattr(holder, part)
            before = getattr(holder, attr)
            if before != value:
                _set_path(self, path, value)
                changes.append(f"{path}: {before!r} -> {value!r}")
        return changes

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
    def from_env(cls, durations: str | None = None) -> BotConfig:
        """``durations`` is a CLI override ("15", "5,15") that takes precedence
        over TPB_WINDOW_MINUTES; it is resolved BEFORE the profile is applied
        so a 5m run started under a 15m .env gets 5m parameters, not both."""
        cfg = cls()
        cfg.mode = os.getenv("TPB_MODE", cfg.mode)
        cfg.latency_profile = os.getenv("TPB_LATENCY_PROFILE", cfg.latency_profile)
        cfg.latency_seed = int(os.getenv("TPB_LATENCY_SEED", cfg.latency_seed))
        cfg.starting_balance = float(
            os.getenv("TPB_STARTING_BALANCE", cfg.starting_balance)
        )
        cfg.log_level = os.getenv("TPB_LOG_LEVEL", cfg.log_level)
        # TPB_DURATIONS is accepted as an alias; TPB_WINDOW_MINUTES wins.
        spec = os.getenv("TPB_WINDOW_MINUTES") or os.getenv("TPB_DURATIONS")
        source = "TPB_WINDOW_MINUTES"
        if durations and str(durations).strip():
            spec, source = durations, "--durations"
        try:
            cfg.apply_durations(parse_durations(spec, cfg.feeds.durations_min))
        except ValueError as exc:
            raise SystemExit(f"{source}: {exc}") from None
        cfg.profile_applied = cfg.apply_duration_profile()
        if cfg.mode not in ("paper", "live"):
            raise SystemExit(f"TPB_MODE must be paper or live, not {cfg.mode!r}")
        if cfg.mode == "live":
            # live is dry-run unless explicitly acknowledged; the CLI enforces
            # the same rule for --armed
            cfg.live.armed = os.getenv("TPB_LIVE_ACK", "").strip() == "I_UNDERSTAND_REAL_MONEY"
        return cfg
