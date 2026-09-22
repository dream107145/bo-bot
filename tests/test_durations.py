"""15-minute windows, and the promise that 5-minute behaviour did not move.

The venue lists `<asset>-updown-5m-<epoch/300>` and `<asset>-updown-15m-
<epoch/900>` for the same seven assets. Everything below the slug is already
duration-agnostic -- the pricer takes its horizon from the market's own
open/close -- so these tests pin the three places that are not: the slug and
its alignment, the trade-window gate, and the correlated-exposure bucket.
"""
from __future__ import annotations

import asyncio

import pytest

from troll_poly_bot.config import BotConfig
from troll_poly_bot.feeds.markets import parse_market
from troll_poly_bot.feeds.polymarket import (
    SUPPORTED_DURATIONS_MIN, duration_tag, slug_for, window_epoch, window_seconds,
)
from troll_poly_bot.market.discovery import (
    AssetRegistry, parse_durations, slug_for_epoch,
)
from troll_poly_bot.risk.limits import RiskManager
from troll_poly_bot.strategy.engine import EngineConfig, StrategyEngine, window_start_s

# 2026-09-22 14:49:45 UTC. 14:45 is a valid open for both durations, which is
# exactly the case that hides an alignment bug -- so the offsets are checked too.
NOW = 1790088585.0


# ───────────────────────────── slugs and alignment ─────────────────────────

def test_window_seconds():
    assert window_seconds(5) == 300
    assert window_seconds(15) == 900


def test_5m_alignment_unchanged():
    assert window_epoch(NOW) == 1790088300
    assert window_epoch(NOW, 1) == 1790088600
    assert window_epoch(NOW, -1) == 1790088000
    assert slug_for("BTC", NOW) == "btc-updown-5m-1790088300"


def test_15m_aligns_to_900_not_to_three_5m_windows():
    assert window_epoch(NOW, duration_min=15) == 1790088300     # 14:45
    assert window_epoch(NOW, 1, duration_min=15) == 1790089200  # 15:00, not 14:50
    assert window_epoch(NOW, -1, duration_min=15) == 1790087400  # 14:30
    assert slug_for("BTC", NOW, duration_min=15) == "btc-updown-15m-1790088300"


def test_15m_epoch_is_not_just_the_5m_epoch():
    """At 14:52 the two durations disagree: 14:50 vs 14:45."""
    t = 1790088720.0                                  # 14:52:00Z
    assert window_epoch(t) == 1790088600              # 14:50
    assert window_epoch(t, duration_min=15) == 1790088300   # 14:45
    assert duration_tag(15) == "15m"


def test_slug_for_epoch_carries_the_duration():
    assert slug_for_epoch("ETH", 1790088300) == "eth-updown-5m-1790088300"
    assert slug_for_epoch("ETH", 1790088300, 15) == "eth-updown-15m-1790088300"


# ───────────────────────────── parsing the .env ────────────────────────────

@pytest.mark.parametrize("spec,want", [
    ("15", (15,)),
    ("5", (5,)),
    ("5,15", (5, 15)),
    ("15,5", (15, 5)),
    ("15m", (15,)),
    (" 5 , 15 ", (5, 15)),
    ("5,5,15", (5, 15)),
    ("", (5,)),
    (None, (5,)),
])
def test_parse_durations(spec, want):
    assert parse_durations(spec) == want


def test_parse_durations_refuses_an_unlisted_window():
    # 10m/30m/1h were probed and do not exist. Failing loudly beats a bot that
    # runs all day constructing slugs the venue never answers.
    with pytest.raises(ValueError):
        parse_durations("10")
    with pytest.raises(ValueError):
        parse_durations("banana")
    assert SUPPORTED_DURATIONS_MIN == (5, 15)


def test_env_drives_the_window(monkeypatch):
    monkeypatch.setenv("TPB_MODE", "paper")
    monkeypatch.setenv("TPB_WINDOW_MINUTES", "15")
    cfg = BotConfig.from_env()
    assert cfg.feeds.durations_min == (15,)
    assert cfg.engine.correlation_bucket_s == 900.0


