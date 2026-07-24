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

import contextlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from avios import endpoints
from avios.config import Settings
from avios.session import Session, cookie_header_from

DASHBOARD_PATH = "/manage-avios/dashboard"
LOGIN_TIMEOUT_MS = 300_000  # 5 minutes to complete password + captcha + MFA
LOGIN_POLL_MS = 2_000  # how often to check whether the session is authenticated
SUPPORTED_BROWSERS = ("chrome", "firefox", "edge", "brave", "safari", "chromium")

# Chromium-family user-data roots per browser (relative to home), used to discover
# per-profile cookie databases (Default, "Profile 1", ...).
_CHROMIUM_ROOTS = {
    "chrome": [
        "Library/Application Support/Google/Chrome",
        ".config/google-chrome",
        "AppData/Local/Google/Chrome/User Data",
    ],
    "brave": [
        "Library/Application Support/BraveSoftware/Brave-Browser",
        ".config/BraveSoftware/Brave-Browser",
        "AppData/Local/BraveSoftware/Brave-Browser/User Data",
    ],
    "edge": [
        "Library/Application Support/Microsoft Edge",
        ".config/microsoft-edge",
        "AppData/Local/Microsoft/Edge/User Data",
    ],
    "chromium": [
        "Library/Application Support/Chromium",
        ".config/chromium",
        "AppData/Local/Chromium/User Data",
    ],
}


class LoginError(RuntimeError):
    """Login could not be completed."""


@dataclass
class ImportResult:
    """Outcome of importing cookies from a browser."""

    count: int
    profile: str | None
    authenticated: bool


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
    playwright_factory: Any = None,
) -> int:
    """Open a browser, wait until the user is actually logged in, save the cookie.

    Returns the number of avios.com cookies captured. ``playwright_factory`` is an
    injection point for tests; production uses the real ``sync_playwright``.
    """
    session = session or Session()
    factory = playwright_factory or _import_sync_playwright()
    base_url = session.settings.base_url
    profile_dir = str(session.settings.config_dir / "chrome-profile")

    with factory() as pw:
        ctx = _open_login_context(pw, headless=headless, user_data_dir=profile_dir)
        page = ctx.new_page()
        page.goto(f"{base_url}{DASHBOARD_PATH}")
        authed = _wait_for_auth(ctx, page, base_url, timeout_ms)
        cookies = list(ctx.cookies())
        ctx.close()

    if not authed:
        raise LoginError("Timed out waiting for login. Run `avios login` again.")
    session.save_cookies(cookies)
    return len(_only_avios(cookies))


def _import_sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise LoginError(
            "Playwright isn't installed. Log in using the 'login' extra:\n"
            "  uvx --from 'avios-cli[login]' avios login\n"
            "  (first time only: uvx --from playwright playwright install chromium)\n"
            "Or import the cookie from Chrome instead (no browser download):\n"
            "  uvx --from 'avios-cli[login]' avios login --from-browser"
        ) from exc
    return sync_playwright


