"""Tests for the typed API client.

The transactions/accounts fixtures are representative, not confirmed shapes, so
these tests assert on counts and field pass-through (via ``extra='allow'``) rather
than a rigid schema. The balance shape is confirmed.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from avios import endpoints
from avios.client import AviosClient, _extract_list
from avios.config import Settings
from avios.session import Session, SessionExpired


def _client(settings: Settings, routes: dict[str, Any]) -> AviosClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path not in routes:
            return httpx.Response(404, json={})
        body = routes[request.url.path]
        if isinstance(body, int):  # treat an int as a status code (e.g. 302)
            return httpx.Response(body, headers={"location": "/auth-gateway/login"})
        return httpx.Response(200, json=body)

    session = Session(settings, transport=httpx.MockTransport(handler))
    session.save_cookies([{"name": "appSession", "value": "x", "domain": "www.avios.com"}])
    return AviosClient(session)


def test_get_balance(settings: Settings, load_fixture: Callable[[str], Any]) -> None:
    client = _client(settings, {endpoints.BALANCE: load_fixture("balance.json")})
    balance = client.get_balance()
    assert balance.balance == 75751
    assert balance.household_avios_balance == 75752


def test_get_transactions_from_bare_list(
    settings: Settings, load_fixture: Callable[[str], Any]
) -> None:
    client = _client(settings, {endpoints.TRANSACTIONS: load_fixture("transactions.json")})
    txns = client.get_transactions()
    assert len(txns) == 3
    assert txns[0].as_dict()["description"] == "Flight BA286 SFO-LHR"


def test_get_transactions_respects_limit(
    settings: Settings, load_fixture: Callable[[str], Any]
) -> None:
    client = _client(settings, {endpoints.TRANSACTIONS: load_fixture("transactions.json")})
    assert len(client.get_transactions(limit=2)) == 2


def test_get_transactions_from_wrapped_object(settings: Settings) -> None:
    payload = {"total": 1, "transactions": [{"date": "2026-07-01", "avios": 100}]}
    client = _client(settings, {endpoints.TRANSACTIONS: payload})
    txns = client.get_transactions()
    assert len(txns) == 1
    assert txns[0].as_dict()["avios"] == 100


def test_get_accounts(settings: Settings, load_fixture: Callable[[str], Any]) -> None:
    client = _client(settings, {endpoints.ACCOUNTS: load_fixture("accounts.json")})
    accounts = client.get_accounts()
    assert len(accounts) == 1
    assert accounts[0].as_dict()["programme"] == "British Airways Executive Club"


def test_raw_normalises_leading_slash(settings: Settings) -> None:
    client = _client(settings, {"/manage-avios/api/user/current": {"id": 1}})
    assert client.raw("manage-avios/api/user/current") == {"id": 1}


def test_expired_session_propagates(settings: Settings) -> None:
    client = _client(settings, {endpoints.BALANCE: 302})
    with pytest.raises(SessionExpired):
        client.get_balance()


def test_extract_list_variants() -> None:
    assert _extract_list([1, 2]) == [1, 2]
    assert _extract_list({"items": [1]}) == [1]
    assert _extract_list({"nope": 1}) == []
    assert _extract_list("string") == []
