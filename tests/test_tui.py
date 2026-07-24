"""Tests for the Textual TUI, driven by Textual's Pilot."""

from __future__ import annotations

import pytest
from textual.widgets import DataTable
from typer.testing import CliRunner

from avios.cli import app as cli_app
from avios.models import Balance, Transaction
from avios.session import NotAuthenticated
from avios.tui.app import AviosApp
from avios.tui.widgets import BalanceDisplay


class FakeClient:
    def get_balance(self) -> Balance:
        return Balance(balance=100, household_avios_balance=200)

    def get_transactions(self, limit: int | None = None) -> list[Transaction]:
        return [
            Transaction.model_validate({"date": "2026-07-01", "avios": 100}),
            Transaction.model_validate({"date": "2026-06-01", "avios": 50}),
        ]


class EmptyClient(FakeClient):
    def get_transactions(self, limit: int | None = None) -> list[Transaction]:
        return []


class UnauthedClient(FakeClient):
    def get_balance(self) -> Balance:
        raise NotAuthenticated


async def test_dashboard_populates_balance_and_transactions() -> None:
    app = AviosApp(client=FakeClient())  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await app._load()
        await pilot.pause()
        assert "100" in app.query_one("#balance", BalanceDisplay).plain
        table = app.query_one("#transactions", DataTable)
        assert table.row_count == 2


async def test_dashboard_handles_empty_transactions() -> None:
    app = AviosApp(client=EmptyClient())  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await app._load()
        await pilot.pause()
        table = app.query_one("#transactions", DataTable)
        assert table.row_count == 1  # the "No transactions" row


async def test_dashboard_shows_login_hint_when_unauthenticated() -> None:
    app = AviosApp(client=UnauthedClient())  # type: ignore[arg-type]
    async with app.run_test() as pilot:
        await app._load()
        await pilot.pause()
        assert "avios login" in app.query_one("#balance", BalanceDisplay).plain


def test_cli_tui_launches_app(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, bool] = {}
    monkeypatch.setattr("avios.tui.app.run", lambda: called.setdefault("ran", True))
    result = CliRunner().invoke(cli_app, ["tui"])
    assert result.exit_code == 0
    assert called.get("ran") is True
