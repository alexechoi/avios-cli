"""Tests for the login helpers.

The Playwright browser flow can't be automated (Auth0 + hCaptcha + MFA need a
human), so we test the cookie-conversion/import helpers and the missing-dependency
paths. ``browser_cookie3`` is faked via ``sys.modules`` so tests don't depend on
the optional ``login`` extra or a real browser profile.
"""

from __future__ import annotations

import sys
import types

import pytest

from avios.auth import (
    LoginError,
    _default_loader,
    _only_avios,
    _to_cookie_dicts,
    import_from_browser,
    login_via_browser,
)
from avios.config import Settings
from avios.session import Session


class FakeCookie:
    def __init__(self, name: str, value: str, domain: str) -> None:
        self.name = name
        self.value = value
        self.domain = domain


def _session(settings: Settings) -> Session:
    return Session(settings)


def test_to_cookie_dicts() -> None:
    raw = [FakeCookie("appSession", "abc", "www.avios.com")]
    assert _to_cookie_dicts(raw) == [
        {"name": "appSession", "value": "abc", "domain": "www.avios.com"}
    ]


def test_only_avios_filters_foreign_domains() -> None:
    cookies = [
        {"name": "appSession", "value": "abc", "domain": "www.avios.com"},
        {"name": "_ga", "value": "junk", "domain": ".google.com"},
    ]
    assert _only_avios(cookies) == [cookies[0]]


def test_import_from_browser_saves_only_avios(settings: Settings) -> None:
    session = _session(settings)
    count = import_from_browser(
        session,
        loader=lambda: [
            FakeCookie("appSession", "abc", "www.avios.com"),
            FakeCookie("_ga", "junk", ".google.com"),
        ],
    )
    assert count == 1
    assert session.is_authenticated()
    assert session.load_cookies() == [
        {"name": "appSession", "value": "abc", "domain": "www.avios.com"}
    ]


def test_import_from_browser_no_cookies_raises(settings: Settings) -> None:
    with pytest.raises(LoginError, match="No avios.com cookies"):
        import_from_browser(_session(settings), loader=lambda: [FakeCookie("_ga", "j", ".x.com")])


def _fake_bc3(monkeypatch: pytest.MonkeyPatch, **funcs: object) -> None:
    module = types.ModuleType("browser_cookie3")
    for name, fn in funcs.items():
        setattr(module, name, fn)
    monkeypatch.setitem(sys.modules, "browser_cookie3", module)


def test_default_loader_passes_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def chrome(**kwargs: str) -> list[FakeCookie]:
        seen.update(kwargs)
        return [FakeCookie("appSession", "x", "www.avios.com")]

    _fake_bc3(monkeypatch, chrome=chrome)
    loader = _default_loader("chrome")
    cookies = list(loader())
    assert seen["domain_name"] == "avios.com"
    assert cookies[0].name == "appSession"


def test_default_loader_unknown_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_bc3(monkeypatch, chrome=lambda **k: [])
    with pytest.raises(LoginError, match="Unknown browser"):
        _default_loader("nope")


def test_login_via_browser_without_playwright(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(LoginError, match="Playwright isn't installed"):
        login_via_browser(_session(settings))
