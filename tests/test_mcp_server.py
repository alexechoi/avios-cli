"""Tests for the MCP server.

Tools are called through the server's own dispatch (`mcp.call_tool`) so the
registered schemas, argument coercion and structured output are exercised, not
just the Python functions underneath.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from avios.accounts import Account, AccountStore
from avios.models import Balance, Profile, Transaction
from avios.programmes import get_programme
from avios.rewards import RewardCalendar, RewardLeg, RewardSearchBlockedError

mcp_server = pytest.importorskip("avios.mcp_server")
ToolError = pytest.importorskip("mcp.server.mcpserver.exceptions").ToolError


@pytest.fixture(autouse=True)
def _reset_cooldown() -> None:
    """The reward cooldown is process-wide; tests must not inherit each other's."""
    mcp_server._cooldown = mcp_server._Cooldown(0)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AccountStore:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "avios"))
    store = AccountStore()
    store.save(Account.from_programme(get_programme("ba"), cookies=[{"name": "s", "value": "1"}]))
    store.save(
        Account.from_programme(get_programme("iberia"), cookies=[{"name": "s", "value": "2"}])
    )
    return store


@pytest.fixture
def empty_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "empty"))


async def call(name: str, **arguments: Any) -> Any:
    """Invoke a tool the way a client would and return its structured content."""
    result = await mcp_server.mcp.call_tool(name, arguments)
    assert not result.is_error, result.content
    return result.structured_content


