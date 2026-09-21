"""A window that never resolves must still leave a mark in the ledger.

When the venue outcome never arrives and we have no TWAP of our own to fall
back on, settlement gives up: `stats.unresolvable` ticks and the positions are
abandoned. It used to return without writing anything, so the dashboard never
learned the window had ended and every fill on it rendered "open" forever --
26% of filled markets in one 4-hour run.
"""
from __future__ import annotations

import pytest

from troll_poly_bot.live import LiveBot, LiveMarket
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.types import Market, Position

SLUG = "btc-updown-5m-1"


def _bot(tmp_path):
    return LiveBot(assets=("BTC",),
                   state_path=str(tmp_path / "state.json"),
                   trade_log=str(tmp_path / "trades.jsonl"))


def _live_market(strike: float = 0.0):
    market = Market(condition_id="c", asset="BTC", yes_token_id="Y", no_token_id="N",
                    strike=strike, open_ts=0, close_ts=300_000,
                    tick_size=0.01, min_size=5.0, slug=SLUG)
    meta = MarketMeta(market=market, resolution_source="chainlink",
                      accepting_orders=True, duration_s=300)
    return LiveMarket(meta=meta)


async def _run_unresolvable(bot, lm, *, shares: float):
    """Drive _settle down the give-up path: no venue outcome, no TWAP."""
    bot._venue_outcome = lambda slug: _none()
    if shares:
        bot.exchange.positions["Y"] = _Pos(shares)
    lm.settle_attempts = bot.SETTLE_MAX_ATTEMPTS      # retries already spent
    await bot._settle(SLUG, lm)


async def _none():
    return None


def _Pos(shares, avg=0.40, fees=0.0):
    """A real Position: abandoning one has to read its cost basis and fees."""
    return Position(token_id="Y", shares=shares,
                    cost_basis=shares * avg, fees_paid=fees)


@pytest.mark.asyncio
async def test_unresolvable_window_emits_a_settle_row(tmp_path):
    bot = _bot(tmp_path)
    lm = _live_market()
    await _run_unresolvable(bot, lm, shares=10.0)

    rows = [r for r in bot.ledger if r["event"] == "settle"]
    assert len(rows) == 1, "an abandoned window must still be reported"
    assert rows[0]["slug"] == SLUG
    assert rows[0]["unresolved"] is True
    assert rows[0]["marked"] is True, "the PnL is a mark, not a settlement"


@pytest.mark.asyncio
async def test_unresolvable_row_reaches_the_trade_log(tmp_path):
    bot = _bot(tmp_path)
    await _run_unresolvable(bot, _live_market(), shares=10.0)
    assert '"unresolved": true' in bot.trade_log.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_no_row_when_we_never_held_the_market(tmp_path):
    """Nothing was filled, so there is no 'open' row needing correction."""
    bot = _bot(tmp_path)
    await _run_unresolvable(bot, _live_market(), shares=0.0)
    assert [r for r in bot.ledger if r["event"] == "settle"] == []


@pytest.mark.asyncio
async def test_window_is_marked_settled_and_counted(tmp_path):
    bot = _bot(tmp_path)
    lm = _live_market()
    await _run_unresolvable(bot, lm, shares=10.0)
    assert lm.settled is True
    assert bot.stats.unresolvable == 1


@pytest.mark.asyncio
async def test_the_risk_budget_is_handed_back(tmp_path):
    """THE bug: a window that never resolves used to hold its slot forever.

    Ten of them in one ten-minute venue outage took the bot to
    max_concurrent_positions and it bought nothing for the next twenty hours,
    refusing every trade with RISK_LIMIT while looking perfectly healthy.
    """
    bot = _bot(tmp_path)
    bot.risk.on_fill(SLUG, "BTC", 1, "UP", 4.0, 10.0)
    assert bot.risk.open, "precondition: we are holding risk on this window"

    await _run_unresolvable(bot, _live_market(), shares=10.0)

    assert SLUG not in bot.risk.open, "the slot must be released"
    assert bot.risk.exposure_total() == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_the_position_is_flattened_at_its_last_mark(tmp_path):
    bot = _bot(tmp_path)
    bot._last_mark["Y"] = 0.60
    before = bot.exchange.balance
    await _run_unresolvable(bot, _live_market(), shares=10.0)

    pos = bot.exchange.positions["Y"]
    assert (pos.shares, pos.cost_basis, pos.fees_paid) == (0.0, 0.0, 0.0)
    # 10 shares marked at 0.60 come back as cash; cost basis was 10 * 0.40
    assert bot.exchange.balance == pytest.approx(before + 6.0)
    assert bot.ledger[-1]["pnl"] == pytest.approx(6.0 - 4.0)


@pytest.mark.asyncio
async def test_a_marked_guess_is_kept_out_of_the_evidence_statistic(tmp_path):
    """epoch_pnl drives the t-stat. An invented number must not reach it."""
    bot = _bot(tmp_path)
    bot._last_mark["Y"] = 0.60
    await _run_unresolvable(bot, _live_market(), shares=10.0)
    assert bot.epoch_pnl == {}
    assert (bot.stats.wins, bot.stats.losses, bot.stats.settled) == (0, 0, 0)
    assert bot.stats.unresolvable == 1


@pytest.mark.asyncio
async def test_reaping_an_unsettled_window_also_releases_it(tmp_path):
    """The safety net: nothing leaves _reap still on the risk book."""
    bot = _bot(tmp_path)
    lm = _live_market()
    lm.meta.market.close_ts = 0.0                 # long past reaping
    bot.markets[SLUG] = lm
    lm.books = {}
    bot.exchange.positions["Y"] = _Pos(10.0)
    bot.risk.on_fill(SLUG, "BTC", 1, "UP", 4.0, 10.0)

    bot._reap(now_ms=10_000_000.0)

    assert SLUG not in bot.markets
    assert SLUG not in bot.risk.open, "reaped while still holding risk"
    assert bot.ledger[-1]["reason"] == "reaped unsettled"


@pytest.mark.asyncio
async def test_retries_are_not_cut_short(tmp_path):
    """Before the attempts are spent it must retry, not declare it unresolved."""
    bot = _bot(tmp_path)
    lm = _live_market()
    bot._venue_outcome = lambda slug: _none()
    bot.exchange.positions["Y"] = _Pos(10.0)
    lm.settle_attempts = 0
    await bot._settle(SLUG, lm)
    assert lm.settled is False
    assert bot.ledger == []
