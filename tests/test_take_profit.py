"""Selling a risen position instead of holding it to the oracle print.

A 5m up/down market pays 1.0 or 0.0. A position that has moved our way is
worth something *now*, on the bid, and worth a coin flip at close. The exit
rule banks the first. These tests pin the arithmetic, because every one of
these decisions moves money and the failure mode -- exiting into a loss the
fee schedule made invisible -- looks like a win in the logs.

The fee here is the venue's real curve: 0.07 * p * (1 - p) per share.
"""
from __future__ import annotations

import pytest

from troll_poly_bot.execution.paper import FeeModel
from troll_poly_bot.live import LiveBot, LiveBook, LiveMarket
from troll_poly_bot.feeds.markets import MarketMeta
from troll_poly_bot.signals.costs import FeeSchedule
from troll_poly_bot.types import Fill, Market, Position, Side

SLUG = "btc-updown-5m-1"
YES, NO = "Y", "N"
CLOSE_MS = 300_000.0


def _bot(tmp_path, **engine):
    bot = LiveBot(assets=("BTC",),
                  state_path=str(tmp_path / "state.json"),
                  trade_log=str(tmp_path / "trades.jsonl"))
    bot.exchange.fees = FeeModel.from_schedule(FeeSchedule(rate=0.07))
    for k, v in engine.items():
        setattr(bot.cfg.engine, k, v)
    return bot


def _market(bot, *, bids=((0.60, 500.0),)):
    market = Market(condition_id="c", asset="BTC", yes_token_id=YES, no_token_id=NO,
                    strike=100.0, open_ts=0, close_ts=CLOSE_MS,
                    tick_size=0.01, min_size=5.0, slug=SLUG)
    lm = LiveMarket(meta=MarketMeta(market=market, resolution_source="chainlink",
                                    accepting_orders=True, duration_s=300))
    for token in (YES, NO):
        lm.books[token] = LiveBook(token)
    lm.books[YES].bids = {p: s for p, s in bids}
    bot.markets[SLUG] = lm
    return lm


def _hold(bot, *, shares=100.0, avg=0.50, fees=0.0):
    """Put us long `shares` of YES at `avg`, having paid `fees` to get there."""
    bot.exchange.positions[YES] = Position(token_id=YES, shares=shares,
                                           cost_basis=shares * avg, fees_paid=fees)


# ───────────────────────────── the exit decision ─────────────────────────────


def test_no_exit_before_the_rise_is_big_enough(tmp_path):
    bot = _bot(tmp_path, take_profit_delta=0.05)
    lm = _market(bot, bids=((0.53, 500.0),))          # +0.03 only
    _hold(bot, avg=0.50)
    assert bot._exit_candidate(lm, YES, 0.0) is None


def test_exit_once_the_bid_has_risen_enough(tmp_path):
    bot = _bot(tmp_path, take_profit_delta=0.05)
    lm = _market(bot, bids=((0.60, 500.0),))          # +0.10
    _hold(bot, shares=100.0, avg=0.50)
    ex = bot._exit_candidate(lm, YES, 0.0)
    assert ex is not None
    assert ex["bid"] == 0.60 and ex["size"] == 100.0
    # 100 sh * 0.10 gross, less 0.07*0.6*0.4 = 0.0168/sh exit fee
    assert ex["pnl"] == pytest.approx(10.0 - 1.68, abs=1e-6)


def test_the_price_tested_is_the_bid_not_the_ask(tmp_path):
    """We sell into the bid. A wide ask must not trigger an exit."""
    bot = _bot(tmp_path, take_profit_delta=0.05)
    lm = _market(bot, bids=((0.52, 500.0),))
    lm.books[YES].asks = {0.70: 500.0}
    _hold(bot, avg=0.50)
    assert bot._exit_candidate(lm, YES, 0.0) is None


def test_entry_fees_are_not_forgotten(tmp_path):
    """A rise that only covers the fees we already paid is not a profit."""
    bot = _bot(tmp_path, take_profit_delta=0.05, take_profit_min_net=0.005)
    lm = _market(bot, bids=((0.56, 500.0),))
    # gross +6.00; exit fee 0.07*0.56*0.44*100 = 1.72; entry fees 5.00
    _hold(bot, shares=100.0, avg=0.50, fees=5.00)
    assert bot._exit_candidate(lm, YES, 0.0) is None, "0.72 net is below 0.50"


def test_fee_eating_the_rise_blocks_the_exit(tmp_path):
    bot = _bot(tmp_path, take_profit_delta=0.01, take_profit_min_net=0.005)
    lm = _market(bot, bids=((0.51, 500.0),))
    _hold(bot, shares=100.0, avg=0.50)
    # +0.01/sh gross against a 0.07*0.51*0.49 = 0.0175/sh fee: a loss
    assert bot._exit_candidate(lm, YES, 0.0) is None
    assert bot.exec_rejections.get("EXIT_FEE_EATS_IT") == 1


