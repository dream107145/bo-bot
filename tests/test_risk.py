"""Risk limits: caps bind, correlated bets shrink, losses halt, never size up after a loss."""
from __future__ import annotations

import pytest

from troll_poly_bot.risk.limits import RiskConfig, RiskManager


def _rm(**kw):
    return RiskManager(cfg=RiskConfig(**kw), starting_balance=100.0)


def test_no_edge_no_size():
    rm = _rm()
    shares, why = rm.size(p=0.5, price=0.5, confidence=1.0, balance=100, asset="BTC", epoch=1, side="UP",
                          available_shares=1000)
    assert shares == 0.0 and "kelly" in why


def test_kelly_and_position_cap():
    rm = _rm(max_position_usdc=10.0)
    # f* = (0.9-0.5)/(0.5) = 0.8 -> stake 100*0.25*0.8 = 20 -> capped at 10 -> 20 shares
    shares, why = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100, asset="BTC", epoch=1, side="UP",
                          available_shares=1000)
    assert shares == pytest.approx(20.0) and why == "position cap"


def test_confidence_scales_and_depth_caps():
    rm = _rm(max_position_usdc=100.0, max_total_exposure_usdc=100.0, max_asset_exposure_usdc=100.0,
             max_epoch_exposure_usdc=100.0, max_shares_per_order=1000)
    full, _ = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100, asset="BTC", epoch=1, side="UP",
                      available_shares=1000)
    half, _ = rm.size(p=0.9, price=0.5, confidence=0.5, balance=100, asset="BTC", epoch=1, side="UP",
                      available_shares=1000)
    assert half == pytest.approx(full / 2)
    thin, _ = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100, asset="BTC", epoch=1, side="UP",
                      available_shares=20)
    assert thin == pytest.approx(10.0)          # half the resting depth


def test_same_direction_in_epoch_is_scaled_by_correlation():
    rm = _rm(max_position_usdc=100.0, max_epoch_exposure_usdc=100.0, max_asset_exposure_usdc=100.0,
             max_total_exposure_usdc=100.0, epoch_correlation=0.5)
    alone, _ = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100, asset="ETH", epoch=7, side="UP",
                       available_shares=1000)
    rm.on_fill("btc-7", "BTC", 7, "UP", usdc=5.0, shares=10.0)
    with_peer, _ = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100, asset="ETH", epoch=7, side="UP",
                           available_shares=1000)
    assert with_peer == pytest.approx(alone / 2)


def test_exposure_caps_in_check():
    rm = _rm(max_epoch_exposure_usdc=8.0, max_position_usdc=10.0)
    rm.on_fill("btc-1", "BTC", 1, "UP", usdc=5.0, shares=10.0)
    assert rm.check("btc-1", "BTC", 1, 1.0).reason == "ALREADY_POSITIONED"
    v = rm.check("eth-1", "ETH", 1, 4.0)
    assert not v.ok and "epoch" in v.detail
    assert rm.check("eth-2", "ETH", 2, 4.0).ok


def test_daily_loss_and_drawdown_halt():
    rm = _rm(max_daily_loss_usdc=5.0, max_drawdown_usdc=100.0)
    rm.on_fill("a", "BTC", 1, "UP", 5.0, 10.0)
    rm.on_settle("a", -6.0)
    assert not rm.halted().ok and "daily" in rm.halted().detail
    rm2 = _rm(max_daily_loss_usdc=100.0, max_drawdown_usdc=5.0)
    rm2.on_fill("a", "BTC", 1, "UP", 5.0, 10.0)
    rm2.on_settle("a", +10.0)
    rm2.on_fill("b", "BTC", 2, "UP", 5.0, 10.0)
    rm2.on_settle("b", -6.0)
    assert not rm2.halted().ok and "drawdown" in rm2.halted().detail


def test_size_never_grows_after_a_loss():
    rm = _rm()
    before, _ = rm.size(p=0.8, price=0.5, confidence=1.0, balance=100, asset="BTC", epoch=1, side="UP",
                        available_shares=1000)
    rm.on_fill("a", "BTC", 1, "UP", 5.0, 10.0)
    rm.on_settle("a", -5.0)
    after, _ = rm.size(p=0.8, price=0.5, confidence=1.0, balance=95, asset="BTC", epoch=2, side="UP",
                       available_shares=1000)
    assert after <= before



# ───────────────────────────── scaling in to one market ────────────────────

def _rm(**kw):
    from troll_poly_bot.risk.limits import RiskConfig, RiskManager
    return RiskManager(RiskConfig(**kw), starting_balance=100.0)


def test_default_is_one_entry_per_window():
    rm = _rm()
    rm.on_fill("btc-updown-15m-1", "BTC", 1, "UP", 2.0, 4.0, now_s=1000.0)
    v = rm.check("btc-updown-15m-1", "BTC", 1, 2.0, side="UP", now_s=2000.0)
    assert not v.ok and v.reason == "ALREADY_POSITIONED"


def test_scale_in_same_side_after_cooldown_within_cap():
    rm = _rm(max_entries_per_market=3, reentry_cooldown_s=45.0, max_position_usdc=5.0)
    rm.on_fill("s", "BTC", 1, "UP", 2.0, 4.0, now_s=1000.0)
    assert not rm.check("s", "BTC", 1, 2.0, side="UP", now_s=1010.0).ok        # cooldown
    assert not rm.check("s", "BTC", 1, 2.0, side="DOWN", now_s=1100.0).ok      # other side
    assert rm.check("s", "BTC", 1, 2.0, side="UP", now_s=1100.0).ok             # add
    assert rm.position_headroom("s") == 3.0
    v = rm.check("s", "BTC", 1, 3.5, side="UP", now_s=1100.0)
    assert not v.ok and v.detail == "position cap"                              # cap counts what we hold
    rm.on_fill("s", "BTC", 1, "UP", 2.0, 4.0, now_s=1100.0)
    rm.on_fill("s", "BTC", 1, "UP", 1.0, 2.0, now_s=1200.0)
    assert rm.open["s"].entries == 3 and rm.open["s"].usdc == 5.0
    v = rm.check("s", "BTC", 1, 0.5, side="UP", now_s=2000.0)
    assert not v.ok and v.reason == "ALREADY_POSITIONED"                        # entries exhausted


def test_add_does_not_consume_a_concurrent_slot():
    rm = _rm(max_entries_per_market=2, reentry_cooldown_s=0.0, max_concurrent_positions=1)
    rm.on_fill("a", "BTC", 1, "UP", 1.0, 2.0, now_s=0.0)
    assert not rm.check("b", "ETH", 1, 1.0, side="UP", now_s=1.0).ok            # a NEW market is refused
    assert rm.check("a", "BTC", 1, 1.0, side="UP", now_s=1.0).ok                # adding to the held one is not


def test_size_uses_remaining_headroom_for_a_held_market():
    rm = _rm(max_entries_per_market=3, max_position_usdc=5.0, min_order_usdc=1.0, kelly_fraction=1.0)
    rm.on_fill("s", "BTC", 1, "UP", 4.0, 8.0, now_s=0.0)
    shares, note = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100.0, asset="BTC", epoch=1,
                           side="UP", available_shares=1000.0, min_size=1.0, slug="s")
    assert note == "position cap" and shares == pytest.approx(2.0)             # $1 left / 0.5
    shares2, _ = rm.size(p=0.9, price=0.5, confidence=1.0, balance=100.0, asset="BTC", epoch=1,
                         side="UP", available_shares=1000.0, min_size=1.0)      # no slug: full cap
    assert shares2 > shares
