"""Browser-assisted login for avios.

avios.com has no plain credential API: login is Auth0 "Universal Login" guarded by
hCaptcha, Akamai Bot Manager and SMS/passkey MFA, so there is no way to POST a
username/password over HTTP and obtain a session. Instead we let a real browser
handle the login once and capture the resulting session cookie.

Two strategies, both requiring the optional ``login`` extra
(``pip install "avios-cli[login]"``):

- :func:`login_via_browser` — open a Playwright browser, the user logs in
  (password + captcha + MFA), and we grab the cookies. Also needs
  ``playwright install chromium``.
- :func:`import_from_browser` — read the avios.com cookie straight out of a
  running browser via ``browser_cookie3`` (no popup) if already logged in there.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, Protocol

from avios.session import Session

DASHBOARD_PATH = "/manage-avios/dashboard"
LOGIN_TIMEOUT_MS = 300_000  # 5 minutes to complete password + captcha + MFA
SUPPORTED_BROWSERS = ("chrome", "firefox", "edge", "brave", "safari", "chromium")


class LoginError(RuntimeError):
    """Login could not be completed."""


class _RawCookie(Protocol):
    name: str
    value: str
    domain: str


def _to_cookie_dicts(raw: Iterable[_RawCookie]) -> list[dict[str, Any]]:
    """Convert browser_cookie3 / cookiejar entries to our stored cookie format."""
    return [{"name": c.name, "value": c.value, "domain": c.domain} for c in raw]


def _only_avios(cookies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [c for c in cookies if "avios.com" in c.get("domain", "")]


def login_via_browser(
    session: Session | None = None,
    *,
    headless: bool = False,
    timeout_ms: int = LOGIN_TIMEOUT_MS,
) -> int:
    """Open a browser, wait for the user to log in, and save the session cookie.

    Returns the number of avios.com cookies captured.
    """
    session = session or Session()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LoginError(
            'Playwright is required. Install with: pip install "avios-cli[login]" '
            "then: playwright install chromium"
        ) from exc

    base_url = session.settings.base_url
    with sync_playwright() as pw:  # pragma: no cover - requires a real browser
        browser = pw.chromium.launch(headless=headless)
        ctx = browser.new_context(user_agent=session.settings.user_agent)
        page = ctx.new_page()
        page.goto(f"{base_url}{DASHBOARD_PATH}")
        try:
            page.wait_for_url("**/manage-avios/**", timeout=timeout_ms)
            page.wait_for_timeout(1500)  # let the session cookie settle
        except Exception as exc:
            browser.close()
            raise LoginError("Did not reach the dashboard in time. Please try again.") from exc
        cookies = list(ctx.cookies())
        browser.close()

    session.save_cookies(cookies)
    return len(_only_avios(cookies))


def _default_loader(browser: str) -> Callable[[], Iterable[_RawCookie]]:
    """Return a callable that reads avios.com cookies from ``browser``."""
    try:
        import browser_cookie3 as bc3
    except ImportError as exc:
        raise LoginError(
            'browser-cookie3 is required. Install with: pip install "avios-cli[login]"'
        ) from exc
    fn = getattr(bc3, browser, None)
    if fn is None:
        raise LoginError(
            f"Unknown browser '{browser}'. Choose from: {', '.join(SUPPORTED_BROWSERS)}"
        )
    return lambda: fn(domain_name="avios.com")


def import_from_browser(
    session: Session | None = None,
    browser: str = "chrome",
    *,
    loader: Callable[[], Iterable[_RawCookie]] | None = None,
) -> int:
    """Import the avios.com session cookie from a running browser.

    Returns the number of avios.com cookies imported.
    """
    session = session or Session()
    load = loader or _default_loader(browser)
    cookies = _only_avios(_to_cookie_dicts(load()))
    if not cookies:
        raise LoginError(
            f"No avios.com cookies found in {browser}. Log into avios.com there first."
        )
    session.save_cookies(cookies)
    return len(cookies)
