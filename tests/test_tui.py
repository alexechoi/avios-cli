"""Tests for the Textual TUI, driven by Textual's Pilot.

The aggregation layer is patched, so these exercise the dashboard's combined
rendering (header + Programme column) without any network.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from textual.widgets import DataTable, Input, Select, TabbedContent
from typer.testing import CliRunner

from avios.accounts import Account
from avios.aggregate import AccountBalance, TaggedTransaction
from avios.cli import app as cli_app
from avios.models import Balance, Transaction
from avios.programmes import get_programme
from avios.rewards import RewardCalendar, RewardSearchQuery
from avios.tui.app import AviosApp
from avios.tui.rewards import RewardFlightsPane
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


class FakeRewardClient:
    def __init__(self, *, fail_inbound: bool = False) -> None:
        self.queries: list[RewardSearchQuery] = []
        self.fail_inbound = fail_inbound

    def search_reward_calendar(self, query: RewardSearchQuery) -> RewardCalendar:
        self.queries.append(query)
        if self.fail_inbound and query.origin == "ABZ":
            raise RuntimeError("inbound failed")
        day = f"{query.month}-05"
        return RewardCalendar.model_validate(
            {
                "origin": query.origin,
                "destination": query.destination,
                "month": query.month,
                "days": {
                    day: {
                        "date": day,
                        "availabilityLevel": 2,
                        "flights": [
                            {
                                "departureAirport": (
                                    "LHR" if query.origin == "LON" else query.origin
                                ),
                                "arrivalAirport": query.destination,
                                "departureTime": f"{day}T06:35:00",
                                "arrivalTime": f"{day}T08:10:00",
                                "duration": 95,
                                "direct": True,
                                "peak": False,
                                "marketing": {"carrier": "BA", "flightNumber": "1300"},
                                "availability": [
                                    {"cabin": "C", "state": "9", "seatsAvailable": 9},
                                    {"cabin": "M", "state": "C", "seatsAvailable": 0},
                                ],
                            }
                        ],
                    },
                    f"{query.month}-06": {
                        "date": f"{query.month}-06",
                        "availabilityLevel": 0,
                        "flights": [],
                    },
                },
            }
        )


def _fill_reward_form(
    app: AviosApp,
    *,
    outbound: str,
    inbound: str = "",
    mode: str = "date",
) -> RewardFlightsPane:
    pane = app.query_one(RewardFlightsPane)
    app.query_one("#flight-mode", Select).value = mode
    app.query_one("#flight-origin", Input).value = "LON"
    app.query_one("#flight-destination", Input).value = "ABZ"
    app.query_one("#flight-outbound", Input).value = outbound
    app.query_one("#flight-return", Input).value = inbound
    return pane


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


async def test_app_mounts_dashboard_and_reward_tabs() -> None:
    client = FakeRewardClient()
    app = AviosApp(accounts=[_account("ba")], reward_client=client)
    async with app.run_test() as pilot:
        await pilot.pause()
        tabs = app.query_one("#main-tabs", TabbedContent)
        assert tabs.active == "dashboard-tab"
        assert app.query_one("#rewards-tab") is not None
        assert app.query_one(RewardFlightsPane) is not None
        assert client.queries == []  # switching/mounting never searches automatically


async def test_reward_tab_exact_date_search() -> None:
    client = FakeRewardClient()
    app = AviosApp(accounts=[_account("ba")], reward_client=client)
    async with app.run_test() as pilot:
        pane = _fill_reward_form(app, outbound="2099-11-05")
        pane.start_search()
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one("#outbound-flights", DataTable)
        assert table.row_count == 1
        assert len(client.queries) == 1
        assert client.queries[0].origin == "LON"
        assert pane.last_status == "Search complete"


async def test_reward_tab_return_reverses_inbound() -> None:
    client = FakeRewardClient()
    app = AviosApp(accounts=[_account("ba")], reward_client=client)
    async with app.run_test() as pilot:
        pane = _fill_reward_form(app, outbound="2099-11-05", inbound="2099-12-05")
        pane.start_search()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert [(query.origin, query.destination) for query in client.queries] == [
            ("LON", "ABZ"),
            ("ABZ", "LON"),
        ]
        inbound = app.query_one("#inbound-flights", DataTable)
        assert inbound.display is True
        assert inbound.row_count == 1


async def test_reward_tab_keeps_outbound_when_inbound_fails() -> None:
    client = FakeRewardClient(fail_inbound=True)
    app = AviosApp(accounts=[_account("ba")], reward_client=client)
    async with app.run_test() as pilot:
        pane = _fill_reward_form(app, outbound="2099-11-05", inbound="2099-12-05")
        pane.start_search()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.query_one("#outbound-flights", DataTable).row_count == 1
        assert app.query_one("#inbound-flights", DataTable).row_count == 1
        assert "1 failed leg" in pane.last_status


async def test_reward_tab_calendar_hides_unavailable_dates() -> None:
    app = AviosApp(accounts=[_account("ba")], reward_client=FakeRewardClient())
    async with app.run_test() as pilot:
        pane = _fill_reward_form(app, outbound="2099-11", mode="calendar")
        pane.start_search()
        await app.workers.wait_for_complete()
        await pilot.pause()
        table = app.query_one("#outbound-flights", DataTable)
        assert table.row_count == 1


async def test_reward_tab_without_ba_account_shows_login_guidance() -> None:
    app = AviosApp(accounts=[])
    async with app.run_test() as pilot:
        pane = _fill_reward_form(app, outbound="2099-11-05")
        pane.start_search()
        await pilot.pause()
        assert "avios login ba" in pane.last_status


def test_cli_tui_launches_app(monkeypatch: pytest.MonkeyPatch) -> None:
    called: dict[str, bool] = {}
    monkeypatch.setattr("avios.tui.app.run", lambda: called.setdefault("ran", True))
    result = CliRunner().invoke(cli_app, ["tui"])
    assert result.exit_code == 0
    assert called.get("ran") is True
