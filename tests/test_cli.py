"""Tests for the CLI command surface.

The client and login helpers are monkeypatched, so these tests exercise argument
parsing, rendering and error handling without any network or browser.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from avios import __version__
from avios.accounts import Account, AccountStore
from avios.aggregate import AccountBalance, TaggedTransaction
from avios.auth import ImportResult, LoginError
from avios.cli import app
from avios.finnair import FinnairCredentials
from avios.models import Balance, Overview, Profile, Transaction
from avios.programmes import get_programme
from avios.rewards import RewardCalendar, RewardLeg, RewardSearchQuery
from avios.session import NotAuthenticated, SessionExpired

runner = CliRunner()


def _query(origin: str, destination: str, month: str) -> RewardSearchQuery:
    return RewardSearchQuery(origin=origin, destination=destination, month=month)


def _fake_calendar(leg: RewardLeg) -> RewardCalendar:
    """A one-flight month in the shape the new RSC parser produces.

    Business availability uses ``J``, and flights sit under ``journeys`` — both
    changed when BA rebuilt the finder, so the fake has to match.
    """
    query = leg.query
    day = f"{query.month}-05"
    priced = leg.departure_date is not None
    return RewardCalendar.model_validate(
        {
            "origin": query.origin,
            "destination": query.destination,
            "month": query.month,
            "originCityName": "London" if query.origin == "LON" else "Aberdeen",
            "destinationCityName": "Aberdeen" if query.destination == "ABZ" else "London",
            "monthsAvailable": [query.month],
            "priced": priced,
            "days": {
                day: {
                    "date": day,
                    "availabilityLevel": 2,
                    "journeys": [
                        {
                            "journeyType": "outbound",
                            "totalDuration": 95,
                            "direct": True,
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
                                        {
                                            "cabin": "J",
                                            "rbd": "U",
                                            "state": "9",
                                            "seatsAvailable": 9,
                                            "price": (
                                                {
                                                    "pricingRowKey": "k",
                                                    "cabinCode": "J",
                                                    "adult": 50000,
                                                }
                                                if priced
                                                else None
                                            ),
                                        },
                                        {
                                            "cabin": "M",
                                            "rbd": "X",
                                            "state": "C",
                                            "seatsAvailable": 0,
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                },
                f"{query.month}-06": {
                    "date": f"{query.month}-06",
                    "availabilityLevel": 0,
                    "journeys": [],
                },
            },
        }
    )


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

    def search_reward_legs(self, legs: Sequence[RewardLeg]) -> list[RewardCalendar | Exception]:
        return [_fake_calendar(leg) for leg in legs]

    def raw(self, path: str) -> dict[str, str]:
        return {"path": path}


@pytest.fixture
def account_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AccountStore:
    """An isolated store with one logged-in BA account (data commands need one)."""
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    store = AccountStore()
    store.save(Account.from_programme(get_programme("ba"), cookies=[{"name": "s", "value": "1"}]))
    return store


@pytest.fixture
def fake_client(account_store: AccountStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("avios.cli.AviosClient", FakeClient)


@pytest.fixture
def two_accounts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AccountStore:
    """An isolated store with two logged-in accounts (BA + Iberia)."""
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    store = AccountStore()
    store.save(Account.from_programme(get_programme("ba"), cookies=[{"name": "s", "value": "1"}]))
    store.save(
        Account.from_programme(get_programme("iberia"), cookies=[{"name": "s", "value": "2"}])
    )
    return store


def _fake_all_balances(accounts: list[Account], *, settings: Any = None) -> list[AccountBalance]:
    return [
        AccountBalance(a, Balance(balance=100, individual=100, household=200)) for a in accounts
    ]


def _fake_merged(
    accounts: list[Account], *, limit_per: int = 50, settings: Any = None
) -> list[TaggedTransaction]:
    out: list[TaggedTransaction] = []
    for a in accounts:
        out.append(
            TaggedTransaction(
                a,
                Transaction.model_validate(
                    {"dateProcessed": "2026-07-01T09:00:00Z", "description": "UBER", "amount": 100}
                ),
            )
        )
        out.append(
            TaggedTransaction(
                a,
                Transaction.model_validate(
                    {
                        "dateProcessed": "2026-06-01T09:00:00Z",
                        "description": "Deliveroo",
                        "amount": 50,
                    }
                ),
            )
        )
    out.sort(key=lambda t: t.transaction.date_processed or "", reverse=True)
    return out


@pytest.fixture
def fake_aggregate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("avios.aggregate.all_balances", _fake_all_balances)
    monkeypatch.setattr("avios.aggregate.merged_transactions", _fake_merged)
    monkeypatch.setattr("avios.aggregate.merged_pending", lambda accounts, *, settings=None: [])


# -- version / help ----------------------------------------------------------
def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    assert "Usage" in result.stdout


# -- data commands (single account) ------------------------------------------
def test_balance_single_shows_detail(account_store: AccountStore, fake_aggregate: None) -> None:
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 0
    out = " ".join(result.stdout.split())
    assert "100" in out
    assert "Household" in out  # single-account view keeps the detail breakdown


def test_balance_json(account_store: AccountStore, fake_aggregate: None) -> None:
    result = runner.invoke(app, ["balance", "--json"])
    assert result.exit_code == 0
    assert '"balance"' in result.stdout
    assert '"programme"' in result.stdout
    assert '"slug"' in result.stdout


def test_transactions_respects_limit(account_store: AccountStore, fake_aggregate: None) -> None:
    result = runner.invoke(app, ["transactions", "--limit", "1"])
    assert result.exit_code == 0
    assert "2026-07-01" in result.stdout
    assert "2026-06-01" not in result.stdout


def test_transactions_single_has_no_programme_column(
    account_store: AccountStore, fake_aggregate: None
) -> None:
    result = runner.invoke(app, ["transactions"])
    assert result.exit_code == 0
    assert "Programme" not in result.stdout  # only shown when >1 account


def test_transactions_json(account_store: AccountStore, fake_aggregate: None) -> None:
    result = runner.invoke(app, ["transactions", "--json"])
    assert result.exit_code == 0
    assert '"amount"' in result.stdout
    assert '"dateProcessed"' in result.stdout
    assert '"programme"' in result.stdout


# -- data commands (multiple accounts / combined) ----------------------------
def test_balance_combined_across_accounts(two_accounts: AccountStore, fake_aggregate: None) -> None:
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 0
    out = " ".join(result.stdout.split())
    assert "British Airways" in out
    assert "Iberia" in out
    assert "Combined" in out
    assert "200" in out  # 100 + 100


def test_balance_account_filter(two_accounts: AccountStore, fake_aggregate: None) -> None:
    result = runner.invoke(app, ["balance", "--account", "iberia"])
    assert result.exit_code == 0
    out = " ".join(result.stdout.split())
    # Filtered to one account -> single detailed view, no Combined row.
    assert "Combined" not in out


def test_balance_unknown_account(two_accounts: AccountStore, fake_aggregate: None) -> None:
    result = runner.invoke(app, ["balance", "--account", "nope"])
    assert result.exit_code == 1


def test_accounts_lists_roster(two_accounts: AccountStore, fake_aggregate: None) -> None:
    result = runner.invoke(app, ["accounts"])
    assert result.exit_code == 0
    out = " ".join(result.stdout.split())
    assert "British Airways" in out
    assert "iberia" in out  # slug column
    assert "Combined" in out


def test_transactions_merged_shows_programme_column(
    two_accounts: AccountStore, fake_aggregate: None
) -> None:
    result = runner.invoke(app, ["transactions"])
    assert result.exit_code == 0
    out = " ".join(result.stdout.split())
    assert "Programme" in out
    assert "British Airways" in out
    assert "Iberia" in out


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


# -- reward flights ----------------------------------------------------------
def test_flights_exact_date_available_only(account_store: AccountStore, fake_client: None) -> None:
    result = runner.invoke(
        app,
        [
            "flights",
            "LON",
            "ABZ",
            "--date",
            "2099-11-05",
            "--cabin",
            "business",
        ],
    )
    assert result.exit_code == 0
    assert "Outbound" in result.stdout
    assert "LON → ABZ" in result.stdout
    assert "BA1300" in result.stdout
    assert "9" in result.stdout


def test_flights_calendar_hides_unavailable_days(
    account_store: AccountStore, fake_client: None
) -> None:
    result = runner.invoke(app, ["flights", "LON", "ABZ", "--month", "2099-11"])
    assert result.exit_code == 0
    assert "2099-11-05" in result.stdout
    assert "2099-11-06" not in result.stdout
    assert "1 flight" in result.stdout


def test_flights_calendar_can_show_unavailable(
    account_store: AccountStore, fake_client: None
) -> None:
    result = runner.invoke(
        app, ["flights", "LON", "ABZ", "--month", "2099-11", "--show-unavailable"]
    )
    assert result.exit_code == 0
    assert "2099-11-05" in result.stdout
    assert "2099-11-06" in result.stdout


def _recording_client(
    monkeypatch: pytest.MonkeyPatch,
    results: list[RewardCalendar | Exception] | None = None,
) -> list[RewardLeg]:
    """Swap in a client that records the legs the CLI asked for."""
    calls: list[RewardLeg] = []

    class RecordingClient(FakeClient):
        def search_reward_legs(self, legs: Sequence[RewardLeg]) -> list[RewardCalendar | Exception]:
            calls.extend(legs)
            return results if results is not None else [_fake_calendar(leg) for leg in legs]

    monkeypatch.setattr("avios.cli.AviosClient", RecordingClient)
    return calls


def test_flights_return_reverses_route_in_one_session(
    account_store: AccountStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _recording_client(monkeypatch)

    result = runner.invoke(
        app,
        ["flights", "LON", "ABZ", "--date", "2099-11-05", "--return-date", "2099-12-05"],
    )

    assert result.exit_code == 0
    assert "Outbound" in result.stdout and "Inbound" in result.stdout
    # Both legs in a single call, so only one browser session is warmed.
    assert [(leg.query.origin, leg.query.destination) for leg in calls] == [
        ("LON", "ABZ"),
        ("ABZ", "LON"),
    ]
    assert [leg.departure_date for leg in calls] == ["2099-11-05", "2099-12-05"]


def test_flights_calendar_mode_does_not_request_pricing(
    account_store: AccountStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only exact-date searches ask for prices — one fewer request per leg."""
    calls = _recording_client(monkeypatch)

    result = runner.invoke(app, ["flights", "LON", "ABZ", "--month", "2099-11"])

    assert result.exit_code == 0
    assert [leg.departure_date for leg in calls] == [None]