def test_thin_bid_blocks_the_exit(tmp_path):
    """FOK is all-or-nothing; sending it without the depth just gets a reject."""
    bot = _bot(tmp_path, take_profit_delta=0.05)
    lm = _market(bot, bids=((0.60, 40.0),))
    _hold(bot, shares=100.0, avg=0.50)
    assert bot._exit_candidate(lm, YES, 0.0) is None
    assert bot.exec_rejections.get("EXIT_THIN_BID") == 1


def test_depth_is_summed_across_levels_at_or_above_the_touch(tmp_path):
    bot = _bot(tmp_path, take_profit_delta=0.05)
    lm = _market(bot, bids=((0.60, 40.0), (0.59, 80.0)))
    _hold(bot, shares=100.0, avg=0.50)
    # only 40 at the 0.60 touch, so the FOK at 0.60 cannot clear 100
    assert bot._exit_candidate(lm, YES, 0.0) is None


def test_empty_book_blocks_the_exit(tmp_path):
    bot = _bot(tmp_path, take_profit_delta=0.05)
    lm = _market(bot, bids=())
    _hold(bot, avg=0.50)
    assert bot._exit_candidate(lm, YES, 0.0) is None
    assert bot.exec_rejections.get("EXIT_NO_BID") == 1


def test_no_position_no_exit(tmp_path):
    bot = _bot(tmp_path)
    lm = _market(bot)
    assert bot._exit_candidate(lm, YES, 0.0) is None


# ─────────────────────────────── the scan gates ──────────────────────────────


def test_disabled_sends_nothing(tmp_path):
    bot = _bot(tmp_path, take_profit_enabled=False, take_profit_delta=0.05)
    lm = _market(bot)
    _hold(bot, avg=0.50)
    bot._take_profit(0.0)
    assert lm.inflight == 0


def test_enabled_sends_one_order(tmp_path):
    bot = _bot(tmp_path, take_profit_delta=0.05)
    lm = _market(bot)
    _hold(bot, avg=0.50)
    bot._take_profit(0.0)
    assert lm.inflight == 1


def test_no_exit_in_the_closing_seconds(tmp_path):
    """The order would land after close and be rejected."""
    bot = _bot(tmp_path, take_profit_delta=0.05, take_profit_min_secs_left=10.0)
    lm = _market(bot)
    _hold(bot, avg=0.50)
    bot._take_profit(CLOSE_MS - 5_000.0)               # 5s left
    assert lm.inflight == 0


def test_no_second_order_while_one_is_in_flight(tmp_path):
    bot = _bot(tmp_path, take_profit_delta=0.05)
    lm = _market(bot)
    _hold(bot, avg=0.50)
    bot._take_profit(0.0)
    bot._take_profit(0.0)
    assert lm.inflight == 1


def test_an_exited_market_is_left_alone(tmp_path):
    bot = _bot(tmp_path, take_profit_delta=0.05)
    lm = _market(bot)
    _hold(bot, avg=0.50)
    lm.exited = True
    bot._take_profit(0.0)
    assert lm.inflight == 0


# ──────────────────────────────── booking it ─────────────────────────────────


def _book_a_win(bot, *, shares=100.0, avg=0.50, entry_fees=1.75, exit_price=0.60):
    """Drive a full exit exactly as the exchange does: apply the sell to the
    position first, THEN book it. _book_exit reads what the fill left behind."""
    _hold(bot, shares=shares, avg=avg, fees=entry_fees)
    exit_fee = 0.07 * exit_price * (1 - exit_price) * shares
    fill = Fill(order_id="o", token_id=YES, side=Side.SELL, price=exit_price,
                size=shares, fee=exit_fee, exchange_ts=1000.0, ack_ts=1000.0,
                decision_ts=1000.0)
    bot.exchange.positions[YES].apply(fill)          # what _settle_fills did
    info = {"slug": SLUG, "asset": "BTC", "epoch": 1, "token_id": YES,
            "side": "UP", "exit": True}
    bot._book_exit(info, [fill], 1000.0)
    return shares * (exit_price - avg) - entry_fees - exit_fee


def test_realised_pnl_is_net_of_both_fees(tmp_path):
    bot = _bot(tmp_path)
    _market(bot)
    expected = _book_a_win(bot)
    assert bot.stats.realised_pnl == pytest.approx(expected, abs=1e-6)


