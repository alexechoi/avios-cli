"""Local storage of logged-in Avios accounts (one per programme).

Each account is a small JSON file at ``<config_dir>/accounts/<slug>.json`` holding
its programme metadata and cookie jar. The pre-multi-account single ``state.json``
is migrated to ``accounts/ba.json`` on first use.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from avios.config import Settings, get_settings
from avios.programmes import DEFAULT_PROGRAMME, Programme, get_programme

if TYPE_CHECKING:  # avoid an import cycle at runtime
    import httpx

    from avios.client import AviosClient
    from avios.finnair import FinnairClient
    from avios.session import Session


@dataclass
class Account:
    """A logged-in account for one programme."""

    slug: str
    opco: str
    base_url: str
    name: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)
    backend: str = "avios"
    token: str | None = None
    api_key: str | None = None

    @classmethod
    def from_programme(
        cls,
        programme: Programme,
        *,
        name: str = "",
        cookies: list[dict[str, Any]] | None = None,
    ) -> Account:
        return cls(
            slug=programme.slug,
            opco=programme.opco,
            base_url=programme.base_url,
            name=name or programme.name,
            cookies=cookies or [],
            backend=programme.backend,
        )

    def session(
        self, settings: Settings | None = None, *, transport: httpx.BaseTransport | None = None
    ) -> Session:
        """Build a :class:`~avios.session.Session` bound to this account."""
        from avios.session import Session

        if self.backend != "avios":
            raise ValueError(f"{self.name or self.slug} does not use an avios.com cookie session")
        return Session(
            settings,
            opco=self.opco,
            base_url=self.base_url,
            cookies=self.cookies,
            transport=transport,
        )

    def client(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> AviosClient | FinnairClient:
        """Build the API client appropriate for this programme's backend."""
        if self.backend == "finnair":
            from avios.finnair import FinnairClient, FinnairSession

            return FinnairClient(
                FinnairSession(
                    self.token or "",
                    self.api_key or "",
                    settings,
                    transport=transport,
                )
            )

        from avios.client import AviosClient

        return AviosClient(self.session(settings, transport=transport))


class AccountStore:
    """Reads/writes accounts under ``<config_dir>/accounts/``."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._migrate_legacy()

    @property
    def dir(self) -> Path:
        return self.settings.config_dir / "accounts"

    def _path(self, slug: str) -> Path:
        return self.dir / f"{slug}.json"

    def _migrate_legacy(self) -> None:
        """Move the old single ``state.json`` to ``accounts/ba.json`` once."""
        legacy = self.settings.state_path
        if not legacy.exists() or self._path(DEFAULT_PROGRAMME).exists():
            return
        try:
            data = json.loads(legacy.read_text())
            cookies = data.get("cookies", []) if isinstance(data, dict) else []
        except (json.JSONDecodeError, OSError):
            cookies = []
        self.save(Account.from_programme(get_programme(DEFAULT_PROGRAMME), cookies=cookies))

    def _load_path(self, path: Path) -> Account:
        data = json.loads(path.read_text())
        return Account(
            slug=data["slug"],
            opco=data["opco"],
            base_url=data["base_url"],
            name=data.get("name", ""),
            cookies=data.get("cookies", []),
            backend=data.get("backend", "avios"),
            token=data.get("token"),
            api_key=data.get("api_key"),
        )

    def list(self) -> list[Account]:
        if not self.dir.exists():
            return []
        accounts = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                accounts.append(self._load_path(path))
            except (json.JSONDecodeError, OSError, KeyError):
                continue
        return accounts

    def get(self, slug: str) -> Account | None:
        path = self._path(slug)
        return self._load_path(path) if path.exists() else None

    def save(self, account: Account) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self._path(account.slug)
        path.write_text(
            json.dumps(
                {
                    "slug": account.slug,
                    "opco": account.opco,
                    "base_url": account.base_url,
                    "name": account.name,
                    "cookies": account.cookies,
                    "backend": account.backend,
                    "token": account.token,
                    "api_key": account.api_key,
                }
            )
        )
        path.chmod(0o600)

    def remove(self, slug: str) -> bool:
        path = self._path(slug)
        if path.exists():
            path.unlink()
            return True
        return False
