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
