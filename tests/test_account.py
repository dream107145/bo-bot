"""Reading a real Polymarket account.

Read-only by construction: the Data API serves on-chain positions to anyone,
so there is no key here and nothing in this path can place an order. What
these tests protect is the boring, expensive stuff -- an address going into a
URL unvalidated, a network blip blanking a balance, one failing section taking
the others down with it, and the response shape changing under us.

The network is stubbed throughout. A test that needed the internet to pass
would fail for reasons that have nothing to do with this code.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from troll_poly_bot.feeds import account

WALLET = "0x4f1d5ae26fc31472966e951af3183308736d8de2"

# trimmed from a real response, 2026-09-20
POSITION = {
    "proxy_wallet": WALLET, "token_id": "347224", "condition_id": "0x16c6",
    "current_size": 7716.3495, "avg_price": 0.4738, "entry_cost_usdc": 3656.1659,
    "total_cost_usdc": 3723.709566, "current_price": 0.605,
    "current_value": 4668.3914, "realized_pnl": 13.0987,
    "unrealized_pnl": 1012.2254, "percent_pnl": 27.6854, "status": "OPEN",
    "redeemable": False, "title": "Will X happen?", "slug": "will-x-happen",
    "outcome": "Yes",
}
TRADE = {
    "proxy_wallet": WALLET, "side": "BUY", "token_id": "161754",
    "size": 2.0, "price": 0.999, "timestamp": 1789908646,
    "title": "Will Y happen?", "slug": "will-y-happen", "outcome": "No",
}


@pytest.fixture()
def api(monkeypatch):
    """Stub the network. `routes` maps a path to a payload or an Exception."""
    calls = []
    routes: dict = {}

    def fake_get(path, params):
        calls.append((path, params))
        val = routes.get(path)
        if isinstance(val, Exception):
            raise val
        if val is None:
            raise account.AccountError(f"no stub for {path}")
        return val

    monkeypatch.setattr(account, "_get", fake_get)
    return type("Api", (), {"routes": routes, "calls": calls})()


# ──────────────────────────── the address is a URL ───────────────────────────


def test_a_valid_address_is_lowercased():
    assert account.normalise_wallet(WALLET.upper().replace("0X", "0x")) == WALLET


def test_surrounding_whitespace_is_tolerated():
    assert account.normalise_wallet(f"  {WALLET}\n") == WALLET


@pytest.mark.parametrize("bad", [
    "", None, "   ",
    "0x123",                                   # too short
    WALLET + "ff",                             # too long
    WALLET.replace("0x", ""),                  # no prefix
    "0xZZZZ5ae26fc31472966e951af3183308736d8de2",   # not hex
    "0x4f1d5ae26fc31472966e951af3183308736d8de2/../admin",
    "0x4f1d5ae26fc31472966e951af3183308736d8de2?user=someone_else",
])
def test_anything_else_is_refused_before_it_reaches_a_url(bad):
    """This value is interpolated into a request; it does not get the benefit
    of the doubt."""
    with pytest.raises(account.AccountError):
        account.normalise_wallet(bad)


# ─────────────────────────────── parsing shapes ──────────────────────────────


def test_value_is_read_from_the_data_wrapper(api):
    api.routes["/v2/value"] = {"data": {"proxy_wallet": WALLET, "value": 48686.1584}}
    assert account.fetch_value(WALLET) == pytest.approx(48686.1584)


def test_value_survives_the_list_wrapped_variant(api):
    api.routes["/v2/value"] = {"data": [{"value": 12.5}]}
    assert account.fetch_value(WALLET) == pytest.approx(12.5)


def test_value_is_none_rather_than_a_crash_when_absent(api):
    api.routes["/v2/value"] = {"data": {}}
    assert account.fetch_value(WALLET) is None


def test_positions_accept_both_the_bare_list_and_the_wrapper(api):
    for payload in ([POSITION], {"data": [POSITION]}):
        api.routes["/v2/positions"] = payload
        got = account.fetch_positions(WALLET)
        assert len(got) == 1 and got[0]["size"] == pytest.approx(7716.3495)


def test_position_fields_are_mapped_not_passed_through(api):
    api.routes["/v2/positions"] = [POSITION]
    p = account.fetch_positions(WALLET)[0]
    assert p["avg_price"] == pytest.approx(0.4738)
    assert p["current_price"] == pytest.approx(0.605)
    assert p["value"] == pytest.approx(4668.3914)
    assert p["unrealized_pnl"] == pytest.approx(1012.2254)
    assert p["title"] == "Will X happen?"
    assert "proxy_wallet" not in p, "the panel does not need it on every row"


def test_trades_are_newest_first_with_notional(api):
    older = dict(TRADE, timestamp=1, price=0.5, size=4.0)
    api.routes["/v2/trades"] = [older, TRADE]
    rows = account.fetch_trades(WALLET)
    assert [r["ts"] for r in rows] == [1789908646, 1]
    assert rows[1]["usdc"] == pytest.approx(2.0)      # 4.0 * 0.5


def test_junk_rows_are_skipped_not_fatal(api):
    api.routes["/v2/positions"] = [POSITION, "not a dict", None, 42]
    assert len(account.fetch_positions(WALLET)) == 1


def test_missing_numbers_do_not_become_strings(api):
    api.routes["/v2/positions"] = [{"title": "T", "current_size": "oops"}]
    p = account.fetch_positions(WALLET)[0]
    assert p["size"] == 0.0 and p["avg_price"] is None


def test_the_row_limit_is_capped(api):
    api.routes["/v2/trades"] = []
    account.fetch_trades(WALLET, limit=10_000)
    assert api.calls[0][1]["limit"] == account.MAX_ROWS


# ───────────────────────── one failure is not all failures ───────────────────


def test_snapshot_combines_the_three_sections(api):
    api.routes.update({"/v2/value": {"data": {"value": 100.0}},
                       "/v2/positions": [POSITION], "/v2/trades": [TRADE]})
    snap = account.snapshot(WALLET)
    assert snap["wallet"] == WALLET
    assert snap["read_only"] is True
    assert snap["value"] == pytest.approx(100.0)
    assert snap["positions_value"] == pytest.approx(4668.3914)
    assert snap["unrealized_pnl"] == pytest.approx(1012.2254)
    assert len(snap["trades"]) == 1


def test_a_failed_positions_call_does_not_blank_the_balance(api):
    api.routes.update({"/v2/value": {"data": {"value": 100.0}},
                       "/v2/positions": account.AccountError("rate limited"),
                       "/v2/trades": [TRADE]})
    snap = account.snapshot(WALLET)
    assert snap["value"] == pytest.approx(100.0), "this one loaded fine"
    assert snap["positions"] == []
    assert "rate limited" in snap["positions_error"]
    assert len(snap["trades"]) == 1, "and this one too"


def test_a_failed_value_call_leaves_the_rest_intact(api):
    api.routes.update({"/v2/value": account.AccountError("HTTP 503"),
                       "/v2/positions": [POSITION], "/v2/trades": []})
    snap = account.snapshot(WALLET)
    assert snap["value"] is None and "503" in snap["value_error"]
    assert len(snap["positions"]) == 1


def test_a_bad_address_fails_the_snapshot_outright(api):
    """Nothing should be requested at all for an address we will not send."""
    with pytest.raises(account.AccountError):
        account.snapshot("definitely-not-an-address")
    assert api.calls == []


def test_http_errors_become_account_errors(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None)
    monkeypatch.setattr(account.urllib.request, "urlopen", boom)
    with pytest.raises(account.AccountError, match="429"):
        account.fetch_value(WALLET)


def test_unreachable_api_becomes_an_account_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("dns is down")
    monkeypatch.setattr(account.urllib.request, "urlopen", boom)
    with pytest.raises(account.AccountError, match="unreachable"):
        account.fetch_value(WALLET)


def test_malformed_json_becomes_an_account_error(monkeypatch):
    class _Resp:
        def read(self, *a): return b"<html>nope</html>"
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(account.urllib.request, "urlopen", lambda *a, **k: _Resp())
    with pytest.raises(account.AccountError):
        account.fetch_value(WALLET)


# ───────────────────────────── it stays read-only ────────────────────────────


def _module_ast():
    import ast
    return ast.parse(open(account.__file__, encoding="utf-8").read())


def test_the_module_imports_nothing_that_could_sign():
    """A guard on intent: this file must not grow a trading path by accident.

    Checked against the parsed module rather than its text, so the prose
    explaining that it is read-only does not trip its own guard.
    """
    import ast
    forbidden = {"eth_account", "web3", "py_clob_client", "eth_keys", "eth_utils",
                 "coincurve", "hmac", "secrets", "eth_abi"}
    imported = set()
    for node in ast.walk(_module_ast()):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not (imported & forbidden), f"signing-capable import: {imported & forbidden}"


def test_every_request_this_module_makes_is_a_GET():
    """urllib turns a Request into a POST the moment it is given a body."""
    import ast
    for node in ast.walk(_module_ast()):
        if not isinstance(node, ast.Call):
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name != "Request":
            continue
        kwargs = {k.arg for k in node.keywords}
        assert "data" not in kwargs, "a body makes this a POST"
        assert "method" not in kwargs, "the default (GET) is the only one allowed"