def test_flights_shows_avios_prices_for_an_exact_date(
    account_store: AccountStore, fake_client: None
) -> None:
    result = runner.invoke(app, ["flights", "LON", "ABZ", "--date", "2099-11-05"])
    assert result.exit_code == 0
    assert "9 · 50,000" in result.stdout
    assert "seats · Avios" in result.stdout


def test_flights_reports_a_failed_leg_and_keeps_the_other(
    account_store: AccountStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    outbound = _fake_calendar(RewardLeg(_query("LON", "ABZ", "2099-11")))
    _recording_client(monkeypatch, [outbound, RuntimeError("Akamai said no")])

    result = runner.invoke(
        app,
        ["flights", "LON", "ABZ", "--date", "2099-11-05", "--return-date", "2099-12-05"],
    )

    assert result.exit_code == 0
    assert "Inbound search failed" in result.stdout
    assert "Akamai said no" in result.stdout
    assert "BA1300" in result.stdout


def test_flights_exits_nonzero_when_every_leg_fails(
    account_store: AccountStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _recording_client(monkeypatch, [RuntimeError("blocked")])

    result = runner.invoke(app, ["flights", "LON", "ABZ", "--month", "2099-11"])

    assert result.exit_code == 1
    assert "Outbound search failed" in result.stdout


def test_flights_json_keeps_full_typed_result(
    account_store: AccountStore, fake_client: None
) -> None:
    result = runner.invoke(
        app,
        ["flights", "LON", "ABZ", "--date", "2099-11-05", "--json"],
    )
    assert result.exit_code == 0
    data = json.loads(result.stdout)
    assert data["journeyType"] == "one-way"
    assert data["mode"] == "date"
    assert data["inbound"] is None
    day = data["outbound"]["days"]["2099-11-05"]
    flight = day["journeys"][0]["flights"][0]
    assert flight["marketing"]["flightNumber"] == "1300"
    assert len(flight["availability"]) == 2
    assert flight["availability"][0]["cabin"] == "J"
    assert flight["availability"][0]["price"]["adult"] == 50000


@pytest.mark.parametrize(
    "arguments",
    [
        ["flights", "LON", "ABZ"],
        ["flights", "LON", "ABZ", "--date", "2099-11-05", "--month", "2099-11"],
        ["flights", "LON", "ABZ", "--month", "2099-11", "--return-date", "2099-11-05"],
        ["flights", "LON", "ABZ", "--date", "2099-11-05", "--cabin", "galley"],
        [
            "flights",
            "LON",
            "ABZ",
            "--date",
            "2099-11-05",
            "--return-date",
            "2099-10-05",
        ],
    ],
)
def test_flights_rejects_invalid_arguments(
    account_store: AccountStore, fake_client: None, arguments: list[str]
) -> None:
    result = runner.invoke(app, arguments)
    assert result.exit_code == 2


def test_flights_requires_ba_account(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    result = runner.invoke(app, ["flights", "LON", "ABZ", "--month", "2099-11"])
    assert result.exit_code == 1
    assert "Not logged in" in result.stdout
    assert "avios login" in result.stdout


# -- error handling ----------------------------------------------------------
def test_no_accounts_prompts_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 1
    assert "Not logged in" in result.stdout


def test_balance_shows_per_account_error_without_crashing(
    account_store: AccountStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A single expired account is captured by the aggregation layer and shown
    # inline (exit 0), so one bad account never hides the others.
    class Client(FakeClient):
        def get_balance(self) -> Balance:
            raise SessionExpired("session expired")

    # aggregation dispatches through Account.client() (backend-aware).
    monkeypatch.setattr("avios.accounts.Account.client", lambda self, *a, **k: Client())
    result = runner.invoke(app, ["balance"])
    assert result.exit_code == 0
    assert "expired" in result.stdout.lower()


def test_whoami_not_authenticated(
    account_store: AccountStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Client-based commands still propagate auth errors to friendly exit codes.
    class Client(FakeClient):
        def get_profile(self) -> Profile:
            raise NotAuthenticated

    monkeypatch.setattr("avios.cli.AviosClient", Client)
    result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 1
    assert "Not logged in" in result.stdout


def test_whoami_session_expired(
    account_store: AccountStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Client(FakeClient):
        def get_profile(self) -> Profile:
            raise SessionExpired

    monkeypatch.setattr("avios.cli.AviosClient", Client)
    result = runner.invoke(app, ["whoami"])
    assert result.exit_code == 2
    assert "Session expired" in result.stdout


# -- login / logout ----------------------------------------------------------
def test_login_from_browser(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    monkeypatch.setattr(
        "avios.cli.import_from_browser",
        lambda prog, browser="chrome", profile=None: ImportResult(2, "Default", True),
    )
    monkeypatch.setattr("avios.accounts.Account.client", lambda self, *a, **k: FakeClient())
    result = runner.invoke(app, ["login", "iberia", "--from-browser"])
    assert result.exit_code == 0
    out = " ".join(result.stdout.split())  # Rich wraps lines
    assert "Logged in to Iberia" in out  # names the programme
    assert "Default" in out  # reports which profile it used
    assert "Balance" in out
    # The account is persisted under its slug.
    assert (tmp_path / "avios" / "accounts" / "iberia.json").exists()


def test_login_from_browser_unauthenticated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    monkeypatch.setattr(
        "avios.cli.import_from_browser",
        lambda prog, browser="chrome", profile=None: ImportResult(20, "Default", False),
    )
    result = runner.invoke(app, ["login", "--from-browser"])
    assert result.exit_code == 2
    normalized = " ".join(result.stdout.split())  # Rich wraps lines
    assert "not a logged-in British Airways session" in normalized
    # Nothing is saved when the imported cookies don't authenticate.
    assert not (tmp_path / "avios" / "accounts").exists()


def test_login_unknown_programme(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    result = runner.invoke(app, ["login", "nope", "--from-browser"])
    assert result.exit_code == 1


def test_login_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))

    def boom(prog: Any, browser: str = "chrome", profile: str | None = None) -> ImportResult:
        raise LoginError("nope")

    monkeypatch.setattr("avios.cli.import_from_browser", boom)
    result = runner.invoke(app, ["login", "--from-browser"])
    assert result.exit_code == 1
    assert "nope" in result.stdout


def test_login_finnair_saves_oauth_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    monkeypatch.setattr(
        "avios.cli.login_finnair_via_browser",
        lambda headless=False: FinnairCredentials("captured-token", "captured-api-key"),
    )
    monkeypatch.setattr("avios.accounts.Account.client", lambda self: FakeClient())

    result = runner.invoke(app, ["login", "finnair"])

    assert result.exit_code == 0
    assert "Finnair Plus" in result.stdout
    account = AccountStore().get("finnair")
    assert account is not None
    assert account.backend == "finnair"
    assert account.token == "captured-token"
    assert account.api_key == "captured-api-key"


def test_logout_single_programme(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    store = AccountStore()
    store.save(Account.from_programme(get_programme("ba"), cookies=[{"name": "s", "value": "1"}]))
    result = runner.invoke(app, ["logout", "ba"])
    assert result.exit_code == 0
    assert "Logged out of British Airways" in result.stdout
    assert store.get("ba") is None


def test_logout_all(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    store = AccountStore()
    store.save(Account.from_programme(get_programme("ba"), cookies=[{"name": "s", "value": "1"}]))
    store.save(
        Account.from_programme(get_programme("iberia"), cookies=[{"name": "s", "value": "2"}])
    )
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0
    assert "2 account(s)" in result.stdout
    assert store.list() == []


def test_logout_when_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    result = runner.invoke(app, ["logout"])
    assert result.exit_code == 0
    assert "No accounts" in result.stdout
