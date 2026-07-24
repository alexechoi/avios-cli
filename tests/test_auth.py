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


# --- fake Playwright, to exercise the poll-until-authenticated flow --------------
class _FakeResponse:
    def __init__(self, ok: bool) -> None:
        self.ok = ok


class _FakeRequest:
    """Auth endpoint returns 401 until `ok_after` calls, then 200."""

    def __init__(self, ok_after: int) -> None:
        self.ok_after = ok_after
        self.calls = 0

    def get(self, url: str) -> _FakeResponse:
        self.calls += 1
        return _FakeResponse(self.calls >= self.ok_after)


class _FakePage:
    def goto(self, url: str) -> None:
        pass

    def wait_for_timeout(self, ms: int) -> None:
        pass


class _FakeContext:
    def __init__(self, ok_after: int, cookies: list[dict[str, str]]) -> None:
        self.request = _FakeRequest(ok_after)
        self._cookies = cookies

    def new_page(self) -> _FakePage:
        return _FakePage()

    def cookies(self) -> list[dict[str, str]]:
        return self._cookies


class _FakeBrowser:
    def __init__(self, ctx: _FakeContext) -> None:
        self._ctx = ctx
        self.closed = False

    def new_context(self, **kwargs: object) -> _FakeContext:
        return self._ctx

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.chromium = _FakeChromium(browser)


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser
        self.launches: list[str | None] = []

    def launch(self, **kwargs: object) -> _FakeBrowser:
        self.launches.append(kwargs.get("channel"))  # type: ignore[arg-type]
        return self._browser


class _FakeFactory:
    """Callable that returns a context manager yielding a fake Playwright."""

    def __init__(self, browser: _FakeBrowser) -> None:
        self._pw = _FakePlaywright(browser)

    def __call__(self) -> _FakeFactory:
        return self

    def __enter__(self) -> _FakePlaywright:
        return self._pw

    def __exit__(self, *args: object) -> bool:
        return False


def test_login_captures_only_after_authenticated(settings: Settings) -> None:
    cookies = [
        {"name": "appSession", "value": "x", "domain": "www.avios.com"},
        {"name": "_ga", "value": "junk", "domain": ".google.com"},
    ]
    ctx = _FakeContext(ok_after=3, cookies=cookies)
    browser = _FakeBrowser(ctx)
    session = _session(settings)

    count = login_via_browser(session, timeout_ms=100_000, playwright_factory=_FakeFactory(browser))

    assert count == 1  # only the avios cookie
    assert session.load_cookies() == cookies
    assert browser.closed
    assert ctx.request.calls >= 3  # polled until authenticated, not immediately


def test_login_times_out_without_saving(settings: Settings) -> None:
    ctx = _FakeContext(
        ok_after=999, cookies=[{"name": "appSession", "value": "x", "domain": "www.avios.com"}]
    )
    browser = _FakeBrowser(ctx)
    session = _session(settings)

    with pytest.raises(LoginError, match="Timed out"):
        login_via_browser(session, timeout_ms=6_000, playwright_factory=_FakeFactory(browser))

    assert session.is_authenticated() is False  # nothing saved on timeout
    assert browser.closed


def test_launch_browser_prefers_chrome_then_falls_back() -> None:
    from avios.auth import _launch_browser

    class Chromium:
        def __init__(self) -> None:
            self.channels: list[str | None] = []

        def launch(self, **kwargs: object) -> str:
            channel = kwargs.get("channel")
            self.channels.append(channel)  # type: ignore[arg-type]
            if channel == "chrome":
                raise RuntimeError("no system chrome")
            return "browser"

    class PW:
        def __init__(self) -> None:
            self.chromium = Chromium()

    pw = PW()
    assert _launch_browser(pw, headless=True) == "browser"
    assert pw.chromium.channels == ["chrome", None]
