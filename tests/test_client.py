"""Tests for the typed API client (balance, transactions, profile, ...)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from avios import endpoints
from avios.client import AviosClient, _extract_list
from avios.config import Settings
from avios.session import Session, SessionExpired


def _client(
    settings: Settings, routes: dict[str, Any], captured: list[httpx.Request] | None = None
) -> AviosClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
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
    client = _client(settings, {endpoints.ACCOUNTS: load_fixture("balance.json")})
    balance = client.get_balance()
    assert balance.balance == 75751
    assert balance.household == 112430


def test_get_transactions(settings: Settings, load_fixture: Callable[[str], Any]) -> None:
    captured: list[httpx.Request] = []
    client = _client(
        settings, {endpoints.TRANSACTIONS: load_fixture("transactions.json")}, captured
    )
    txns = client.get_transactions(limit=2)
    assert len(txns) == 2
    assert txns[0].type is not None and txns[0].type.value == "Collection"
    assert txns[0].amount == 50
    assert txns[1].amount == -52000
    # Sends the required opco header and the limit as offset.
    req = captured[-1]
    assert req.headers["x-avios-opco"] == "BAEC"
    assert "offset=2" in str(req.url)
    assert "status=completed" in str(req.url)


def test_get_profile(settings: Settings) -> None:
    payload = {"idToken": "jwt", "tokenContent": {"name": "Alex"}}
    client = _client(settings, {endpoints.AUTH_USER: payload})
    profile = client.get_profile()
    assert profile.as_dict()["tokenContent"]["name"] == "Alex"


def test_raw_normalises_leading_slash(settings: Settings) -> None:
    client = _client(settings, {"/manage-avios/api/user/current": {"id": 1}})
    assert client.raw("manage-avios/api/user/current") == {"id": 1}


def test_expired_session_propagates(settings: Settings) -> None:
    client = _client(settings, {endpoints.ACCOUNTS: 302})
    with pytest.raises(SessionExpired):
        client.get_balance()


def test_extract_list_variants() -> None:
    assert _extract_list([1, 2]) == [1, 2]
    assert _extract_list({"items": [1]}) == [1]
    assert _extract_list({"nope": 1}) == []
    assert _extract_list("string") == []
