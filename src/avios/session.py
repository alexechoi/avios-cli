"""Session and cookie handling for avios.

avios.com authenticates its internal JSON endpoints with a browser session cookie
(no bearer token, no request signing). This module stores that cookie jar and
builds pre-authenticated :class:`httpx.Client` instances. BA reward search is the
exception: its Akamai-protected web flow uses Chrome directly. Acquiring cookies
in the first place (browser-assisted login) lives in :mod:`avios.auth`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from avios.config import Settings, get_settings


class SessionError(RuntimeError):
    """Base class for session problems."""


class NotAuthenticated(SessionError):
    """No stored session is available; the user must log in."""


class SessionExpired(SessionError):
    """The stored session is no longer valid; the user must log in again."""


def cookie_header_from(cookies: list[dict[str, Any]]) -> str:
    """Build a ``Cookie`` header from stored cookies, scoped to avios.com."""
    return "; ".join(
        f"{c['name']}={c['value']}" for c in cookies if "avios.com" in c.get("domain", "")
    )


class Session:
    """A pre-authenticated avios session.

    Two modes:

    - *account mode* — pass ``opco``/``base_url``/``cookies`` (used by multi-account
      code; cookies live in memory and are persisted by ``AccountStore``).
    - *legacy mode* — no account args; falls back to ``Settings`` and the single
      ``state.json`` cookie file.

    A ``transport`` may be injected for testing (e.g. ``httpx.MockTransport``).
    The ``AVIOS_COOKIE`` environment variable, if set, overrides the cookie jar.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        opco: str | None = None,
        base_url: str | None = None,
        cookies: list[dict[str, Any]] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._opco = opco
        self._base_url = base_url
        self._cookies = cookies  # in-memory jar (account mode); None -> use file
        self._transport = transport

    @property
    def opco(self) -> str:
        return self._opco or self.settings.opco

    @property
    def base_url(self) -> str:
        return self._base_url or self.settings.base_url

    @property
    def state_path(self) -> Path:
        return self.settings.state_path

    # -- cookie storage -------------------------------------------------------
    def save_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """Persist a Playwright/browser-style cookie list to ``state.json`` (0600)."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"cookies": cookies}))
        self.state_path.chmod(0o600)

    def load_cookies(self) -> list[dict[str, Any]]:
        if not self.state_path.exists():
            return []
        data = json.loads(self.state_path.read_text())
        cookies = data.get("cookies", [])
        return cookies if isinstance(cookies, list) else []

    def cookie_header(self) -> str:
        env = os.environ.get("AVIOS_COOKIE")
        if env:
            return env.strip()
        cookies = self._cookies if self._cookies is not None else self.load_cookies()
        return cookie_header_from(cookies)

    def browser_cookies(self) -> list[dict[str, Any]]:
        """Return stored structured cookies suitable for seeding a browser context."""
        cookies = self._cookies if self._cookies is not None else self.load_cookies()
        return [dict(cookie) for cookie in cookies]

    @property
    def has_custom_transport(self) -> bool:
        """Whether requests are routed through an injected transport (normally tests)."""
        return self._transport is not None

    def is_authenticated(self) -> bool:
        return bool(self.cookie_header())

    def clear(self) -> None:
        """Remove the stored session (logout)."""
        self.state_path.unlink(missing_ok=True)

    # -- HTTP -----------------------------------------------------------------
    def client(self) -> httpx.Client:
        """Return a pre-authenticated client. Raises :class:`NotAuthenticated`."""
        cookie = self.cookie_header()
        if not cookie:
            raise NotAuthenticated("No saved session. Run `avios login` first.")
        headers = {
            "accept": "application/json, text/plain, */*",
            "user-agent": self.settings.user_agent,
            "referer": f"{self.base_url}/manage-avios/dashboard",
            # Required by the manage-avios API (401 without it).
            "x-avios-opco": self.opco,
            "cookie": cookie,
        }
        return httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=self.settings.request_timeout,
            follow_redirects=False,
            transport=self._transport,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Send an authenticated request and apply common session-expiry handling."""
        query_params: httpx.QueryParams | None
        if isinstance(params, list):
            query_params = httpx.QueryParams(tuple(params))
        else:
            query_params = httpx.QueryParams(params) if params is not None else None
        try:
            with self.client() as client:
                response = client.request(
                    method,
                    path,
                    params=query_params,
                    headers=headers,
                )
        except httpx.TimeoutException as exc:
            raise SessionExpired(
                "Request timed out — your session has probably expired. Run `avios login` again."
            ) from exc
        if response.status_code in (301, 302, 401):
            raise SessionExpired("Session expired. Run `avios login` again.")
        response.raise_for_status()
        return response

    def get_json(self, path: str) -> Any:
        """GET ``path`` and return parsed JSON.

        A redirect to the auth gateway (301/302) or a 401 means the session is no
        longer valid, surfaced as :class:`SessionExpired`. avios.com also *hangs*
        requests from an expired session, so a timeout is treated the same way.
        """
        response = self.request("GET", path)
        ctype = response.headers.get("content-type", "")
        return response.json() if "json" in ctype else response.text

    def get_text(
        self,
        path: str,
        *,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> str:
        """GET a text endpoint with optional query parameters and request headers."""
        return self.request("GET", path, params=params, headers=headers).text