def test_env_default_is_still_5m(monkeypatch):
    monkeypatch.delenv("TPB_WINDOW_MINUTES", raising=False)
    monkeypatch.delenv("TPB_DURATIONS", raising=False)
    monkeypatch.setenv("TPB_MODE", "paper")
    cfg = BotConfig.from_env()
    assert cfg.feeds.durations_min == (5,)
    assert cfg.engine.correlation_bucket_s == 300.0


def test_env_rejects_a_bad_window(monkeypatch):
    monkeypatch.setenv("TPB_MODE", "paper")
    monkeypatch.setenv("TPB_WINDOW_MINUTES", "7")
    with pytest.raises(SystemExit):
        BotConfig.from_env()


# ───────────────────────────── the trade window ────────────────────────────

def test_trade_window_start_is_unchanged_at_5m():
    cfg = EngineConfig()
    assert window_start_s(cfg, 300.0) == pytest.approx(295.0)


def test_trade_window_start_scales_to_15m():
    """The knob means "wait 5s after the open", not "295 seconds left".

    Read literally, a 295 start would discard the first ten minutes of every
    15m window -- two thirds of the tradeable time, and the two thirds where
    the book is widest and the model's disagreement largest.
    """
    cfg = EngineConfig()
    assert window_start_s(cfg, 900.0) == pytest.approx(895.0)


def test_trade_window_start_respects_a_tuned_value():
    cfg = EngineConfig(trade_window_start_s=200.0)     # skip the first 100s
    assert window_start_s(cfg, 300.0) == pytest.approx(200.0)
    assert window_start_s(cfg, 900.0) == pytest.approx(800.0)


def test_trade_window_end_stays_absolute():
    # A book goes one-sided because expiry is near, not because the window was
    # short: the last minute is dead at both durations.
    cfg = EngineConfig()
    assert cfg.trade_window_end_s == 60.0


# ───────────────────────── correlated-exposure bucket ──────────────────────

def _engine(bucket_s: float) -> StrategyEngine:
    return StrategyEngine(EngineConfig(correlation_bucket_s=bucket_s), RiskManager())


def test_bucket_groups_the_same_window_at_one_duration():
    eng = _engine(300.0)
    a = eng.correlation_epoch(1790088300 * 1000.0)     # btc 14:45 5m
    b = eng.correlation_epoch(1790088300 * 1000.0)     # eth 14:45 5m
    c = eng.correlation_epoch(1790088600 * 1000.0)     # btc 14:50 5m
    assert a == b and a != c                           # unchanged from before


def test_bucket_joins_a_15m_window_to_the_5m_windows_inside_it():
    # 14:45 15m spans 14:45, 14:50 and 14:55 at 5m. All one bet on one spot
    # path, so the per-epoch cap must see them as one.
    eng = _engine(900.0)
    fifteen = eng.correlation_epoch(1790088300 * 1000.0)
    assert eng.correlation_epoch(1790088300 * 1000.0) == fifteen   # 14:45
    assert eng.correlation_epoch(1790088600 * 1000.0) == fifteen   # 14:50
    assert eng.correlation_epoch(1790088900 * 1000.0) == fifteen   # 14:55
    assert eng.correlation_epoch(1790089200 * 1000.0) != fifteen   # 15:00


def test_apply_durations_sets_the_bucket_to_the_longest():
    cfg = BotConfig()
    cfg.apply_durations((5, 15))
    assert cfg.engine.correlation_bucket_s == 900.0
    cfg.apply_durations((5,))
    assert cfg.engine.correlation_bucket_s == 300.0


# ───────────────────────────── discovery probing ───────────────────────────

