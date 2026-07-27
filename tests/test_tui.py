"""Tests for the Textual TUI, driven by Textual's Pilot.

The aggregation layer is patched, so these exercise the dashboard's combined
rendering (header + Programme column) without any network.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from textual.widgets import DataTable
from typer.testing import CliRunner

from avios.accounts import Account
from avios.aggregate import AccountBalance, TaggedTransaction
from avios.cli import app as cli_app
from avios.models import Balance, Transaction
from avios.programmes import get_programme
from avios.tui.app import AviosApp
from avios.tui.widgets import BalanceDisplay


def _account(slug: str = "ba") -> Account:
    return Account.from_programme(get_programme(slug), cookies=[{"name": "s", "value": slug}])


def _balance(account: Account, value: int = 100) -> AccountBalance:
    return AccountBalance(account, Balance(balance=value, individual=value, household=value * 2))


def _txn(account: Account, date: str, desc: str, amount: int) -> TaggedTransaction:
    return TaggedTransaction(
        account,
        Transaction.model_validate({"dateProcessed": date, "description": desc, "amount": amount}),
    )


@pytest.fixture
def patch_aggregate(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def _apply(balances: list[AccountBalance], transactions: list[TaggedTransaction]) -> None:
        monkeypatch.setattr(
            "avios.aggregate.all_balances", lambda accounts, *, settings=None: balances
        )
        monkeypatch.setattr(
            "avios.aggregate.merged_transactions",
            lambda accounts, *, limit_per=50, settings=None: transactions,
        )

    return _apply


async def test_dashboard_single_account(patch_aggregate: Callable[..., None]) -> None:
    ba = _account("ba")
    patch_aggregate([_balance(ba)], [_txn(ba, "2026-07-01T09:00:00Z", "UBER", 100)])
    app = AviosApp(accounts=[ba])
    async with app.run_test() as pilot:
        await app._load()
        await pilot.pause()
        assert "100" in app.query_one("#balance", BalanceDisplay).last_text
        table = app.query_one("#transactions", DataTable)
        assert table.row_count == 1
        assert len(table.columns) == 4  # no Programme column for a single account


async def test_dashboard_combined_two_accounts(patch_aggregate: Callable[..., None]) -> None:
    ba, iberia = _account("ba"), _account("iberia")
    patch_aggregate(
        [_balance(ba, 100), _balance(iberia, 50)],
        [
            _txn(iberia, "2026-07-15T09:00:00Z", "Vueling", 20),
            _txn(ba, "2026-07-01T09:00:00Z", "UBER", 100),
        ],
    )
    app = AviosApp(accounts=[ba, iberia])
    async with app.run_test() as pilot:
        await app._load()
        await pilot.pause()
        header = app.query_one("#balance", BalanceDisplay).last_text
        assert "Combined" in header
        assert "150" in header  # 100 + 50
        assert "British Airways" in header and "Iberia" in header
        table = app.query_one("#transactions", DataTable)
        assert table.row_count == 2
        assert len(table.columns) == 5  # Programme column added


async def test_dashboard_empty_transactions(patch_aggregate: Callable[..., None]) -> None:
    ba = _account("ba")
    patch_aggregate([_balance(ba)], [])
    app = AviosApp(accounts=[ba])
    async with app.run_test() as pilot:
        await app._load()
        await pilot.pause()
        table = app.query_one("#transactions", DataTable)
        assert table.row_count == 1  # the "No transactions" row


async def test_dashboard_all_sessions_expired(patch_aggregate: Callable[..., None]) -> None:
    ba = _account("ba")
    patch_aggregate([AccountBalance(ba, balance=None, error="session expired")], [])
    app = AviosApp(accounts=[ba])
    async with app.run_test() as pilot:
        await app._load()
        await pilot.pause()
        assert "avios login" in app.query_one("#balance", BalanceDisplay).last_text


async def test_dashboard_no_accounts() -> None:
    app = AviosApp(accounts=[])
    async with app.run_test() as pilot:
        await app._load()
        await pilot.pause()
        assert "No accounts" in app.query_one("#balance", BalanceDisplay).last_text


def test_banner_text_contains_art_and_tagline() -> None:
    from avios.tui.art import banner_text

    plain = banner_text().plain
    assert "█" in plain
    assert "your Avios, in the terminal" in plain


async def test_dashboard_mounts_banner() -> None:
    app = AviosApp(accounts=[_account("ba")])
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.query_one("#banner") is not None


def test_cli_tui_launches_app(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, bool] = {}
    monkeypatch.setattr("avios.tui.app.run", lambda: called.setdefault("ran", True))
    result = CliRunner().invoke(cli_app, ["tui"])
    assert result.exit_code == 0
    assert called.get("ran") is True
