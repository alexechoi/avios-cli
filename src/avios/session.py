"""Session and cookie handling for avios.

avios.com authenticates its internal JSON endpoints with a browser session cookie
(no bearer token, no request signing). This module stores that cookie jar and
builds pre-authenticated :class:`httpx.Client` instances. Acquiring the cookie in
the first place (browser-assisted login) lives in :mod:`avios.auth`.
"""

from __future__ import annotations

import json
import os
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
    """Persisted avios session backed by a cookie jar on disk.

    A ``transport`` may be injected for testing (e.g. ``httpx.MockTransport``).
    The ``AVIOS_COOKIE`` environment variable, if set, overrides the stored jar
    with a raw ``Cookie`` header string.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self._transport = transport

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
        return cookie_header_from(self.load_cookies())

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
            "referer": f"{self.settings.base_url}/manage-avios/dashboard",
            "cookie": cookie,
        }
        return httpx.Client(
            base_url=self.settings.base_url,
            headers=headers,
            timeout=self.settings.request_timeout,
            follow_redirects=False,
            transport=self._transport,
        )

    def get_json(self, path: str) -> Any:
        """GET ``path`` and return parsed JSON.

        A redirect to the auth gateway (301/302) or a 401 means the session is no
        longer valid, surfaced as :class:`SessionExpired`.
        """
        with self.client() as client:
            response = client.get(path)
        if response.status_code in (301, 302, 401):
            raise SessionExpired("Session expired. Run `avios login` again.")
        response.raise_for_status()
        ctype = response.headers.get("content-type", "")
        return response.json() if "json" in ctype else response.text