def test_the_position_is_flattened(tmp_path):
    bot = _bot(tmp_path)
    _market(bot)
    _book_a_win(bot)
    pos = bot.exchange.positions[YES]
    assert (pos.shares, pos.cost_basis, pos.fees_paid) == (0.0, 0.0, 0.0)


def test_risk_exposure_is_released(tmp_path):
    """The point of the feature: the epoch's budget is free again."""
    bot = _bot(tmp_path)
    _market(bot)
    bot.risk.on_fill(SLUG, "BTC", 1, "UP", 50.0, 100.0)
    assert bot.risk.exposure_total() == pytest.approx(50.0)
    _book_a_win(bot)
    assert bot.risk.exposure_total() == pytest.approx(0.0)


def test_a_profitable_exit_counts_as_a_win(tmp_path):
    bot = _bot(tmp_path)
    _market(bot)
    _book_a_win(bot)
    assert (bot.stats.wins, bot.stats.losses, bot.stats.settled) == (1, 0, 1)


def test_the_market_is_marked_exited(tmp_path):
    bot = _bot(tmp_path)
    lm = _market(bot)
    expected = _book_a_win(bot)
    assert lm.exited is True
    assert lm.pnl == pytest.approx(expected, abs=1e-6)


def test_both_ledger_rows_are_written(tmp_path):
    bot = _bot(tmp_path)
    _market(bot)
    _book_a_win(bot)
    kinds = [(r["event"], r.get("action") or r.get("exited")) for r in bot.ledger]
    assert kinds == [("fill", "SELL"), ("settle", True)]
    assert bot.ledger[0]["cost"] < 0, "a sale is a credit, not a cost"
    assert bot.ledger[1]["pnl"] == pytest.approx(bot.stats.realised_pnl, abs=1e-4)


def test_epoch_pnl_records_the_exit(tmp_path):
    """The evidence statistic clusters by epoch; an exit is part of that bet."""
    bot = _bot(tmp_path)
    _market(bot)
    expected = _book_a_win(bot)
    assert bot.epoch_pnl[1] == pytest.approx(expected, abs=1e-6)


def test_settlement_after_an_exit_does_not_book_it_twice(tmp_path):
    """had_position is False once flat, so settlement adds nothing."""
    bot = _bot(tmp_path)
    lm = _market(bot)
    expected = _book_a_win(bot)
    had_position = any(
        abs(bot.exchange.positions[t].shares) > 1e-9
        for t in (YES, NO) if t in bot.exchange.positions)
    assert had_position is False
    assert bot.stats.realised_pnl == pytest.approx(expected, abs=1e-6)
    assert lm.pnl == pytest.approx(expected, abs=1e-6)


# ───────────────────────── end to end, through the exchange ──────────────────


def test_round_trip_through_the_real_exchange(tmp_path):
    """Submit -> match on the bid -> ack -> book. No stubs in the middle.

    The unit tests above pin each piece; this one proves they are actually
    wired together, which is where a feature like this really breaks.
    """
    bot = _bot(tmp_path, take_profit_delta=0.05, take_profit_min_secs_left=1.0)
    lm = _market(bot, bids=((0.60, 500.0),))
    bot.token_index[YES] = (YES, lm)
    bot.token_index[NO] = (NO, lm)
    _hold(bot, shares=100.0, avg=0.50, fees=1.75)
    start_balance = bot.exchange.balance

    bot._take_profit(0.0)
    assert lm.inflight == 1, "an exit order should have been sent"

    # advance past submit + match + ack latency
    for t in range(0, 20_001, 250):
        for res in bot.exchange.step(float(t), bot._true_book, bot._close_ts):
            bot._on_result(res, float(t))
        if lm.exited:
            break

    assert lm.exited is True, "the exit never came back"
    assert bot.exchange.positions[YES].shares == 0.0
    assert lm.inflight == 0

    # 100 sh sold at 0.60 = 60.00 in, less the 0.07*0.6*0.4*100 = 1.68 fee
    assert bot.exchange.balance == pytest.approx(start_balance + 60.0 - 1.68, abs=1e-6)
    # realised: 10.00 gross - 1.75 entry fees - 1.68 exit fee
    assert bot.stats.realised_pnl == pytest.approx(10.0 - 1.75 - 1.68, abs=1e-6)
    assert [r["event"] for r in bot.ledger] == ["fill", "settle"]


