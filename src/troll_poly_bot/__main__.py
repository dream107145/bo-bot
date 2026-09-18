"""python -m troll_poly_bot --balance 100

Live PAPER trading against real Polymarket data, across every 5-minute crypto
market the venue lists. Fills are simulated; this repo contains no
order-signing code, so no real order can be placed.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .config import BotConfig
from .live import LiveBot


def main() -> None:
    ap = argparse.ArgumentParser(description="troll-poly-bot live paper trader")
    ap.add_argument("--balance", type=float, default=100.0)
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
                         "clear it, because a restart also resets the balance")
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

    bot = LiveBot(
        assets=tuple(a.strip().upper() for a in args.assets.split(",") if a.strip()),
        balance=args.balance,
        cfg=cfg,
        exchanges=tuple(e.strip().lower() for e in args.exchanges.split(",") if e.strip()),
        reset_history=not args.keep_history,
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
    print(f"final equity  ${snap['equity']:.2f}   pnl {snap['pnl']:+.2f}")
    print(f"assets {', '.join(snap['assets']) or 'none discovered'}")
    print(f"settled {snap['stats']['settled']}  wins {snap['stats']['wins']}  "
          f"losses {snap['stats']['losses']}  basis disagreements {snap['stats']['basis_disagreements']}")
    print(f"orders submitted {int(snap['orders'].get('orders_submitted', 0))}  "
          f"filled {int(snap['orders'].get('orders_filled', 0))}")
    top = sorted(snap["skips"].items(), key=lambda kv: -kv[1])[:8]
    print("why not trading: " + (", ".join(f"{k} {v}" for k, v in top) or "no evaluations"))
    print(f"evidence: {snap['evidence']}")
    print(f"state written to {bot.state_path}")


if __name__ == "__main__":
    main()
