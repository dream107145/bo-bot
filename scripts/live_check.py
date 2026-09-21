"""Where is the money? Diagnose a --live setup without placing anything.

    python scripts/live_check.py

Reads the same .env the bot does and prints ONLY public addresses and
balances, never the key. It logs in through Polymarket's unified SDK, which
reports the account's wallet type itself (Deposit Wallet for every account
created or upgraded since May 2026, legacy proxy or Safe for older ones),
then reads the collateral balance, the exchange allowances and the public
Data API for the account wallet.

The signer address printed here is NOT the address on your Polymarket
profile, and that is expected: the key signs, the account wallet holds the
funds.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
from dotenv import load_dotenv                         # noqa: E402

load_dotenv()

from troll_poly_bot.execution.polymarket import LiveConfigError, LiveCredentials, _parse_balance   # noqa: E402


def data_api(path: str, params: dict):
    url = f"https://data-api.polymarket.com{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": "troll-poly-bot/0.1"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def main() -> None:
    try:
        creds = LiveCredentials.from_env()
    except LiveConfigError as exc:
        print(f"config: {exc}")
        return
    print(f"configured wallet        {creds.wallet or '(unset: the SDK derives your Deposit Wallet)'}")
    print(f"relayer api key          {'given (the bot can redeem winners)' if creds.can_redeem else 'none (use Auto-Redeem in the app)'}")

    try:
        client = creds.build_client()
    except Exception as exc:                              # noqa: BLE001
        print(f"\nlogin FAILED: {type(exc).__name__}: {str(exc)[:200]}")
        print("   The key must be the signer of the account whose address is TPB_POLY_WALLET.")
        print("   Email login: profile -> settings -> export private key.")
        return
    bal = 0.0
    try:
        wallet_type = str(client.wallet_type)
        wallet = str(client.wallet)
        print(f"\nvenue says: wallet type  {wallet_type}")
        print(f"            wallet       {wallet}")
        print(f"            signer       {client.signer}")
        bal, allow = _parse_balance(client.get_balance_allowance(asset_type="COLLATERAL"))
        allow_txt = "n/a" if allow is None else ("unlimited" if allow > 1e12 else f"{allow:.2f}")
        print(f"            collateral   {bal:.2f} USDC   exchange allowance {allow_txt}")
        try:
            state = client.get_trading_approvals_state()
            # the perpetuals deposit approval is not needed to trade the 5-minute markets
            perps = {"0xdca4af75705dbb50f62437045aff9921947917d2"}
            try:
                from polymarket.environments import PRODUCTION, get_environment_config
                perps.add(str(get_environment_config(PRODUCTION).perps_deposit_contract).lower())
            except Exception:                             # noqa: BLE001
                pass
            missing = [a.spender for a in state.missing.erc20 if str(a.spender).lower() not in perps]
            missing += [f"CTF for {a.operator}" for a in getattr(state.missing, "erc1155", ())]
            if not missing:
                print("            approvals    complete for CLOB trading")
            else:
                print(f"            approvals    MISSING for CLOB trading: {', '.join(map(str, missing))}")
                print("                         (trade once in the app, or give the bot a Relayer API key and run")
                print("                          setup_trading_approvals; with a plain wallet approve them yourself)")
        except Exception as exc:                          # noqa: BLE001
            print(f"            approvals    could not be read ({type(exc).__name__})")
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()

    print("\npublic Data API (no key needed):")
    try:
        v = data_api("/v2/value", {"user": wallet})
        data = v.get("data") if isinstance(v, dict) else v
        if isinstance(data, list):
            data = data[0] if data else {}
        val = (data or {}).get("value")
        pos = data_api("/v2/positions", {"user": wallet, "limit": 5})
        rows = pos.get("data") if isinstance(pos, dict) else pos
        print(f"  {wallet}  portfolio value {val}  open positions {len(rows or [])}")
    except Exception as exc:                              # noqa: BLE001
        print(f"  {wallet}  error: {type(exc).__name__}: {str(exc)[:80]}")

    print()
    if bal >= 1.0:
        print(f"-> ready: {bal:.2f} USDC tradable from the {wallet_type}. Next: python -m troll_poly_bot --live")
    else:
        print("-> the account shows no collateral. Deposits made in the app take a minute or two to appear.")
        print("   Check that TPB_POLY_WALLET is the address on your profile and the key is that login's signer.")


if __name__ == "__main__":
    main()
