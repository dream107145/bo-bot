"""Real order execution on Polymarket, behind three separate locks.

This is the one module in the repo that can spend money. It is deliberately
small, boring and defensive.

The three locks
---------------
1. ``--live`` alone is a **dry run**: it authenticates against your account,
   reads the real balance and allowance, reconciles every 30 s, and signs
   every order the strategy wants to send -- and then does NOT post it. The
   order shows up in the ledger as ``DRY_RUN``. Run this first.
2. ``--live --armed`` posts real orders, and only if the environment carries
   ``TPB_LIVE_ACK=I_UNDERSTAND_REAL_MONEY``. The dashboard's Start button
   cannot arm the bot; only the command line can.
3. While armed, every order is checked against caps that are independent of
   the strategy's own risk config (``LiveCaps``): notional per order, open
   notional, orders per hour, a daily-loss halt, and a **kill file** -- touch
   ``data/KILL`` and nothing further is sent, no restart needed.

Orders are fill-or-kill only, exactly like the paper engine, so a stopped
bot never leaves a resting order behind. There is no chase, no retry.

What the venue is trusted with
------------------------------
The account's cash balance is read from the venue and the bot's ``balance``
is the starting bankroll plus the venue's change since start. Fills are what
the venue's order response says was matched, not what we asked for. Fees
are an estimate from the market's schedule; the venue's real deduction
shows up in the next reconciliation.

Redemption
----------
A winning token pays 1 USDC only after it is **redeemed**. Smart-contract
accounts (Deposit Wallets and the older proxies) redeem through Polymarket's
relayer, which needs a **Relayer API key** made in the app (Settings -> API
Keys -> Relayer API Keys). Give it to the bot as ``TPB_POLY_RELAYER_API_KEY``
plus ``TPB_POLY_RELAYER_API_KEY_ADDRESS`` and every resolved winner is
redeemed from the worker thread, retried while the market is still settling
on-chain. Without it the bot does not redeem: turn on Auto-Redeem in the app
instead. Either way, until the cash lands the amount is carried here as
``pending_redemption`` and counted in equity.

Venue client
------------
Polymarket moved to CLOB V2 on 28 April 2026. The old ``py-clob-client``
signs V1 orders, which the venue now rejects, and it cannot sign for a
Deposit Wallet at all (signature type 3, ERC-1271). This module therefore
uses Polymarket's unified SDK, ``polymarket-client`` (``SecureClient``),
which detects the wallet type itself and binds the API key to the right
address. Orders are signed as fill-or-kill *market* orders with a price
bound, which is the V2 shape of the paper engine's FOK limit order.

Credentials
-----------
Read from the environment only (``.env`` is loaded by the CLI); never
logged, never written to the state file. ``TPB_POLY_PRIVATE_KEY`` is the
signer's key (email login: profile -> settings -> export private key).
``TPB_POLY_WALLET`` (older name ``TPB_POLY_FUNDER``) is the account wallet
shown under your Polymarket profile; it is a different address from the
signer, and that is expected. API credentials are derived from the key
unless ``TPB_POLY_API_KEY/SECRET/PASSPHRASE`` are given.
``TPB_POLY_SIGNATURE_TYPE`` is no longer needed; if set it is only compared
with what the venue reports.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..types import Fill, Order, OrderState, Position, Side
from .paper import EPS, FeeModel, OrderResult, RejectReason

log = logging.getLogger("live.exchange")

CLOB_HOST = "https://clob.polymarket.com"
CHAIN_ID = 137
USDC_DECIMALS = 1_000_000.0
ACK_PHRASE = "I_UNDERSTAND_REAL_MONEY"
SIGNATURE_TYPE_OF = {"EOA": 0, "POLY_PROXY": 1, "GNOSIS_SAFE": 2, "DEPOSIT_WALLET": 3}
UNFILLED_CODES = {"unmatched", "fok_not_filled", "fak_not_filled"}
REDEEM_RETRY_S = 90.0
REDEEM_MAX_ATTEMPTS = 12


class LiveConfigError(Exception):
    """Something about the account setup is wrong. Always fatal at startup."""


@dataclass(slots=True)
class LiveCaps:
    """Hard limits that sit outside the strategy. Dollars are real here."""
    max_order_usdc: float = 5.0
    max_open_usdc: float = 25.0
    max_daily_loss_usdc: float = 10.0
    max_orders_per_hour: int = 60
    kill_file: str = "data/KILL"


@dataclass
class LiveCredentials:
    private_key: str
    wallet: str | None = None
    signature_type: int | None = None        # informational; the venue reports the real one
    api_key: str | None = None
    api_secret: str | None = None
    api_passphrase: str | None = None
    relayer_key: str | None = None
    relayer_address: str | None = None

    @classmethod
    def from_env(cls, env: dict | None = None) -> "LiveCredentials":
        env = os.environ if env is None else env

        def get(name: str) -> str | None:
            return (env.get(name) or "").strip() or None

        key = get("TPB_POLY_PRIVATE_KEY")
        if not key:
            raise LiveConfigError(
                "TPB_POLY_PRIVATE_KEY is not set. Put it in .env (never in the shell history). "
                "For an email login export it from Polymarket: profile -> settings -> export private key.")
        wallet = get("TPB_POLY_WALLET") or get("TPB_POLY_FUNDER")
        if wallet is not None and not (wallet.startswith("0x") and len(wallet) == 42):
            raise LiveConfigError(
                "TPB_POLY_WALLET must be the 0x-prefixed 40-hex-character address shown under your Polymarket profile")
        sig_txt = get("TPB_POLY_SIGNATURE_TYPE")
        sig: int | None = None
        if sig_txt is not None:
            if sig_txt not in ("0", "1", "2", "3"):
                raise LiveConfigError(
                    "TPB_POLY_SIGNATURE_TYPE, if set, must be 0, 1, 2 or 3. It is optional: the venue "
                    "reports the wallet type itself")
            sig = int(sig_txt)
        relayer_key, relayer_addr = get("TPB_POLY_RELAYER_API_KEY"), get("TPB_POLY_RELAYER_API_KEY_ADDRESS")
        if bool(relayer_key) != bool(relayer_addr):
            raise LiveConfigError(
                "TPB_POLY_RELAYER_API_KEY and TPB_POLY_RELAYER_API_KEY_ADDRESS go together "
                "(Polymarket app: Settings -> API Keys -> Relayer API Keys)")
        return cls(private_key=key, wallet=wallet, signature_type=sig,
                   api_key=get("TPB_POLY_API_KEY"), api_secret=get("TPB_POLY_API_SECRET"),
                   api_passphrase=get("TPB_POLY_API_PASSPHRASE"),
                   relayer_key=relayer_key, relayer_address=relayer_addr)

    @property
    def funder(self) -> str | None:           # the old name, still used by callers
        return self.wallet

    @property
    def can_redeem(self) -> bool:
        return self.relayer_key is not None

    def redacted(self) -> dict:
        return {"wallet": self.wallet, "signature_type": self.signature_type,
                "api_creds": "given" if self.api_key else "derived",
                "relayer_key": "given" if self.relayer_key else "none",
                "private_key": f"...{self.private_key[-4:]}"}

    def build_client(self):
        """An authenticated ``polymarket.SecureClient`` for this account.

        Creating it derives the CLOB API credentials (or validates the given
        ones) and asks the venue which wallet type the account is.
        """
        try:
            from polymarket import ApiKeyCreds, RelayerApiKey, SecureClient
        except ImportError as exc:                       # pragma: no cover
            raise LiveConfigError("polymarket-client is not installed: pip install -e '.[live]'") from exc
        creds = None
        if self.api_key and self.api_secret and self.api_passphrase:
            creds = ApiKeyCreds.model_validate(
                {"apiKey": self.api_key, "secret": self.api_secret, "passphrase": self.api_passphrase})
        relayer = None
        if self.relayer_key and self.relayer_address:
            relayer = RelayerApiKey(key=self.relayer_key, address=self.relayer_address)
        client = SecureClient.create(private_key=self.private_key, wallet=self.wallet,
                                     credentials=creds, api_key=relayer)
        detected = str(getattr(client, "wallet_type", "") or "")
        known = SIGNATURE_TYPE_OF.get(detected)
        if self.signature_type is not None and known is not None and known != self.signature_type:
            log.warning("TPB_POLY_SIGNATURE_TYPE=%d but the venue reports a %s (type %d); the venue wins",
                        self.signature_type, detected, known)
        return client


def _parse_balance(resp: Any) -> tuple[float, float | None]:
    """(collateral balance USDC, allowance USDC or None) from the venue reply.

    Takes the SDK's ``BalanceAllowance`` model (integer base units) or the
    raw dict the REST endpoint returns.
    """
    if hasattr(resp, "balance") and hasattr(resp, "allowances"):
        bal = float(resp.balance or 0) / USDC_DECIMALS
        values = [float(v or 0) for v in dict(resp.allowances or {}).values()]
        return bal, (max(values) / USDC_DECIMALS if values else None)
    if not isinstance(resp, dict):
        raise LiveConfigError(f"unexpected balance reply: {resp!r}")
    bal = float(resp.get("balance") or 0.0) / USDC_DECIMALS
    allow: float | None = None
    raw = resp.get("allowances")
    if isinstance(raw, dict) and raw:
        allow = max(float(v or 0.0) for v in raw.values()) / USDC_DECIMALS
    elif resp.get("allowance") is not None:
        allow = float(resp.get("allowance") or 0.0) / USDC_DECIMALS
    return bal, allow


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _exc_is(exc: Exception, *names: str) -> bool:
    """Match SDK exceptions by class name so the tests need no SDK."""
    return any(cls.__name__ in names for cls in type(exc).__mro__)


class PolymarketExchange:
    """Same surface as ``PaperExchange``, real venue behind it."""

    RECONCILE_S = 30.0
    MAX_CONSECUTIVE_ERRORS = 5
    is_live = True

    def __init__(
        self,
        client,
        fees: FeeModel,
        bankroll: float,
        caps: LiveCaps | None = None,
        armed: bool = False,
        clock: Callable[[], float] | None = None,
        wallet: str | None = None,
        can_redeem: bool = False,
    ) -> None:
        self.client = client
        self.fees = fees
        self.caps = caps or LiveCaps()
        self.armed = armed
        self.clock = clock or (lambda: time.time() * 1000.0)
        self.address = wallet
        self.can_redeem = can_redeem
        self.condition_of: dict[str, str] = {}     # token id -> condition id, for redemption
        self._redeem_pending: dict[str, tuple[int, float]] = {}   # condition -> (attempts, due monotonic s)
        self.redeemed: list[str] = []

        self.bankroll = bankroll
        self.starting_balance = bankroll
        self.balance = bankroll                  # bankroll + venue cash change since start
        self.venue_balance: float | None = None
        self.venue_balance_at_start: float | None = None
        self.allowance: float | None = None
        self.pending_redemption = 0.0            # USDC owed by unredeemed winners
        self._local_cash_delta = 0.0             # what OUR fills moved the cash by

        self.positions: dict[str, Position] = defaultdict(lambda: Position(""))
        self.fills: list[Fill] = []
        self.results: list[OrderResult] = []
        self.n_submitted = 0
        self.n_rejected: dict[RejectReason, int] = defaultdict(int)
        self.latency = None

        self.halted: str | None = None
        self.consecutive_errors = 0
        self.last_error = ""
        self.last_reconcile_ms = 0.0
        self.realised = 0.0
        self._queue: deque[Order] = deque()
        self._done: deque[OrderResult] = deque()
        self._sent_times: deque[float] = deque()
        self._rtts: list[float] = []

    # ------------------------------------------------------------- startup

    def authenticate(self) -> dict:
        """Read the account. Refuses to continue if it cannot be traded from."""
        from_venue = self._fetch_balance()
        bal, allow = from_venue
        self.venue_balance = self.venue_balance_at_start = bal
        self.allowance = allow
        if allow is not None and allow < min(bal, 1.0):
            raise LiveConfigError(
                f"the account holds {bal:.2f} USDC but the exchange contract has an allowance of "
                f"{allow:.2f}. Deposit or trade once through the Polymarket app, which sets the "
                "allowance, or approve the exchange contract for a plain wallet.")
        if bal < self.bankroll:
            log.warning("bankroll %.2f capped to the account's %.2f USDC", self.bankroll, bal)
            self.bankroll = self.starting_balance = self.balance = bal
        if bal < 1.0:
            raise LiveConfigError(f"the account holds only {bal:.2f} USDC; nothing to trade with")
        return self.snapshot()

    def _fetch_balance(self) -> tuple[float, float | None]:
        return _parse_balance(self.client.get_balance_allowance(asset_type="COLLATERAL"))

    # -------------------------------------------------------------- submit

    def _open_notional(self) -> float:
        return sum(p.cost_basis for p in self.positions.values() if p.shares > EPS)

    def _orders_last_hour(self) -> int:
        cutoff = time.time() - 3600.0
        while self._sent_times and self._sent_times[0] < cutoff:
            self._sent_times.popleft()
        return len(self._sent_times)

    def _precheck(self, order: Order) -> tuple[RejectReason, str] | None:
        if Path(self.caps.kill_file).exists():
            return RejectReason.KILL_SWITCH, f"{self.caps.kill_file} exists"
        if self.halted:
            return RejectReason.HALTED, self.halted
        if not (0.0 < order.price < 1.0) or order.size <= 0.0:
            return RejectReason.TICK_SIZE, f"price {order.price} size {order.size}"
        if order.side is Side.BUY:
            notional = order.price * order.size
            if notional > self.caps.max_order_usdc + 1e-9:
                return RejectReason.LIVE_CAPS, f"order {notional:.2f} > max_order_usdc {self.caps.max_order_usdc:.2f}"
            if self._open_notional() + notional > self.caps.max_open_usdc + 1e-9:
                return RejectReason.LIVE_CAPS, f"open {self._open_notional():.2f} + {notional:.2f} > max_open_usdc"
            if notional > self.balance + 1e-9:
                return RejectReason.INSUFFICIENT_BALANCE, f"cash {self.balance:.2f}"
        if self._orders_last_hour() >= self.caps.max_orders_per_hour:
            return RejectReason.LIVE_CAPS, f"{self.caps.max_orders_per_hour} orders in the last hour"
        return None

    def submit(self, order: Order, now: float) -> str:
        order.decision_ts = now
        order.state = OrderState.IN_FLIGHT
        self.n_submitted += 1
        why = self._precheck(order)
        if why is not None:
            reason, detail = why
            log.warning("REFUSED %s %s %.2f @ %.2f: %s (%s)", order.side.value, order.token_id[:10],
                        order.size, order.price, reason.value, detail)
            self._finish(order, [], reason, now)
            return order.order_id
        self._queue.append(order)
        return order.order_id

    def _finish(self, order: Order, fills: list[Fill], reason: RejectReason, ts: float) -> None:
        if reason is not RejectReason.NONE:
            order.state = OrderState.REJECTED
            self.n_rejected[reason] += 1
        self._done.append(OrderResult(order, fills, reason, ts))

    def step(self, now: float, book_of=None, close_ts_of=None) -> list[OrderResult]:
        out = list(self._done)
        self._done.clear()
        for r in out:
            r.ack_ts = r.ack_ts or now
        self.results.extend(out)
        return out

    # ---------------------------------------------------------------- worker

    async def run(self, stop: asyncio.Event) -> None:
        """Post queued orders off the event loop; reconcile with the venue."""
        while not stop.is_set():
            if self._queue:
                order = self._queue.popleft()
                await asyncio.to_thread(self._place, order)
                continue
            if time.monotonic() * 1000.0 - self.last_reconcile_ms > self.RECONCILE_S * 1000.0:
                self.last_reconcile_ms = time.monotonic() * 1000.0
                await asyncio.to_thread(self.reconcile)
                continue
            due = self._redeem_due(time.monotonic())
            if due is not None:
                await asyncio.to_thread(self._redeem, due)
                continue
            await asyncio.sleep(0.05)
        close = getattr(self.client, "close", None)
        if callable(close):
            try:
                close()
            except Exception:                             # noqa: BLE001
                pass

    # ------------------------------------------------------------ redemption

    def _redeem_due(self, now_mono: float) -> str | None:
        for cond, (_, due) in self._redeem_pending.items():
            if due <= now_mono:
                return cond
        return None

    def _redeem(self, condition_id: str) -> None:
        """Ask the relayer to redeem one resolved market. Retried while it settles."""
        attempts, _ = self._redeem_pending.get(condition_id, (0, 0.0))
        try:
            self.client.redeem_positions(condition_id=condition_id)
        except Exception as exc:                          # noqa: BLE001
            attempts += 1
            if attempts >= REDEEM_MAX_ATTEMPTS:
                self._redeem_pending.pop(condition_id, None)
                log.warning("redeem %s gave up after %d attempts (stays in pending_redemption; "
                            "redeem it in the app): %s", condition_id[:10], attempts, exc)
            else:
                self._redeem_pending[condition_id] = (attempts, time.monotonic() + REDEEM_RETRY_S)
                log.info("redeem %s not yet possible (attempt %d): %s", condition_id[:10], attempts, str(exc)[:120])
            return
        self._redeem_pending.pop(condition_id, None)
        self.redeemed.append(condition_id)
        log.info("REDEEM submitted for %s; the cash shows up at the next reconcile", condition_id[:10])

    def _api_error(self, exc: Exception) -> None:
        self.consecutive_errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"[:200]
        log.error("venue API error (%d in a row): %s", self.consecutive_errors, self.last_error)
        if self.consecutive_errors >= self.MAX_CONSECUTIVE_ERRORS and not self.halted:
            self.halt(f"{self.consecutive_errors} consecutive API errors")

    def halt(self, reason: str) -> None:
        self.halted = reason
        log.error("LIVE TRADING HALTED: %s", reason)

    def _sign(self, order: Order, price: float, size: float):
        """A signed fill-or-kill market order with a price bound. Nothing is sent."""
        if order.side is Side.BUY:
            amount = math.floor(size * price * 100.0) / 100.0      # USDC to spend, sub-cent dropped
            return self.client.create_market_order(token_id=order.token_id, side="BUY", amount=f"{amount:.2f}",
                                                   max_price=f"{price:.2f}", order_type="FOK")
        return self.client.create_market_order(token_id=order.token_id, side="SELL", shares=f"{size:.2f}",
                                               min_price=f"{price:.2f}", order_type="FOK")

    def _place(self, order: Order) -> None:
        price = round(order.price, 2)
        size = math.floor(order.size * 100.0) / 100.0
        try:
            signed = self._sign(order, price, size)
        except Exception as exc:                          # noqa: BLE001
            if _exc_is(exc, "InsufficientLiquidityError"):
                log.info("FOK not fillable from the book: %s", exc)
                self._finish(order, [], RejectReason.FOK_UNFILLABLE, self.clock())
            elif _exc_is(exc, "UserInputError"):
                log.warning("order refused before signing: %s", exc)
                self._finish(order, [], RejectReason.TICK_SIZE, self.clock())
            else:
                self._api_error(exc)
                self._finish(order, [], RejectReason.API_ERROR, self.clock())
            return
        if not self.armed:
            log.info("DRY RUN  would post %s %-4s %6.2f sh @ %.2f  (signed locally, not sent)",
                     order.side.value, order.tag[:4], size, price)
            self._finish(order, [], RejectReason.DRY_RUN, self.clock())
            return
        t0 = time.perf_counter()
        try:
            resp = self.client.post_order(signed)
        except Exception as exc:                          # noqa: BLE001
            self._api_error(exc)
            self._finish(order, [], RejectReason.API_ERROR, self.clock())
            return
        rtt = (time.perf_counter() - t0) * 1000.0
        self._rtts.append(rtt)
        self._sent_times.append(time.time())
        now = self.clock()
        if not getattr(resp, "ok", False):
            code = str(getattr(resp, "code", "") or "")
            msg = str(getattr(resp, "message", "") or "")
            if code in UNFILLED_CODES:
                self.consecutive_errors = 0
                log.info("FOK not filled: %s", msg or code)
                self._finish(order, [], RejectReason.FOK_UNFILLABLE, now)
            elif code == "not_enough_balance":
                self.consecutive_errors = 0
                log.warning("venue refused for balance: %s", msg)
                self._finish(order, [], RejectReason.INSUFFICIENT_BALANCE, now)
            elif code == "market_not_ready":
                self.consecutive_errors = 0
                self._finish(order, [], RejectReason.MARKET_CLOSED, now)
            else:                                         # auth, signature, unknown: count it
                self._api_error(RuntimeError(f"venue rejected the order: {code}: {msg}"))
                self._finish(order, [], RejectReason.API_ERROR, now)
            return
        self.consecutive_errors = 0
        shares, usdc = self._matched_amounts(resp, order)
        if shares <= EPS:
            self._finish(order, [], RejectReason.FOK_UNFILLABLE, now)
            return
        avg = usdc / shares
        fee = self.fees.charge(avg, shares, order.token_id)
        fill = Fill(order_id=order.order_id, token_id=order.token_id, side=order.side, price=avg,
                    size=shares, fee=fee, exchange_ts=now, ack_ts=now, decision_ts=order.decision_ts,
                    expected_price=order.expected_price, tag=order.tag)
        order.filled_size += shares
        order.filled_notional += usdc
        order.state = OrderState.FILLED
        pos = self.positions[order.token_id]
        if not pos.token_id:
            pos.token_id = order.token_id
        pos.apply(fill)
        cash = -(usdc + fee) if order.side is Side.BUY else (usdc - fee)
        self.balance += cash
        self._local_cash_delta += cash
        self.fills.append(fill)
        log.info("VENUE FILL %s %.2f sh @ %.4f  fee ~%.4f  rtt %.0fms  id %s",
                 order.side.value, shares, avg, fee, rtt, str(getattr(resp, "order_id", ""))[:12])
        self._finish(order, [fill], RejectReason.NONE, now)

    def _matched_amounts(self, resp: Any, order: Order) -> tuple[float, float]:
        """(shares, usdc) actually matched, from the reply or the order record.

        The venue reports ``making_amount``/``taking_amount`` in USDC and
        shares (BUY: making = USDC spent, taking = shares received).
        """
        making = _num(getattr(resp, "making_amount", None))
        taking = _num(getattr(resp, "taking_amount", None))
        shares, usdc = (taking, making) if order.side is Side.BUY else (making, taking)
        if shares > EPS and usdc > EPS:
            return shares, usdc
        oid = getattr(resp, "order_id", None)
        if not oid:
            return 0.0, 0.0
        for _ in range(3):
            try:
                rec = self.client.get_order(order_id=oid)
            except Exception as exc:                      # noqa: BLE001
                # a killed FOK is gone from the venue's books: not an API fault
                log.info("order %s not retrievable after posting: %s", str(oid)[:12], str(exc)[:120])
                return 0.0, 0.0
            matched = _num(getattr(rec, "size_matched", None))
            px = _num(getattr(rec, "price", None))
            if matched > EPS and px > 0.0:
                return matched, matched * px
            time.sleep(0.3)
        return 0.0, 0.0

    # ------------------------------------------------------------ settlement

    def settle_market(self, token_id: str, won: bool) -> float:
        pos = self.positions.get(token_id)
        if pos is None or abs(pos.shares) <= EPS:
            return 0.0
        payout = pos.shares if won else 0.0
        pnl = payout - pos.cost_basis - pos.fees_paid
        if won:
            # the USDC arrives when the token is redeemed; carry it until then
            self.pending_redemption += payout
            cond = self.condition_of.get(token_id)
            if self.can_redeem and cond and cond not in self._redeem_pending:
                # the market resolves on-chain a little after the TWAP window; first try soon
                self._redeem_pending[cond] = (0, time.monotonic() + 20.0)
        pos.shares = 0.0
        pos.cost_basis = 0.0
        pos.fees_paid = 0.0
        self.realised += pnl
        self._check_daily_loss()
        return pnl

    def _check_daily_loss(self) -> None:
        if self.halted:
            return
        if -self.realised >= self.caps.max_daily_loss_usdc:
            self.halt(f"realised loss {self.realised:+.2f} reached max_daily_loss_usdc {self.caps.max_daily_loss_usdc:.2f}")

    def reconcile(self) -> None:
        """The venue's cash is the truth; ours is a running guess in between."""
        try:
            bal, allow = self._fetch_balance()
        except Exception as exc:                          # noqa: BLE001
            self._api_error(exc)
            return
        self.consecutive_errors = 0
        self.venue_balance, self.allowance = bal, allow
        if self.venue_balance_at_start is None:
            self.venue_balance_at_start = bal
        venue_delta = bal - self.venue_balance_at_start
        # cash that moved which our own fills do not explain: redemptions
        # (or a deposit). Retire pending redemptions against it.
        unexplained = venue_delta - self._local_cash_delta
        if unexplained > 0.01:
            self.pending_redemption = max(0.0, self.pending_redemption - unexplained)
        self._local_cash_delta = venue_delta
        self.balance = self.bankroll + venue_delta

    # ------------------------------------------------------------- reporting

    def latency_report(self) -> dict[str, float]:
        filled = {f.order_id for f in self.fills}
        slips = [f.slippage for f in self.fills if f.expected_price is not None]
        return {
            "orders_submitted": float(self.n_submitted),
            "orders_filled": float(len(filled)),
            "fill_rate": len(filled) / self.n_submitted if self.n_submitted else 0.0,
            "rejected_fok_unfillable": float(self.n_rejected[RejectReason.FOK_UNFILLABLE]),
            "rejected_market_closed": float(self.n_rejected[RejectReason.MARKET_CLOSED]),
            "rejected_no_liquidity": float(self.n_rejected[RejectReason.NO_LIQUIDITY]),
            "mean_slippage_per_share": sum(slips) / len(slips) if slips else 0.0,
            "total_slippage_cost": sum(f.slippage * f.size for f in self.fills if f.expected_price is not None),
            "total_fees": sum(f.fee for f in self.fills),
            "mean_round_trip_ms": sum(self._rtts) / len(self._rtts) if self._rtts else 0.0,
            "p95_round_trip_ms": sorted(self._rtts)[int(len(self._rtts) * 0.95)] if len(self._rtts) >= 20 else 0.0,
            "filled_shares": sum(f.size for f in self.fills),
        }

    def equity(self, mark=None) -> float:
        eq = self.balance + self.pending_redemption
        if mark is not None:
            for token_id, pos in self.positions.items():
                if abs(pos.shares) <= EPS:
                    continue
                m = mark(token_id)
                if m is not None:
                    eq += pos.shares * m
        return eq

    def snapshot(self) -> dict:
        return {
            "armed": self.armed,
            "address": self.address,
            "wallet_type": str(getattr(self.client, "wallet_type", "") or "") or None,
            "can_redeem": self.can_redeem,
            "redeem_pending": len(self._redeem_pending),
            "redeemed": len(self.redeemed),
            "bankroll": round(self.bankroll, 4),
            "venue_balance": None if self.venue_balance is None else round(self.venue_balance, 4),
            "venue_balance_at_start": None if self.venue_balance_at_start is None else round(self.venue_balance_at_start, 4),
            "allowance": None if self.allowance is None else round(self.allowance, 2),
            "pending_redemption": round(self.pending_redemption, 4),
            "realised": round(self.realised, 4),
            "halted": self.halted,
            "last_error": self.last_error,
            "consecutive_errors": self.consecutive_errors,
            "orders_last_hour": self._orders_last_hour(),
            "kill_file_present": Path(self.caps.kill_file).exists(),
            "caps": {"max_order_usdc": self.caps.max_order_usdc, "max_open_usdc": self.caps.max_open_usdc,
                     "max_daily_loss_usdc": self.caps.max_daily_loss_usdc,
                     "max_orders_per_hour": self.caps.max_orders_per_hour, "kill_file": self.caps.kill_file},
        }
