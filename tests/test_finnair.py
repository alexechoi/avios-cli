"""Contract tests for the Finnair-specific OAuth/API integration."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from avios.config import Settings
from avios.finnair import (
    FINNAIR_PROFILE_PATH,
    FinnairClient,
    FinnairCredentials,
    FinnairSession,
    _points,
    login_finnair_via_browser,
)
from avios.session import SessionExpired


def _client(
    settings: Settings,
    responses: list[dict[str, Any] | int],
    captured: list[httpx.Request],
) -> FinnairClient:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        value = responses.pop(0)
        if isinstance(value, int):
            return httpx.Response(value, json={})
        return httpx.Response(200, json=value)

    session = FinnairSession(
        "oauth-token",
        "api-key",
        settings,
        transport=httpx.MockTransport(handler),
    )
    return FinnairClient(session)


def test_balance_uses_profile_request_and_finnair_headers(settings: Settings) -> None:
    captured: list[httpx.Request] = []
    client = _client(
        settings,
        [{"profile": {"awardPoints": 12345, "firstname": "A"}}],
        captured,
    )

    balance = client.get_balance()

    assert balance.balance == 12345
    assert balance.individual == 12345
    request = captured[0]
    assert request.url.path == FINNAIR_PROFILE_PATH
    assert request.method == "POST"
    assert request.headers["oauth_token"] == "oauth-token"
    assert request.headers["x-api-key"] == "api-key"
    assert request.headers["origin"] == "https://www.finnair.com"
    assert request.content == b'{"profileRequest":{"type":"BASIC","cache":"USE"}}'


def test_transactions_are_mapped_to_shared_model(settings: Settings) -> None:
    captured: list[httpx.Request] = []
    response = {
        "transactions": {
            "transactions": [
                {
                    "id": "one",
                    "transactionId": "tx-1",
                    "date": "2026-07-20",
                    "description": "Flight reward",
                    "awardPoints": "-12500",
                    "transactionSubType": "Redemption",
                    "partnerName": "Finnair",
                    "partnerType": "AIRLINE",
                    "productType": "FLIGHT",
                    "status": "processed",
                },
                {
                    "id": "two",
                    "date": "2026-07-01",
                    "description": "Partner collection",
                    "awardPoints": "+750",
                    "status": "processed",
                },
            ]
        }
    }
    client = _client(settings, [response], captured)

    items = client.get_transactions(limit=1)

    assert len(items) == 1
    assert items[0].identifier == "one"
    assert items[0].date_processed == "2026-07-20"
    assert items[0].amount == -12500
    assert items[0].type is not None and items[0].type.value == "Redemption"
    assert items[0].partner is not None and items[0].partner.value == "Finnair"
    assert captured[0].content == b'{"transactionsRequest":{}}'


def test_profile_maps_fields_used_by_whoami(settings: Settings) -> None:
    captured: list[httpx.Request] = []
    client = _client(
        settings,
        [
            {
                "profile": {
                    "firstname": "Alex",
                    "lastname": "Example",
                    "email": "a@example.test",
                    "tier": "Basic",
                    "memberNumber": "123",
                }
            }
        ],
        captured,
    )

    claims = client.get_profile().as_dict()["tokenContent"]
    assert claims["name"] == "Alex Example"
    assert claims["https://avios.com/customer_tier_name"] == "Basic"
    assert claims["https://avios.com/membership_id"] == "123"


def test_expired_token_is_session_expired(settings: Settings) -> None:
    captured: list[httpx.Request] = []
    client = _client(settings, [401], captured)
    with pytest.raises(SessionExpired, match="Finnair session expired"):
        client.get_balance()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(100, 100), ("+1,250", 1250), (" -500 ", -500), ("unknown", None), (None, None)],
)
def test_points(value: object, expected: int | None) -> None:
    assert _points(value) == expected


class _FakeRequest:
    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = headers


class _FakePage:
    def __init__(self) -> None:
        self.handler: Any = None
        self.waits = 0

    def on(self, event: str, handler: Any) -> None:
        assert event == "request"
        self.handler = handler

    def goto(self, url: str) -> None:
        assert "finnair.com" in url

    def wait_for_timeout(self, timeout: int) -> None:
        self.waits += 1
        if self.waits == 2:
            self.handler(
                _FakeRequest(
                    "https://api.finnair.com/d/loyalty-service/legacy/current/api/profile",
                    {"oauth_token": "captured-token", "x-api-key": "captured-api-key"},
                )
            )


class _FakeContext:
    def __init__(self) -> None:
        self.page = _FakePage()
        self.closed = False

    def new_page(self) -> _FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, context: _FakeContext) -> None:
        self.context = context

    def launch_persistent_context(self, *args: Any, **kwargs: Any) -> _FakeContext:
        return self.context


class _FakePlaywright:
    def __init__(self, context: _FakeContext) -> None:
        self.chromium = _FakeChromium(context)


class _FakeFactory:
    def __init__(self, context: _FakeContext) -> None:
        self.playwright = _FakePlaywright(context)

    def __call__(self) -> _FakeFactory:
        return self

    def __enter__(self) -> _FakePlaywright:
        return self.playwright

    def __exit__(self, *args: object) -> bool:
        return False


def test_browser_login_captures_oauth_header(settings: Settings) -> None:
    context = _FakeContext()
    token = login_finnair_via_browser(
        settings=settings,
        headless=True,
        timeout_ms=2_000,
        playwright_factory=_FakeFactory(context),
    )
    assert token == FinnairCredentials("captured-token", "captured-api-key")
    assert context.closed
