"""python -m troll_poly_bot --balance 100

Paper trading by default: real Polymarket data, simulated fills.

    --live            real account, DRY RUN: authenticates, reconciles, signs
                      every order the strategy wants and posts none of them
    --live --armed    posts real fill-or-kill orders. Requires
                      TPB_LIVE_ACK=I_UNDERSTAND_REAL_MONEY in the environment.

Credentials come from the environment (a local .env is loaded), never from
flags: TPB_POLY_PRIVATE_KEY (the signer), TPB_POLY_WALLET (the account wallet
shown on your profile) and optionally TPB_POLY_RELAYER_API_KEY(+_ADDRESS) so
the bot can redeem winners. The venue reports the wallet type itself.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys

from .config import BotConfig
from .live import LiveBot


def _build_live_exchange(args, cfg: BotConfig):
    from dotenv import load_dotenv
    load_dotenv()
    logging.getLogger("httpx").setLevel(logging.WARNING)      # the SDK logs every request at INFO
    from .execution.paper import FeeModel
    from .execution.polymarket import (
        ACK_PHRASE, LiveCaps, LiveConfigError, LiveCredentials, PolymarketExchange,
    )
    from .signals.costs import FeeSchedule

    armed = bool(args.armed)
    if armed and os.getenv("TPB_LIVE_ACK", "").strip() != ACK_PHRASE:
        raise SystemExit(
            f"refusing to arm: set TPB_LIVE_ACK={ACK_PHRASE} in the environment (or .env) "
            "to confirm you want real orders placed. Without --armed this is a dry run.")
    try:
        creds = LiveCredentials.from_env()
        client = creds.build_client()
    except LiveConfigError as exc:
        raise SystemExit(f"live setup: {exc}") from exc
    except Exception as exc:                              # noqa: BLE001
        raise SystemExit(f"could not log in to the venue: {type(exc).__name__}: {exc}") from exc
    caps = LiveCaps(max_order_usdc=args.max_order_usdc, max_open_usdc=args.max_open_usdc,
                    max_daily_loss_usdc=args.max_daily_loss, max_orders_per_hour=args.max_orders_per_hour,
                    kill_file=args.kill_file)
    wallet = str(getattr(client, "wallet", "") or creds.wallet or "")
    exchange = PolymarketExchange(client, FeeModel.from_schedule(FeeSchedule()), bankroll=args.balance,
                                  caps=caps, armed=armed, wallet=wallet, can_redeem=creds.can_redeem)
    try:
        info = exchange.authenticate()
    except LiveConfigError as exc:
        raise SystemExit(f"live setup: {exc}") from exc
    except Exception as exc:                              # noqa: BLE001
        raise SystemExit(f"could not read the account from the venue: {type(exc).__name__}: {exc}") from exc
    cfg.mode = "live"
    cfg.live.armed = armed
    banner = "REAL MONEY -- ORDERS WILL BE POSTED" if armed else "DRY RUN -- orders are signed but never posted"
    allow = info["allowance"]
    allow_txt = "n/a" if allow is None else ("unlimited" if allow > 1e12 else f"{allow:.2f}")
    print("=" * 72)
    print(f"  LIVE ACCOUNT  {banner}")
    signer = str(getattr(client, "signer", "") or "")
    print(f"  wallet {info['address']}  type {info['wallet_type'] or '?'}  "
          f"signer ...{signer[-6:]}  api creds {creds.redacted()['api_creds']}")
    print(f"  venue balance {info['venue_balance']:.2f} USDC  allowance {allow_txt}  bankroll {info['bankroll']:.2f}")
    print(f"  caps: {caps.max_order_usdc:.2f}/order  {caps.max_open_usdc:.2f} open  "
          f"{caps.max_daily_loss_usdc:.2f} daily loss  {caps.max_orders_per_hour}/hour  kill file {caps.kill_file}")
    if creds.can_redeem:
        print("  redemption: automatic through your Relayer API key")
    else:
        print("  redemption: no Relayer API key in .env -> turn on Auto-Redeem in the Polymarket app, or add")
        print("              TPB_POLY_RELAYER_API_KEY and TPB_POLY_RELAYER_API_KEY_ADDRESS (Settings -> API Keys)")
    print("=" * 72)
    return exchange


def main() -> None:
    ap = argparse.ArgumentParser(description="troll-poly-bot: paper by default, real account with --live")
    ap.add_argument("--balance", type=float, default=100.0,
                    help="paper: starting balance. live: the bankroll the risk caps scale to "
                         "(capped to what the account holds)")
    ap.add_argument("--assets", default="",
                    help="comma separated restriction, e.g. BTC,ETH. Default: every asset the venue lists")
    ap.add_argument("--exchanges", default="binance,bybit,coinbase",
                    help="spot sources for the composite price")
    ap.add_argument("--min-edge", type=float, default=None,
                    help="required NET edge after fees, slippage and the uncertainty charge")
    ap.add_argument("--blend", type=float, default=None,
                    help="weight on the market mid in the blended probability (default 0.5)")
    ap.add_argument("--take-profit", type=float, default=None,
                    help="sell early once the best bid is this far above our average "
                         "entry, in probability units (default 0.05). 0 disables")
    ap.add_argument("--stop-loss", type=float, default=None,
                    help="sell when the best bid falls this far BELOW our average entry, "
                         "in probability units (default 0.20). 0 disables")
    ap.add_argument("--keep-history", action="store_true",
                    help="append to data/live_trades.jsonl across restarts. Default is to "
                         "clear it, because a restart also resets the balance (always kept in --live)")
    ap.add_argument("--live", action="store_true",
                    help="trade the real Polymarket account from .env credentials (DRY RUN unless --armed)")
    ap.add_argument("--armed", action="store_true",
                    help="with --live: actually post orders. Needs TPB_LIVE_ACK in the environment")
    ap.add_argument("--max-order-usdc", type=float, default=5.0, help="live cap per order (default 5)")
    ap.add_argument("--max-open-usdc", type=float, default=25.0, help="live cap on open notional (default 25)")
    ap.add_argument("--max-daily-loss", type=float, default=10.0,
                    help="live: halt for the rest of the run once realised losses reach this (default 10)")
    ap.add_argument("--max-orders-per-hour", type=int, default=60)
    ap.add_argument("--kill-file", default="data/KILL",
                    help="live: create this file and no further order is sent")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="stop after this many seconds (0 = run until Ctrl+C)")
    args = ap.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = BotConfig()
    cfg.scale_risk_to_balance(args.balance)
    if args.min_edge is not None:
        cfg.engine.min_net_edge = args.min_edge
    if args.blend is not None:
        cfg.engine.market_blend = min(max(args.blend, 0.0), 1.0)
    if args.take_profit is not None:
        cfg.engine.take_profit_enabled = args.take_profit > 0
        if args.take_profit > 0:
            cfg.engine.take_profit_delta = args.take_profit
    if args.stop_loss is not None:
        cfg.engine.stop_loss_enabled = args.stop_loss > 0
        if args.stop_loss > 0:
            cfg.engine.stop_loss_delta = args.stop_loss

    exchange = None
    if args.live:
        exchange = _build_live_exchange(args, cfg)
        # the risk caps scale to the bankroll the account can actually fund
        cfg = BotConfig(engine=cfg.engine, mode="live")
        cfg.live.armed = bool(args.armed)
        cfg.scale_risk_to_balance(exchange.bankroll)
        if args.min_edge is not None:
            cfg.engine.min_net_edge = args.min_edge
    elif args.armed:
        raise SystemExit("--armed only means something with --live")

    bot = LiveBot(
        assets=tuple(a.strip().upper() for a in args.assets.split(",") if a.strip()),
        balance=exchange.bankroll if exchange is not None else args.balance,
        cfg=cfg,
        exchanges=tuple(e.strip().lower() for e in args.exchanges.split(",") if e.strip()),
        reset_history=(not args.keep_history) and exchange is None,
        exchange=exchange,
    )

    async def runner() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, bot.stop)
            except NotImplementedError:
                pass                       # Windows: KeyboardInterrupt instead
        if args.duration > 0:
            loop.call_later(args.duration, bot.stop)
        await bot.run()

    try:
        asyncio.run(runner())
    except KeyboardInterrupt:
        bot.stop()

    snap = bot.snapshot()
    print("\n" + "=" * 68)
    print(f"mode {snap['mode']}   final equity  ${snap['equity']:.2f}   pnl {snap['pnl']:+.2f}")
    print(f"assets {', '.join(snap['assets']) or 'none discovered'}")
    print(f"settled {snap['stats']['settled']}  wins {snap['stats']['wins']}  "
          f"losses {snap['stats']['losses']}  basis disagreements {snap['stats']['basis_disagreements']}")
    print(f"orders submitted {int(snap['orders'].get('orders_submitted', 0))}  "
          f"filled {int(snap['orders'].get('orders_filled', 0))}")
    top = sorted(snap["skips"].items(), key=lambda kv: -kv[1])[:8]
    print("why not trading: " + (", ".join(f"{k} {v}" for k, v in top) or "no evaluations"))
    print(f"evidence: {snap['evidence']}")
    if snap.get("live"):
        lv = snap["live"]
        print(f"account: venue balance {lv['venue_balance']}  pending redemption {lv['pending_redemption']}  "
              f"halted {lv['halted']}")
    print(f"state written to {bot.state_path}")


if __name__ == "__main__":
    sys.exit(main())