def _wait_for_auth(ctx: Any, page: Any, base_url: str, timeout_ms: int) -> bool:
    """Poll the auth endpoint until the session is authenticated (or time out).

    The dashboard is a client-side SPA, so its URL is NOT a reliable signal — it
    matches the moment we navigate, before login. ``/auth-gateway/user`` returns
    401 until logged in, then 200.
    """
    for _ in range(max(1, timeout_ms // LOGIN_POLL_MS)):
        if _is_authenticated(ctx, base_url):
            return True
        page.wait_for_timeout(LOGIN_POLL_MS)
    return False


def _is_authenticated(ctx: Any, base_url: str) -> bool:
    try:
        return bool(ctx.request.get(f"{base_url}{endpoints.AUTH_USER}").ok)
    except Exception:
        return False


def _open_login_context(pw: Any, *, headless: bool, user_data_dir: str) -> Any:
    """Open a browser context for login, tuned to avoid the hCaptcha bot loop.

    hCaptcha/Akamai serve endless challenges to obviously-automated browsers, so:
    - prefer the user's real Chrome (``channel="chrome"``) over bundled Chromium;
    - disable the automation fingerprint (``navigator.webdriver`` /
      ``--enable-automation``);
    - use a **persistent profile** so cookies and captcha reputation carry over
      between attempts (a brand-new, empty profile looks high-risk).
    """
    args = ["--disable-blink-features=AutomationControlled"]
    ignore_default_args = ["--enable-automation"]
    for extra in ({"channel": "chrome"}, {}):
        try:
            return pw.chromium.launch_persistent_context(
                user_data_dir,
                headless=headless,
                args=args,
                ignore_default_args=ignore_default_args,
                **extra,
            )
        except Exception:
            continue
    raise LoginError(
        "Couldn't launch a browser. Install Chromium once with:\n"
        "  uvx --from playwright playwright install chromium"
    )


def _bc3() -> Any:
    try:
        import browser_cookie3 as bc3
    except ImportError as exc:
        raise LoginError(
            "browser-cookie3 isn't installed. Run with the 'login' extra:\n"
            "  uvx --from 'avios-cli[login]' avios login --from-browser"
        ) from exc
    return bc3


def _browser_fn(bc3: Any, browser: str) -> Any:
    fn = getattr(bc3, browser, None)
    if fn is None:
        raise LoginError(
            f"Unknown browser '{browser}'. Choose from: {', '.join(SUPPORTED_BROWSERS)}"
        )
    return fn


def _profile_cookie_files(browser: str, profile: str | None) -> list[tuple[str, Path]]:
    """Discover (profile_name, Cookies-db) pairs for a chromium-family browser."""
    found: list[tuple[str, Path]] = []
    for rel in _CHROMIUM_ROOTS.get(browser, []):
        root = Path.home() / rel
        if not root.exists():
            continue
        for prof in sorted(p for p in root.iterdir() if p.is_dir()):
            if profile and prof.name != profile:
                continue
            for db in (prof / "Cookies", prof / "Network" / "Cookies"):
                if db.exists():
                    found.append((prof.name, db))
                    break
    return found


def _candidate_cookie_sets(
    browser: str, profile: str | None
) -> list[tuple[str, list[dict[str, Any]]]]:
    """Return [(profile_name, avios_cookies)] to try, most specific first."""
    bc3 = _bc3()
    fn = _browser_fn(bc3, browser)
    candidates: list[tuple[str, list[dict[str, Any]]]] = []
    for name, db in _profile_cookie_files(browser, profile):
        try:
            raw = fn(domain_name="avios.com", cookie_file=str(db))
        except Exception:
            continue
        candidates.append((name, _only_avios(_to_cookie_dicts(raw))))
    if not profile:
        # Fallback to browser_cookie3's own default lookup.
        with contextlib.suppress(Exception):
            raw = fn(domain_name="avios.com")
            candidates.append(("default", _only_avios(_to_cookie_dicts(raw))))
    return candidates


def _cookies_authenticate(settings: Settings, cookies: list[dict[str, Any]]) -> bool:
    """True if these cookies yield a 200 from the auth endpoint."""
    header = cookie_header_from(cookies)
    if not header:
        return False
    try:
        with httpx.Client(
            base_url=settings.base_url,
            timeout=15.0,
            follow_redirects=False,
            headers={
                "accept": "application/json",
                "user-agent": settings.user_agent,
                "cookie": header,
            },
        ) as client:
            return client.get(endpoints.AUTH_USER).status_code == 200
    except httpx.HTTPError:
        return False


def import_from_browser(
    session: Session | None = None,
    browser: str = "chrome",
    *,
    profile: str | None = None,
    loader: Callable[[], Iterable[_RawCookie]] | None = None,
    authenticator: Callable[[list[dict[str, Any]]], bool] | None = None,
) -> ImportResult:
    """Import the avios.com session cookie from a browser profile.

    Scans the browser's profiles, prefers the one whose cookies actually
    authenticate, and saves them. ``loader``/``authenticator`` are test seams.
    """
    session = session or Session()
    authenticate = authenticator or (lambda c: _cookies_authenticate(session.settings, c))

    if loader is not None:
        candidates: list[tuple[str, list[dict[str, Any]]]] = [
            ("browser", _only_avios(_to_cookie_dicts(loader())))
        ]
    else:
        candidates = _candidate_cookie_sets(browser, profile)

    working: tuple[str, list[dict[str, Any]]] | None = None
    fallback: tuple[str, list[dict[str, Any]]] | None = None
    for name, cookies in candidates:
        if not cookies:
            continue
        if fallback is None:
            fallback = (name, cookies)
        if authenticate(cookies):
            working = (name, cookies)
            break

    chosen = working or fallback
    if chosen is None:
        where = f"profile '{profile}'" if profile else browser
        raise LoginError(
            f"No avios.com cookies found in {where}. Log into avios.com in that "
            "browser first (and check the profile with --profile)."
        )
    name, cookies = chosen
    session.save_cookies(cookies)
    return ImportResult(count=len(cookies), profile=name, authenticated=working is not None)
