"""python -m troll_poly_bot.web"""
from __future__ import annotations

import argparse

from .server import serve


def main() -> None:
    ap = argparse.ArgumentParser(description="troll-poly-bot paper console")
    ap.add_argument("--host", default="127.0.0.1",
                    help="loopback by default; this server has no auth")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--wallet", default=None,
                    help="Polymarket account wallet (the address on your profile) to show read-only balance, "
                         "positions and trade history for. Also TPB_POLYMARKET_WALLET. "
                         "No key is involved and nothing here can place an order")
    args = ap.parse_args()
    if args.wallet:
        from .server import ACCOUNT
        from ..feeds.account import AccountError, normalise_wallet
        try:
            ACCOUNT.wallet = normalise_wallet(args.wallet)
        except AccountError as exc:
            raise SystemExit(f"--wallet: {exc}")
    serve(args.host, args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
