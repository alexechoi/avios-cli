"""Tests for BA reward-flight RSC parsing and requests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from avios.client import AviosClient
from avios.config import Settings
from avios.rewards import (
    Cabin,
    PassengerCounts,
    RewardSearchAccessError,
    RewardSearchProtocolError,
    RewardSearchQuery,
    RewardSearchRangeError,
    UnsupportedProgramme,
    _fetch_reward_rsc_via_browser,
    _select_airport,
    _set_passengers,
    parse_reward_rsc,
)
from avios.session import Session, SessionExpired

FIXTURE = Path(__file__).parent / "fixtures" / "reward_search.rsc"
COOKIE = [{"name": "appSession", "value": "x", "domain": "www.avios.com"}]


def query(**overrides: Any) -> RewardSearchQuery:
    values: dict[str, Any] = {
        "origin": "lon",
        "destination": "abz",
        "month": "2099-11",
    }
    values.update(overrides)
    return RewardSearchQuery(**values)


def test_query_normalises_and_validates() -> None:
    result = query(
        passengers=PassengerCounts(adults=2, children=1),
        cabins=(Cabin.ECONOMY, Cabin.BUSINESS, Cabin.ECONOMY),
    )
    assert result.origin == "LON"
    assert result.destination == "ABZ"
    assert result.cabins == (Cabin.ECONOMY, Cabin.BUSINESS)

    with pytest.raises(ValidationError):
        query(origin="London")
    with pytest.raises(ValidationError):
        query(destination="LON")
    with pytest.raises(ValidationError):
        query(month="2020-01")
    with pytest.raises(ValidationError):
        query(passengers=PassengerCounts(adults=1, infants=2))
    with pytest.raises(ValidationError, match="at most 9"):
        query(passengers=PassengerCounts(adults=9, children=1))


def test_parse_realistic_rsc_without_fixed_record_position() -> None:
    result = parse_reward_rsc(FIXTURE.read_text(), query())
    assert result.origin_city_name == "London"
    assert result.destination_city_name == "Aberdeen"
    assert len(result.days) == 2

    day = result.day("2099-11-05")
    assert day.availability_level == 2
    assert len(day.flights) == 2
    assert day.flights[0].marketing.flight_number == "1300"
    assert day.flights[0].seats_for(Cabin.BUSINESS) == 9
    assert day.flights[0].seats_for(Cabin.ECONOMY) == 0
    assert day.flights[1].peak is True


def test_parser_filters_to_selected_cabins() -> None:
    result = parse_reward_rsc(FIXTURE.read_text(), query(cabins=(Cabin.ECONOMY,)))
    availability = result.day("2099-11-05").flights[0].availability
    assert [item.cabin for item in availability] == ["M"]


def test_parser_distinguishes_expiry_protocol_and_range() -> None:
    with pytest.raises(SessionExpired):
        parse_reward_rsc('0:{"message":"Sign in to continue"}', query())
    with pytest.raises(RewardSearchProtocolError):
        parse_reward_rsc('0:{"message":"unexpected"}', query())
    with pytest.raises(RewardSearchRangeError):
        parse_reward_rsc(FIXTURE.read_text(), query(month="2099-12"))


def test_client_sends_captured_rsc_contract(settings: Settings) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            text=FIXTURE.read_text(),
            headers={"content-type": "text/x-component"},
        )

    session = Session(
        settings,
        opco="BAEC",
        base_url="https://www.avios.com",
        cookies=COOKIE,
        transport=httpx.MockTransport(handler),
    )
    result = AviosClient(session).search_reward_calendar(query())

    assert result.month == "2099-11"
    request = captured[0]
    assert request.url.path == "/en-GB/spend-avios/search-reward-flights/results"
    params = request.url.params.multi_items()
    assert ("originAirport", "LON") in params
    assert ("destinationAirport", "ABZ") in params
    assert ("month", "11") in params
    assert [value for key, value in params if key == "cabin"] == [
        "Economy",
        "Premium Economy",
        "Business",
        "First",
    ]
    assert request.headers["rsc"] == "1"
    assert request.headers["next-url"] == "/en-GB/BA/spend-avios/search-reward-flights"
    assert "next-router-state-tree" in request.headers
    assert "_rsc" in request.url.params


def test_non_ba_session_is_rejected_before_network(settings: Settings) -> None:
    session = Session(settings, opco="IBP", cookies=COOKIE)
    with pytest.raises(UnsupportedProgramme):
        AviosClient(session).search_reward_calendar(query())


def test_flight_search_403_has_actionable_login_error(settings: Settings) -> None:
    session = Session(
        settings,
        opco="BAEC",
        cookies=COOKIE,
        transport=httpx.MockTransport(lambda request: httpx.Response(403, request=request)),
    )
    with pytest.raises(RewardSearchAccessError, match="avios login ba"):
        AviosClient(session).search_reward_calendar(query())


class _BrowserResponse:
    status = 200


class _BrowserPage:
    url = "https://www.avios.com/en-GB/spend-avios/search-reward-flights"

    def __init__(self) -> None:
        self.fetch_args: dict[str, Any] = {}

    def goto(self, url: str, **kwargs: object) -> _BrowserResponse:
        return _BrowserResponse()

    def wait_for_timeout(self, milliseconds: int) -> None:
        pass

    def evaluate(self, expression: str, arg: dict[str, Any]) -> dict[str, Any]:
        self.fetch_args = arg
        return {
            "status": 200,
            "url": arg["url"],
            "body": FIXTURE.read_text(),
        }


class _BrowserContext:
    def __init__(self) -> None:
        self.page = _BrowserPage()
        self.seeded_cookies: list[dict[str, Any]] = []
        self.closed = False

    def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.seeded_cookies = cookies

    def cookies(self) -> list[dict[str, Any]]:
        return []

    def new_page(self) -> _BrowserPage:
        return self.page

    def close(self) -> None:
        self.closed = True


class _BrowserChromium:
    def __init__(self, ctx: _BrowserContext) -> None:
        self.ctx = ctx

    def launch_persistent_context(self, path: str, **kwargs: object) -> _BrowserContext:
        return self.ctx


class _BrowserPlaywright:
    def __init__(self, ctx: _BrowserContext) -> None:
        self.chromium = _BrowserChromium(ctx)


class _BrowserFactory:
    def __init__(self, ctx: _BrowserContext) -> None:
        self.playwright = _BrowserPlaywright(ctx)

    def __call__(self) -> _BrowserFactory:
        return self

    def __enter__(self) -> _BrowserPlaywright:
        return self.playwright

    def __exit__(self, *args: object) -> bool:
        return False


def test_browser_fetch_seeds_cookies_and_returns_rsc(settings: Settings) -> None:
    ctx = _BrowserContext()
    captured: list[tuple[str, RewardSearchQuery]] = []
    session = Session(
        settings,
        opco="BAEC",
        base_url="https://www.avios.com",
        cookies=COOKIE,
    )

    def driver(page: object, base_url: str, reward_query: RewardSearchQuery) -> str:
        captured.append((base_url, reward_query))
        return FIXTURE.read_text()

    payload = _fetch_reward_rsc_via_browser(
        session,
        query(),
        playwright_factory=_BrowserFactory(ctx),
        search_driver=driver,
    )

    assert payload == FIXTURE.read_text()
    assert ctx.seeded_cookies == [
        {"name": "appSession", "value": "x", "domain": "www.avios.com", "path": "/"}
    ]
    assert captured[0][0] == "https://www.avios.com"
    assert captured[0][1].origin == "LON"
    assert ctx.closed


def test_browser_search_retries_one_nextjs_shell(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads = iter(['0:{"message":"router shell"}', FIXTURE.read_text()])
    calls = 0

    def fetch(session: Session, reward_query: RewardSearchQuery) -> str:
        nonlocal calls
        calls += 1
        return next(payloads)

    monkeypatch.setattr("avios.rewards._fetch_reward_rsc_via_browser", fetch)
    session = Session(settings, opco="BAEC", base_url="https://www.avios.com", cookies=COOKIE)

    result = AviosClient(session).search_reward_calendar(query())

    assert result.month == "2099-11"
    assert calls == 2


class _ClickTarget:
    def __init__(self) -> None:
        self.clicks = 0

    def click(self, **kwargs: object) -> None:
        self.clicks += 1


class _AirportInput:
    def __init__(self, value: str) -> None:
        self.value = value
        self.fills: list[str] = []

    def input_value(self) -> str:
        return self.value

    def fill(self, value: str) -> None:
        self.fills.append(value)
        self.value = value


class _FormPage:
    def __init__(self, airport_value: str) -> None:
        self.airport = _AirportInput(airport_value)
        self.targets: dict[str, _ClickTarget] = {}

    def locator(self, selector: str) -> _AirportInput:
        return self.airport

    def get_by_test_id(self, test_id: str) -> _ClickTarget:
        return self.targets.setdefault(test_id, _ClickTarget())


def test_airport_selection_preserves_matching_default() -> None:
    page = _FormPage("London All Airports (LON)")

    _select_airport(page, "origin", "LON")

    assert page.airport.fills == []
    assert page.targets == {}


def test_airport_and_passenger_controls_drive_site_test_ids() -> None:
    page = _FormPage("")

    _select_airport(page, "destination", "ABZ")
    _set_passengers(page, PassengerCounts(adults=2, children=1))

    assert page.airport.fills == ["ABZ"]
    assert page.targets["destination-ABZ-airport-option"].clicks == 1
    assert page.targets["passenger number select"].clicks == 2
    assert page.targets["Adults-plus"].clicks == 1
    assert page.targets["Children-plus"].clicks == 1
