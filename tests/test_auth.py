"""Tests for the login helpers.

The interactive browser flow can't be automated (Auth0 + hCaptcha + MFA need a
human), so we inject a fake Playwright to exercise the poll-until-authenticated
logic, and fake ``browser_cookie3`` via ``sys.modules`` so tests don't depend on
the optional ``login`` extra or a real browser profile.
"""

from __future__ import annotations

import sys
import types

import pytest

from avios.auth import (
    ImportResult,
    LoginError,
    _browser_fn,
    _only_avios,
    _open_login_context,
    _to_cookie_dicts,
    import_from_browser,
    login_via_browser,
)
from avios.config import Settings
from avios.programmes import get_programme

BA = get_programme("ba")


class FakeCookie:
    def __init__(self, name: str, value: str, domain: str) -> None:
        self.name = name
        self.value = value
        self.domain = domain


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


def test_import_from_browser_uses_authenticated_cookies(settings: Settings) -> None:
    result = import_from_browser(
        BA,
        settings=settings,
        loader=lambda: [
            FakeCookie("appSession", "abc", "www.avios.com"),
            FakeCookie("_ga", "junk", ".google.com"),
        ],
        authenticator=lambda cookies: True,
    )
    assert isinstance(result, ImportResult)
    assert result.count == 1  # only the avios cookie
    assert result.authenticated is True
    assert result.cookies == [{"name": "appSession", "value": "abc", "domain": "www.avios.com"}]


def test_import_from_browser_reports_unauthenticated(settings: Settings) -> None:
    result = import_from_browser(
        BA,
        settings=settings,
        loader=lambda: [FakeCookie("appSession", "stale", "www.avios.com")],
        authenticator=lambda cookies: False,  # cookies exist but don't authenticate
    )
    assert result.count == 1
    assert result.authenticated is False  # returned as best-effort, flagged as not working
    assert result.cookies == [{"name": "appSession", "value": "stale", "domain": "www.avios.com"}]


def test_import_from_browser_no_cookies_raises(settings: Settings) -> None:
    with pytest.raises(LoginError, match="No avios.com cookies"):
        import_from_browser(
            BA,
            settings=settings,
            loader=lambda: [FakeCookie("_ga", "j", ".x.com")],
            authenticator=lambda cookies: True,
        )


def test_browser_fn_unknown_browser() -> None:
    bc3 = types.ModuleType("browser_cookie3")
    bc3.chrome = lambda **k: []  # type: ignore[attr-defined]
    with pytest.raises(LoginError, match="Unknown browser"):
        _browser_fn(bc3, "nope")


def test_login_via_browser_without_playwright(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
    with pytest.raises(LoginError, match="Playwright isn't installed"):
        login_via_browser(BA, settings=settings)


# --- fake Playwright (persistent context) to exercise the login flow ------------
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
    def __init__(self) -> None:
        self.visited: list[str] = []
        self.url = ""

    def goto(self, url: str, **kwargs: object) -> None:
        self.visited.append(url)
        self.url = url

    def wait_for_timeout(self, ms: int) -> None:
        pass

    def title(self) -> str:
        return "Search Reward Flights"


class _FakeContext:
    def __init__(self, ok_after: int, cookies: list[dict[str, str]]) -> None:
        self.request = _FakeRequest(ok_after)
        self._cookies = cookies
        self.closed = False
        self.page = _FakePage()

    def new_page(self) -> _FakePage:
        return self.page

    def cookies(self) -> list[dict[str, str]]:
        return self._cookies

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, ctx: _FakeContext, fail_channel: str | None = None) -> None:
        self._ctx = ctx
        self._fail_channel = fail_channel
        self.channels: list[str | None] = []

    def launch_persistent_context(self, user_data_dir: str, **kwargs: object) -> _FakeContext:
        channel = kwargs.get("channel")
        self.channels.append(channel)  # type: ignore[arg-type]
        if channel == self._fail_channel:
            raise RuntimeError("channel unavailable")
        return self._ctx


class _FakePlaywright:
    def __init__(self, ctx: _FakeContext, fail_channel: str | None = None) -> None:
        self.chromium = _FakeChromium(ctx, fail_channel)


class _FakeFactory:
    """Callable returning a context manager that yields the fake Playwright."""

    def __init__(self, ctx: _FakeContext, fail_channel: str | None = None) -> None:
        self._pw = _FakePlaywright(ctx, fail_channel)

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

    captured = login_via_browser(
        BA, settings=settings, timeout_ms=100_000, playwright_factory=_FakeFactory(ctx)
    )

    assert captured == [{"name": "appSession", "value": "x", "domain": "www.avios.com"}]
    assert ctx.closed
    assert ctx.request.calls >= 3  # polled until authenticated, not immediately
    assert ctx.page.visited[-1].endswith("/en-GB/spend-avios/search-reward-flights")


def test_ba_login_waits_for_separate_reward_session(settings: Settings) -> None:
    ctx = _FakeContext(
        ok_after=1,
        cookies=[{"name": "ba_session_id", "value": "x", "domain": "www.avios.com"}],
    )

    with pytest.raises(LoginError, match="reward-flight login"):
        login_via_browser(
            BA,
            settings=settings,
            timeout_ms=4_000,
            playwright_factory=_FakeFactory(ctx),
        )

    assert ctx.page.visited[-1].endswith("/en-GB/spend-avios/search-reward-flights")
    assert ctx.closed


def test_login_times_out_without_returning(settings: Settings) -> None:
    ctx = _FakeContext(
        ok_after=999, cookies=[{"name": "appSession", "value": "x", "domain": "www.avios.com"}]
    )

    with pytest.raises(LoginError, match="Timed out"):
        login_via_browser(
            BA, settings=settings, timeout_ms=6_000, playwright_factory=_FakeFactory(ctx)
        )

    assert ctx.closed  # browser is always cleaned up, even on timeout


def test_open_login_context_prefers_chrome_then_falls_back(tmp_path: object) -> None:
    ctx = _FakeContext(ok_after=1, cookies=[])
    pw = _FakePlaywright(ctx, fail_channel="chrome")
    result = _open_login_context(pw, headless=True, user_data_dir=str(tmp_path))
    assert result is ctx
    assert pw.chromium.channels == ["chrome", None]  # tried Chrome, fell back to Chromium
