"""Runtime configuration for avios.

Settings are resolved from defaults and ``AVIOS_``-prefixed environment variables
(e.g. ``AVIOS_BASE_URL``, ``AVIOS_CONFIG_DIR``). The session cookie itself is not
stored here — see :mod:`avios.session`.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_BASE_URL = "https://www.avios.com"

# A realistic desktop-Chrome UA. The internal endpoints are cookie-authenticated
# and don't sign requests, but sending a browser-like UA avoids trivial filtering.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _default_config_dir() -> Path:
    """Return the XDG config directory for avios (``~/.config/avios`` by default)."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "avios"


class Settings(BaseSettings):
    """Resolved runtime settings."""

    model_config = SettingsConfigDict(env_prefix="AVIOS_", extra="ignore")

    base_url: str = DEFAULT_BASE_URL
    user_agent: str = DEFAULT_USER_AGENT
    config_dir: Path = Field(default_factory=_default_config_dir)
    request_timeout: float = 20.0
    # Operating company code sent as `x-avios-opco`; required by the manage-avios
    # API. "BAEC" = British Airways Executive Club (override with AVIOS_OPCO).
    opco: str = "BAEC"

    @property
    def state_path(self) -> Path:
        """Path to the saved session file (cookie jar)."""
        return self.config_dir / "state.json"


def get_settings() -> Settings:
    """Build a fresh :class:`Settings` (reads the environment each call)."""
    return Settings()
