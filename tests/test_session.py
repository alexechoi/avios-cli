"""Tests for the session / cookie layer."""

from __future__ import annotations

import stat

import httpx
import pytest

from avios.config import Settings
from avios.session import NotAuthenticated, Session, SessionExpired, cookie_header_from

AVIOS_COOKIE = [{"name": "appSession", "value": "abc", "domain": "www.avios.com"}]


def _session(settings: Settings, handler: httpx.MockTransport | None = None) -> Session:
    return Session(settings, transport=handler)


def test_save_and_load_cookies_round_trip(settings: Settings) -> None:
    session = _session(settings)
    session.save_cookies(AVIOS_COOKIE)
    assert session.load_cookies() == AVIOS_COOKIE


def test_state_file_is_private(settings: Settings) -> None:
    session = _session(settings)
    session.save_cookies(AVIOS_COOKIE)
    mode = stat.S_IMODE(session.state_path.stat().st_mode)
    assert mode == 0o600


def test_cookie_header_filters_to_avios_domain() -> None:
    cookies = [
        {"name": "appSession", "value": "abc", "domain": "www.avios.com"},
        {"name": "_ga", "value": "junk", "domain": ".google.com"},
    ]
    assert cookie_header_from(cookies) == "appSession=abc"


def test_env_cookie_overrides_stored(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _session(settings)
    monkeypatch.setenv("AVIOS_COOKIE", "override=1")
    assert session.cookie_header() == "override=1"


def test_is_authenticated(settings: Settings) -> None:
    session = _session(settings)
    assert session.is_authenticated() is False
    session.save_cookies(AVIOS_COOKIE)
    assert session.is_authenticated() is True


def test_clear_removes_state(settings: Settings) -> None:
    session = _session(settings)
    session.save_cookies(AVIOS_COOKIE)
    session.clear()
    assert session.is_authenticated() is False
    session.clear()  # idempotent


def test_client_requires_authentication(settings: Settings) -> None:
    with pytest.raises(NotAuthenticated):
        _session(settings).client()


def test_get_json_returns_parsed_body(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["cookie"] == "appSession=abc"
        assert request.headers["x-avios-opco"] == "BAEC"
        return httpx.Response(200, json={"balance": 75751})

    session = _session(settings, httpx.MockTransport(handler))
    session.save_cookies(AVIOS_COOKIE)
    assert session.get_json("/en-GB/spend-avios/api/avios-balance") == {"balance": 75751}


@pytest.mark.parametrize("status", [301, 302, 401])
def test_get_json_raises_on_expired_session(settings: Settings, status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers={"location": "/auth-gateway/login"})

    session = _session(settings, httpx.MockTransport(handler))
    session.save_cookies(AVIOS_COOKIE)
    with pytest.raises(SessionExpired):
        session.get_json("/manage-avios/api/user/current/balance")