def _calendar(leg: RewardLeg, *, seats: int = 4, price: int | None = 50000) -> RewardCalendar:
    query = leg.query
    day = leg.departure_date or f"{query.month}-05"
    return RewardCalendar.model_validate(
        {
            "origin": query.origin,
            "destination": query.destination,
            "month": query.month,
            "originCityName": "London",
            "destinationCityName": "Hong Kong",
            "priced": price is not None,
            "days": {
                day: {
                    "date": day,
                    "availabilityLevel": 2,
                    "journeys": [
                        {
                            "journeyType": "outbound",
                            "direct": True,
                            "flights": [
                                {
                                    "departureAirport": "LHR",
                                    "arrivalAirport": query.destination,
                                    "departureTime": f"{day}T18:55:00",
                                    "arrivalTime": f"{day}T15:50:00",
                                    "duration": 775,
                                    "direct": True,
                                    "peak": False,
                                    "marketing": {"carrier": "BA", "flightNumber": "0031"},
                                    "availability": [
                                        {
                                            "cabin": "J",
                                            "rbd": "U",
                                            "seatsAvailable": seats,
                                            "price": (
                                                {"pricingRowKey": "k", "adult": price}
                                                if price is not None
                                                else None
                                            ),
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            },
        }
    )


class FakeAviosClient:
    """Canned AviosClient covering only what the MCP tools call."""

    legs_seen: list[list[RewardLeg]] = []
    raise_blocked = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_balance(self) -> Balance:
        return Balance(balance=1000, individual=1000, household=1500)

    def get_transactions(self, limit: int = 50) -> list[Transaction]:
        return [
            Transaction.model_validate(
                {
                    "dateProcessed": "2026-08-01T09:00:00.000Z",
                    "description": "BA FLIGHT",
                    "type": {"value": "Collection", "id": "DV_COL"},
                    "partner": {"value": "British Airways", "id": "BA"},
                    "amount": 250,
                }
            )
        ][:limit]

    def get_pending_transactions(self) -> list[Transaction]:
        return []

    def get_profile(self) -> Profile:
        return Profile.model_validate(
            {
                "idToken": "SECRET.JWT.VALUE",
                "tokenContent": {
                    "name": "Alex Choi",
                    "email": "alex@example.com",
                    "https://avios.com/customer_tier_name": "Gold",
                    "https://avios.com/membership_id": "BA123",
                },
            }
        )

    def search_reward_legs(self, legs: Sequence[RewardLeg]) -> list[RewardCalendar | Exception]:
        type(self).legs_seen.append(list(legs))
        if type(self).raise_blocked:
            raise RewardSearchBlockedError("Akamai blocked this request from your IP address.")
        return [_calendar(leg) for leg in legs]


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch) -> type[FakeAviosClient]:
    FakeAviosClient.legs_seen = []
    FakeAviosClient.raise_blocked = False
    monkeypatch.setattr("avios.mcp_server.AviosClient", FakeAviosClient)
    monkeypatch.setattr("avios.accounts.Account.client", lambda self, *a, **k: FakeAviosClient())
    # isinstance(client, AviosClient) guards whoami; keep that check satisfied.
    monkeypatch.setattr("avios.mcp_server.AviosClient", FakeAviosClient)
    return FakeAviosClient


# -- registration ------------------------------------------------------------
async def test_every_tool_is_registered_read_only_with_structured_output() -> None:
    tools = {tool.name: tool for tool in await mcp_server.mcp.list_tools()}

    assert set(tools) == {
        "list_accounts",
        "get_balance",
        "get_transactions",
        "get_pending_transactions",
        "whoami",
        "search_reward_flights",
    }
    for name, tool in tools.items():
        assert tool.annotations is not None, name
        assert tool.annotations.read_only_hint is True, name
        assert tool.annotations.destructive_hint is False, name
        assert tool.output_schema, name
        assert tool.description, name


async def test_reward_search_is_marked_open_world_and_warns_about_blocking() -> None:
    tool = next(
        tool for tool in await mcp_server.mcp.list_tools() if tool.name == "search_reward_flights"
    )
    assert tool.annotations is not None
    assert tool.annotations.open_world_hint is True
    # An agent has to be told not to loop or retry; that is the whole safety story.
    assert "rate-limited" in (tool.description or "").lower()
    assert "ip address" in (tool.description or "").lower()
    assert "one call" in (tool.description or "").lower()


# -- accounts ----------------------------------------------------------------
async def test_list_accounts_never_returns_cookies(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    result = await call("list_accounts")

    assert [item["slug"] for item in result["accounts"]] == ["ba", "iberia"]
    for item in result["accounts"]:
        assert set(item) == {"slug", "name", "opco", "backend"}
    assert "cookie" not in str(result).lower()


async def test_missing_login_tells_the_user_which_command_to_run(empty_store: None) -> None:
    with pytest.raises(ToolError, match="avios login"):
        await call("list_accounts")


async def test_unknown_account_lists_the_ones_that_exist(store: AccountStore) -> None:
    with pytest.raises(ToolError, match="Logged-in accounts: ba, iberia"):
        await call("get_balance", account="aerlingus")


# -- balances and transactions -----------------------------------------------
async def test_balance_sums_across_accounts(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    result = await call("get_balance")

    assert len(result["balances"]) == 2
    assert result["combined_total"] == 2000
    assert result["currency"] == "Avios"


async def test_balance_can_focus_one_account(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    result = await call("get_balance", account="ba")

    assert [item["account"] for item in result["balances"]] == ["ba"]
    assert result["combined_total"] == 1000


async def test_transactions_are_tagged_and_capped(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    result = await call("get_transactions", limit=1)

    assert result["count"] == 1
    entry = result["transactions"][0]
    assert entry["account"] in {"ba", "iberia"}
    assert entry["description"] == "BA FLIGHT"
    assert entry["type"] == "Collection"
    assert entry["partner"] == "British Airways"
    assert entry["amount"] == 250


async def test_transaction_limit_is_bounded_server_side(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    result = await call("get_transactions", limit=10_000)
    assert result["count"] <= mcp_server.MAX_TRANSACTIONS


async def test_pending_returns_an_empty_list_not_an_error(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    result = await call("get_pending_transactions")
    assert result == {"transactions": [], "count": 0, "truncated": False}


# -- profile -----------------------------------------------------------------
async def test_whoami_returns_claims_but_never_the_id_token(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    result = await call("whoami", account="ba")

    assert result == {
        "account": "ba",
        "name": "Alex Choi",
        "tier": "Gold",
        "membership_id": "BA123",
        "email": "alex@example.com",
    }
    assert "SECRET.JWT.VALUE" not in str(result)


async def test_whoami_on_a_non_avios_programme_points_at_a_usable_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Finnair has its own loyalty API and no equivalent profile endpoint."""
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "finnair-first"))
    AccountStore().save(
        Account.from_programme(get_programme("finnair"), cookies=[{"name": "s", "value": "1"}])
    )

    with pytest.raises(ToolError, match="list_accounts"):
        await call("whoami", account="finnair")


# -- reward search -----------------------------------------------------------
async def test_reward_search_returns_seats_and_prices(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    result = await call(
        "search_reward_flights",
        origin="LON",
        destination="HKG",
        departure_date="2027-01-05",
        cabins=["business"],
        adults=2,
    )

    assert result["cabins"] == ["Business"]
    assert result["passengers"]["adults"] == 2
    leg = result["legs"][0]
    assert leg["direction"] == "outbound"
    assert leg["origin_city"] == "London"
    assert leg["priced"] is True
    cabin = leg["days"][0]["flights"][0]["cabins"][0]
    assert cabin["cabin"] == "Business"
    assert cabin["seats"] == 4
    assert cabin["avios_per_adult"] == 50000
    # Party total, so an agent does not have to do the arithmetic itself.
    assert cabin["avios_total"] == 100000


async def test_return_trip_is_one_batched_search(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    """The whole point: never a loop of one-leg searches."""
    result = await call(
        "search_reward_flights",
        origin="LON",
        destination="HKG",
        departure_date="2027-01-05",
        return_date="2027-01-19",
    )

    assert len(fake_client.legs_seen) == 1
    legs = fake_client.legs_seen[0]
    assert [(leg.query.origin, leg.query.destination) for leg in legs] == [
        ("LON", "HKG"),
        ("HKG", "LON"),
    ]
    assert [leg.departure_date for leg in legs] == ["2027-01-05", "2027-01-19"]
    assert [item["direction"] for item in result["legs"]] == ["outbound", "inbound"]


async def test_month_search_does_not_request_pricing(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    result = await call("search_reward_flights", origin="LON", destination="HKG", month="2027-01")

    assert [leg.departure_date for leg in fake_client.legs_seen[0]] == [None]
    assert any("departure_date for Avios prices" in note for note in result["notes"])


async def test_cooldown_refuses_a_rapid_second_search(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    mcp_server._cooldown = mcp_server._Cooldown(300)

    await call("search_reward_flights", origin="LON", destination="HKG", month="2027-01")
    with pytest.raises(ToolError, match="rate-limited"):
        await call("search_reward_flights", origin="LON", destination="ABZ", month="2027-01")

    assert len(fake_client.legs_seen) == 1


async def test_a_block_is_reported_as_terminal_and_not_retryable(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    fake_client.raise_blocked = True

    with pytest.raises(ToolError, match="Do not retry"):
        await call("search_reward_flights", origin="LON", destination="HKG", month="2027-01")


async def test_a_failed_leg_is_reported_without_losing_the_other(
    store: AccountStore, monkeypatch: pytest.MonkeyPatch, fake_client: type[FakeAviosClient]
) -> None:
    def half_broken(self: Any, legs: Sequence[RewardLeg]) -> list[RewardCalendar | Exception]:
        return [_calendar(legs[0]), RuntimeError("inbound exploded")]

    monkeypatch.setattr(FakeAviosClient, "search_reward_legs", half_broken)

    result = await call(
        "search_reward_flights",
        origin="LON",
        destination="HKG",
        departure_date="2027-01-05",
        return_date="2027-01-19",
    )

    assert result["legs"][0]["error"] is None
    assert result["legs"][1]["error"] == "inbound exploded"
    assert result["legs"][0]["days"]
    assert any("A leg failed" in note for note in result["notes"])


async def test_companion_voucher_seats_are_flagged_with_a_note(
    store: AccountStore, monkeypatch: pytest.MonkeyPatch, fake_client: type[FakeAviosClient]
) -> None:
    def voucher_only(self: Any, legs: Sequence[RewardLeg]) -> list[RewardCalendar | Exception]:
        calendar = _calendar(legs[0])
        flight = calendar.day("2027-01-05").flights[0]
        flight.availability[0].rbd = "I"
        return [calendar]

    monkeypatch.setattr(FakeAviosClient, "search_reward_legs", voucher_only)

    result = await call(
        "search_reward_flights",
        origin="LON",
        destination="HKG",
        departure_date="2027-01-05",
        cabins=["business"],
    )

    cabin = result["legs"][0]["days"][0]["flights"][0]["cabins"][0]
    assert cabin["companion_voucher_only"] is True
    assert any("companion-voucher" in note for note in result["notes"])


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"origin": "LON", "destination": "HKG"}, "exactly one"),
        (
            {
                "origin": "LON",
                "destination": "HKG",
                "month": "2027-01",
                "departure_date": "2027-01-05",
            },
            "exactly one",
        ),
        (
            {
                "origin": "LON",
                "destination": "HKG",
                "month": "2027-01",
                "return_date": "2027-01-05",
            },
            "return_date needs departure_date",
        ),
        (
            {
                "origin": "LON",
                "destination": "HKG",
                "departure_date": "2027-01-05",
                "return_month": "2027-02",
            },
            "return_month needs month",
        ),
        (
            {
                "origin": "LON",
                "destination": "HKG",
                "departure_date": "2027-01-19",
                "return_date": "2027-01-05",
            },
            "before departure_date",
        ),
        (
            {"origin": "LON", "destination": "HKG", "month": "2027-02", "return_month": "2027-01"},
            "before month",
        ),
        (
            {"origin": "LON", "destination": "HKG", "departure_date": "2020-01-05"},
            "in the past",
        ),
        (
            {"origin": "London", "destination": "HKG", "month": "2027-01"},
            "three-letter",
        ),
        ({"origin": "LON", "destination": "LON", "month": "2027-01"}, "must differ"),
        (
            {"origin": "LON", "destination": "HKG", "month": "2027-01", "cabins": ["galley"]},
            "Unknown cabin",
        ),
        (
            {"origin": "LON", "destination": "HKG", "month": "not-a-month"},
            "YYYY-MM",
        ),
    ],
)
async def test_reward_search_rejects_bad_arguments(
    store: AccountStore,
    fake_client: type[FakeAviosClient],
    arguments: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ToolError, match=message):
        await call("search_reward_flights", **arguments)
    assert fake_client.legs_seen == []  # never reaches the network


async def test_reward_search_needs_a_ba_account(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_client: type[FakeAviosClient]
) -> None:
    monkeypatch.setenv("AVIOS_CONFIG_DIR", str(tmp_path / "iberia-only"))
    AccountStore().save(
        Account.from_programme(get_programme("iberia"), cookies=[{"name": "s", "value": "1"}])
    )

    with pytest.raises(ToolError, match="avios login ba"):
        await call("search_reward_flights", origin="LON", destination="HKG", month="2027-01")


# -- entry point -------------------------------------------------------------
def test_entry_point_explains_a_missing_extra_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`avios-mcp` is launched by a client, which shows the user very little."""
    from avios import mcp_entry

    monkeypatch.setattr(mcp_entry, "find_spec", lambda name: None)

    with pytest.raises(SystemExit) as excinfo:
        mcp_entry.main()

    assert excinfo.value.code == 1
    message = capsys.readouterr().err
    assert "avios-cli[mcp]" in message
    assert "Traceback" not in message


def test_entry_point_serves_over_stdio_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    from avios import mcp_entry

    transports: list[str] = []
    monkeypatch.setattr(
        mcp_server.mcp, "run", lambda transport="stdio": transports.append(transport)
    )

    mcp_entry.main()

    assert transports == ["stdio"]


# -- stdio transport ---------------------------------------------------------
def test_stdio_keeps_stdout_pure_json_rpc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Anything non-protocol on stdout corrupts the stream and breaks the client.

    Worth guarding: the library logs a line per HTTP request, and it only takes one
    handler defaulting to stdout to silently break every client.
    """
    import json
    import subprocess
    import sys
    import threading

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    env = {**os.environ, "AVIOS_CONFIG_DIR": str(tmp_path / "avios")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "avios.mcp_server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    # Read replies while stdin stays open: closing it can shut the server down
    # before it has answered, which would make this test flaky rather than strict.
    watchdog = threading.Timer(60.0, proc.kill)
    watchdog.start()
    lines: list[str] = []
    try:
        assert proc.stdin is not None and proc.stdout is not None
        for item in requests:
            proc.stdin.write(json.dumps(item) + "\n")
        proc.stdin.flush()
        while True:
            line = proc.stdout.readline()
            if not line:  # server exited or the watchdog fired
                break
            if line.strip():
                lines.append(line)
                if json.loads(line).get("id") == 2:
                    break
    finally:
        watchdog.cancel()
        proc.kill()
        proc.wait(timeout=30)

    assert lines, "server produced no protocol output"
    for line in lines:
        message = json.loads(line)  # raises if anything else reached stdout
        assert message["jsonrpc"] == "2.0"

    listed = next((json.loads(line) for line in lines if json.loads(line).get("id") == 2), None)
    assert listed is not None, f"no tools/list reply in {lines}"
    assert {tool["name"] for tool in listed["result"]["tools"]} == {
        "list_accounts",
        "get_balance",
        "get_transactions",
        "get_pending_transactions",
        "whoami",
        "search_reward_flights",
    }


async def test_cabin_names_are_forgiving(
    store: AccountStore, fake_client: type[FakeAviosClient]
) -> None:
    result = await call(
        "search_reward_flights",
        origin="LON",
        destination="HKG",
        month="2027-01",
        cabins=["Club", "premium economy", "ECONOMY"],
    )
    assert result["cabins"] == ["Business", "Premium Economy", "Economy"]
