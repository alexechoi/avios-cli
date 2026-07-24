"""Tests for the CLI command surface.

The client and login helpers are monkeypatched, so these tests exercise argument
parsing, rendering and error handling without any network or browser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from avios import __version__
from avios.auth import ImportResult, LoginError
from avios.cli import app
from avios.models import Balance, Overview, Profile, Transaction
from avios.session import NotAuthenticated, SessionExpired

runner = CliRunner()


class FakeClient:
    """Stand-in for AviosClient with canned data."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_balance(self) -> Balance:
        return Balance(balance=100, individual=100, household=200)

    def get_transactions(self, limit: int = 50) -> list[Transaction]:
        items = [
            Transaction.model_validate(
                {
                    "dateProcessed": "2026-07-01T09:00:00.000Z",
                    "description": "UBER",
                    "type": {"value": "Collection", "id": "DV_COL"},
                    "amount": 100,
                }
            ),
            Transaction.model_validate(
                {
                    "dateProcessed": "2026-06-01T09:00:00.000Z",
                    "description": "Deliveroo UK",
                    "type": {"value": "BA Avios eStore", "id": "BA_AV_ESTORE"},
                    "amount": 50,
                }
            ),
        ]
        return items[:limit]

    def get_pending_transactions(self) -> list[Transaction]:
        return []

    def get_overview(self) -> Overview:
        return Overview.model_validate({"tier": "Blue"})

    def get_profile(self) -> Profile:
        return Profile.model_validate(
            {
                "idToken": "jwt",
                "tokenContent": {
                    "name": "Alex Choi",
                    "https://avios.com/customer_tier_name": "Gold",
                    "https://avios.com/membership_id": "BA123",
                },
            }
        )

    def raw(self, path: str) -> dict[str, str]:
        return {"path": path}


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("avios.cli.AviosClient", FakeClient)


# -- version / help ----------------------------------------------------------
def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.stdout


# -- data commands -----------------------------------------------------------
def test_balance_table(fake_client: None) -> None:
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 0
    assert "100" in result.stdout


def test_balance_json(fake_client: None) -> None:
    result = runner.invoke(app, ["balance", "--json"])
    assert result.exit_code == 0
    assert '"balance"' in result.stdout
    assert '"household"' in result.stdout


def test_transactions_respects_limit(fake_client: None) -> None:
    result = runner.invoke(app, ["transactions", "--limit", "1"])
    assert result.exit_code == 0
    assert "2026-07-01" in result.stdout
    assert "2026-06-01" not in result.stdout


def test_transactions_json(fake_client: None) -> None:
    result = runner.invoke(app, ["transactions", "--json"])
    assert result.exit_code == 0
    assert '"amount"' in result.stdout
    assert '"dateProcessed"' in result.stdout


def test_whoami(fake_client: None) -> None:
    result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 0
    out = " ".join(result.stdout.split())
    assert "Alex Choi" in out
    assert "Gold" in out


def test_raw(fake_client: None) -> None:
    result = runner.invoke(app, ["raw", "/x"])
    assert result.exit_code == 0
    assert '"path"' in result.stdout


# -- error handling ----------------------------------------------------------
def test_not_authenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client(FakeClient):
        def get_balance(self) -> Balance:
            raise NotAuthenticated

    monkeypatch.setattr("avios.cli.AviosClient", Client)
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 1
    assert "Not logged in" in result.stdout


def test_session_expired(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client(FakeClient):
        def get_balance(self) -> Balance:
            raise SessionExpired

    monkeypatch.setattr("avios.cli.AviosClient", Client)
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 2
    assert "Session expired" in result.stdout


# -- login / logout ----------------------------------------------------------
def test_login_from_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "avios.cli.import_from_browser",
        lambda session, browser, profile=None: ImportResult(2, "Default", True),
    )
    monkeypatch.setattr("avios.cli.AviosClient", FakeClient)
    result = runner.invoke(app, ["login", "--from-browser"])
    assert result.exit_code == 0
    assert "Logged in" in result.stdout
    assert "Default" in result.stdout
    assert "Balance" in result.stdout


def test_login_from_browser_unauthenticated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "avios.cli.import_from_browser",
        lambda session, browser, profile=None: ImportResult(20, "Default", False),
    )
    result = runner.invoke(app, ["login", "--from-browser"])
    assert result.exit_code == 2
    normalized = " ".join(result.stdout.split())  # Rich wraps lines
    assert "not a logged-in avios session" in normalized


def test_login_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(session: Any, browser: str, profile: str | None = None) -> ImportResult:
        raise LoginError("nope")

    monkeypatch.setattr("avios.cli.import_from_browser", boom)
    result = runner.invoke(app, ["login", "--from-browser"])
    assert result.exit_code == 1
    assert "nope" in result.stdout


def test_logout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0
    assert "Logged out" in result.stdout