def test_a_rejected_exit_leaves_the_position_alone(tmp_path):
    """The bid vanishes in flight: FOK rejects, and we are still long."""
    bot = _bot(tmp_path, take_profit_delta=0.05, take_profit_min_secs_left=1.0)
    lm = _market(bot, bids=((0.60, 500.0),))
    bot.token_index[YES] = (YES, lm)
    _hold(bot, shares=100.0, avg=0.50, fees=1.75)

    bot._take_profit(0.0)
    lm.books[YES].bids = {}                       # pulled before we land

    for t in range(0, 20_001, 250):
        for res in bot.exchange.step(float(t), bot._true_book, bot._close_ts):
            bot._on_result(res, float(t))

    assert lm.exited is False
    assert bot.exchange.positions[YES].shares == 100.0, "still long"
    assert bot.stats.realised_pnl == 0.0
    assert lm.inflight == 0, "inflight must clear or the market is stuck"
    assert any(k.startswith("EXIT_") for k in bot.exec_rejections)


# ──────────────────────────── the other half: stops ──────────────────────────


def test_stop_fires_when_the_bid_falls_far_enough(tmp_path):
    bot = _bot(tmp_path, stop_loss_delta=0.20)
    lm = _market(bot, bids=((0.28, 500.0),))          # entry 0.50, -0.22
    _hold(bot, shares=100.0, avg=0.50)
    ex = bot._exit_candidate(lm, YES, 0.0)
    assert ex is not None and ex["kind"] == "STOP_LOSS"
    assert ex["pnl"] < 0, "a stop realises a loss; that is the point"


def test_a_small_drawdown_is_held(tmp_path):
    """Digitals revert. The stop is a circuit breaker, not a scalp."""
    bot = _bot(tmp_path, stop_loss_delta=0.20)
    lm = _market(bot, bids=((0.40, 500.0),))          # -0.10 only
    _hold(bot, avg=0.50)
    assert bot._exit_candidate(lm, YES, 0.0) is None


def test_the_fee_guard_never_blocks_a_stop(tmp_path):
    """take_profit_min_net must not keep us in a losing position."""
    bot = _bot(tmp_path, stop_loss_delta=0.20, take_profit_min_net=0.005)
    lm = _market(bot, bids=((0.20, 500.0),))
    _hold(bot, shares=100.0, avg=0.50)
    ex = bot._exit_candidate(lm, YES, 0.0)
    assert ex is not None and ex["kind"] == "STOP_LOSS"


def test_stop_can_be_disabled_without_touching_take_profit(tmp_path):
    bot = _bot(tmp_path, stop_loss_enabled=False, take_profit_delta=0.05)
    lm = _market(bot, bids=((0.20, 500.0),))
    _hold(bot, avg=0.50)
    assert bot._exit_candidate(lm, YES, 0.0) is None
    lm.books[YES].bids = {0.60: 500.0}                 # the upside still works
    assert bot._exit_candidate(lm, YES, 0.0)["kind"] == "TAKE_PROFIT"


def test_take_profit_wins_when_somehow_both_would_apply(tmp_path):
    bot = _bot(tmp_path, take_profit_delta=0.05, stop_loss_delta=0.20)
    lm = _market(bot, bids=((0.60, 500.0),))
    _hold(bot, avg=0.50)
    assert bot._exit_candidate(lm, YES, 0.0)["kind"] == "TAKE_PROFIT"


def test_a_stop_is_booked_as_a_loss(tmp_path):
    bot = _bot(tmp_path)
    lm = _market(bot)
    lost = _book_a_win(bot, avg=0.50, exit_price=0.25, entry_fees=1.75)
    assert lost < 0
    assert bot.stats.losses == 1 and bot.stats.wins == 0
    assert lm.exited is True
    assert bot.ledger[-1]["pnl"] == pytest.approx(lost, abs=1e-4)


def test_a_stop_still_releases_the_risk_budget(tmp_path):
    bot = _bot(tmp_path)
    _market(bot)
    bot.risk.on_fill(SLUG, "BTC", 1, "UP", 50.0, 100.0)
    _book_a_win(bot, avg=0.50, exit_price=0.25)
    assert bot.risk.exposure_total() == pytest.approx(0.0)


# ───────────────────── frequency: smaller bets, more windows ─────────────────


def test_epoch_budget_now_admits_twice_as_many_windows():
    """The frequency change is arithmetic, not a loosened cap.

    max_epoch_exposure / max_position is the ceiling on how many 300s windows
    we can be in at once. The aggregate caps did not move; the per-market
    stake halved, so the ceiling doubled.
    """
    from troll_poly_bot.risk.limits import RiskConfig
    r = RiskConfig()
    assert r.max_epoch_exposure_usdc == 30.0, "aggregate epoch cap unchanged"
    assert r.max_total_exposure_usdc == 50.0, "aggregate total cap unchanged"
    assert r.max_epoch_exposure_usdc / r.max_position_usdc == 6.0   # was 3.0
    # and the position count cannot outrun the money cap
    assert r.max_concurrent_positions * r.max_position_usdc <= r.max_total_exposure_usdc