def _row(slug: str, duration_min: int) -> dict:
    epoch = int(slug.rsplit("-", 1)[1])
    end = epoch + duration_min * 60
    import datetime as dt
    return {
        "slug": slug,
        "conditionId": "0xabc",
        "clobTokenIds": '["11","22"]',
        "outcomes": '["Up","Down"]',
        "endDate": dt.datetime.fromtimestamp(end, dt.timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "orderPriceMinTickSize": "0.01",
        "orderMinSize": "5",
        "acceptingOrders": True,
        "cryptoMarketConfig": {"asset": slug.split("-")[0], "duration": f"{duration_min}m",
                               "twapEnabled": True, "twapLookbackSeconds": 60},
    }


def test_registry_probes_each_duration_at_its_own_epoch():
    asked: list[str] = []

    async def fetch(slug: str):
        asked.append(slug)
        return {"slug": slug} if slug.startswith("btc") else None

    reg = AssetRegistry(candidates=("BTC", "ETH"), durations=(5, 15))
    assert asyncio.run(reg.refresh(fetch, NOW)) == ("BTC",)
    assert "btc-updown-5m-1790088300" in asked
    assert "btc-updown-15m-1790088300" in asked
    assert reg.assets_for(5) == ("BTC",)
    assert reg.assets_for(15) == ("BTC",)


def test_asset_listed_at_only_one_duration_is_not_probed_at_the_other():
    """HYPE-style case: listed at 5m, absent at 15m.

    It must stay tradeable at 5m and never produce a 15m slug, or every
    discovery pass burns a request on a market that does not exist.
    """
    async def fetch(slug: str):
        if slug.startswith("sol") and "15m" in slug:
            return None
        return {"slug": slug}

    reg = AssetRegistry(candidates=("BTC", "SOL"), durations=(5, 15))
    assert asyncio.run(reg.refresh(fetch, NOW)) == ("BTC", "SOL")
    assert reg.assets_for(5) == ("BTC", "SOL")
    assert reg.assets_for(15) == ("BTC",)


def test_cached_row_never_returns_another_window():
    async def fetch(slug: str):
        return {"slug": slug}

    reg = AssetRegistry(candidates=("BTC",), durations=(15,))
    asyncio.run(reg.refresh(fetch, NOW))
    assert reg.cached_row("btc-updown-15m-1790088300") is not None
    assert reg.cached_row("btc-updown-15m-1790089200") is None     # the NEXT one


def test_failed_probe_carries_the_asset_over_per_duration():
    from troll_poly_bot.market.discovery import FetchFailed

    state = {"fail": False}

    async def fetch(slug: str):
        if state["fail"]:
            raise FetchFailed("dns")
        return {"slug": slug}

    reg = AssetRegistry(candidates=("BTC",), durations=(5, 15))
    asyncio.run(reg.refresh(fetch, NOW))
    assert reg.assets_for(15) == ("BTC",)
    state["fail"] = True
    asyncio.run(reg.refresh(fetch, NOW + 60))
    # a probe that could not be made says nothing; it must not delist
    assert reg.assets == ("BTC",)
    assert reg.assets_for(15) == ("BTC",)


# ───────────────────────────── market parsing ──────────────────────────────

def test_parse_market_reads_a_15m_window():
    meta = parse_market(_row("btc-updown-15m-1790088300", 15))
    assert meta is not None
    assert meta.duration_s == 900.0
    assert meta.market.close_ts - meta.market.open_ts == 900_000.0


def test_parse_market_still_reads_a_5m_window():
    meta = parse_market(_row("btc-updown-5m-1790088300", 5))
    assert meta is not None
    assert meta.duration_s == 300.0


def test_parse_market_rejects_a_slug_whose_end_disagrees():
    # A 15m slug whose endDate is 5 minutes later means one of the two
    # assumptions is wrong; every horizon derived from it would be wrong too.
    row = _row("btc-updown-15m-1790088300", 15)
    row["endDate"] = "2026-09-22T14:50:00Z"
    assert parse_market(row) is None


# ───────────────────────────── the 15m parameter profile ───────────────────

from troll_poly_bot.config import DURATION_PROFILES


def test_5m_profile_is_empty_so_nothing_moves():
    assert DURATION_PROFILES[5] == {}
    cfg = BotConfig()
    before = (cfg.engine.market_blend, cfg.engine.min_net_edge, cfg.engine.trade_window_end_s,
              cfg.engine.take_profit_enabled, cfg.engine.max_book_age_ms)
    assert cfg.apply_duration_profile((5,)) == []
    assert (cfg.engine.market_blend, cfg.engine.min_net_edge, cfg.engine.trade_window_end_s,
            cfg.engine.take_profit_enabled, cfg.engine.max_book_age_ms) == before


def test_15m_profile_values_are_the_measured_ones():
    cfg = BotConfig()
    changes = cfg.apply_duration_profile((15,))
    assert cfg.engine.market_blend == 0.7
    assert cfg.engine.min_net_edge == 0.05
    assert cfg.engine.trade_window_end_s == 120.0
    assert cfg.engine.take_profit_enabled is False
    assert cfg.engine.max_book_age_ms == 5000.0
    assert cfg.engine.take_profit_min_secs_left == 120.0
    assert len(changes) == len(DURATION_PROFILES[15])
    # what the study said to leave alone
    assert cfg.risk.kelly_fraction == BotConfig().risk.kelly_fraction
    assert cfg.vol.default_ratio == BotConfig().vol.default_ratio
    assert cfg.engine.stop_loss_delta == BotConfig().engine.stop_loss_delta


def test_profile_follows_the_primary_duration():
    a = BotConfig(); a.apply_duration_profile((15, 5))
    b = BotConfig(); b.apply_duration_profile((5, 15))
    assert a.engine.min_net_edge == 0.05
    assert b.engine.min_net_edge == 0.02


def test_every_profile_path_exists():
    cfg = BotConfig()
    for prof in DURATION_PROFILES.values():
        for path in prof:
            holder, attr = cfg, path
            if "." in path:
                prefix, attr = path.rsplit(".", 1)
                for part in prefix.split("."):
                    holder = getattr(holder, part)
            assert hasattr(holder, attr), path


def test_env_applies_the_15m_profile(monkeypatch):
    monkeypatch.setenv("TPB_MODE", "paper")
    monkeypatch.setenv("TPB_WINDOW_MINUTES", "15")
    cfg = BotConfig.from_env()
    assert cfg.engine.market_blend == 0.7 and cfg.engine.take_profit_enabled is False
    assert cfg.profile_applied


def test_cli_override_beats_env_before_the_profile(monkeypatch):
    """A 5m run started under a 15m .env must get 5m parameters, not the 15m
    overrides left over from the environment's profile."""
    monkeypatch.setenv("TPB_MODE", "paper")
    monkeypatch.setenv("TPB_WINDOW_MINUTES", "15")
    cfg = BotConfig.from_env(durations="5")
    assert cfg.feeds.durations_min == (5,)
    assert cfg.engine.market_blend == 0.5 and cfg.engine.min_net_edge == 0.02
    assert cfg.engine.take_profit_enabled is True
    assert cfg.profile_applied == []


def test_trade_window_at_15m_with_profile():
    # the real path: durations first (sets the reference window), then the
    # profile (sets the knob in that window's own seconds-left)
    cfg = BotConfig(); cfg.apply_durations((15,)); cfg.apply_duration_profile((15,))
    assert cfg.engine.window_reference_s == 900.0
    assert cfg.engine.trade_window_start_s == 895.0
    assert window_start_s(cfg.engine, 900.0) == pytest.approx(895.0)
    assert window_start_s(cfg.engine, 300.0) == pytest.approx(295.0)   # a 5m market alongside
    assert cfg.engine.trade_window_end_s == 120.0


def test_timing_knob_reads_in_the_traded_windows_clock(monkeypatch):
    """What the dashboard shows is what the bot compares the clock to."""
    from troll_poly_bot import control
    monkeypatch.setenv("TPB_MODE", "paper"); monkeypatch.setenv("TPB_WINDOW_MINUTES", "15")
    cfg = BotConfig.from_env()
    assert control.current_values(cfg)["engine.trade_window_start_s"] == 895.0
    rows = {r["key"]: r for r in control.spec(900.0)}
    assert rows["engine.trade_window_start_s"]["hi"] == 895.0
    assert rows["engine.trade_window_end_s"]["hi"] == 890.0
    assert "15-minute" in rows["engine.trade_window_start_s"]["help"]
    rows5 = {r["key"]: r for r in control.spec(300.0)}
    assert rows5["engine.trade_window_start_s"]["hi"] == 295.0
    # validation accepts a 15m value that the old 300 cap would have clamped
    clean, errors = control.validate({"engine.trade_window_start_s": 895})
    assert clean == {"engine.trade_window_start_s": 895.0} and not errors
