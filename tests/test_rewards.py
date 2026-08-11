"""Tests for BA reward-flight parsing and the captured request contract.

The RSC fixture and the action payloads below are trimmed copies of a real
www.avios.com session (August 2026), so a protocol change breaks these tests
rather than silently returning empty results.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from pydantic import ValidationError

from avios.client import AviosClient
from avios.config import Settings
from avios.rewards import (
    PRICING_ACTION,
    SINGLE_JOURNEY_ACTION,
    Cabin,
    PassengerCounts,
    RewardLeg,
    RewardSearchAccessError,
    RewardSearchBlockedError,
    RewardSearchProtocolError,
    RewardSearchQuery,
    RewardSearchRangeError,
    UnsupportedProgramme,
    attach_prices,
    build_id,
    chunk_paths,
    parse_action_tokens,
    parse_pricing_action,
    parse_reward_rsc,
    parse_server_action_ids,
    parse_single_journey_action,
    pricing_rows,
    reward_search_session,
)
from avios.session import Session, SessionExpired

FIXTURE = Path(__file__).parent / "fixtures" / "reward_search.rsc"
COOKIE = [{"name": "appSession", "value": "x", "domain": "www.avios.com"}]

#: Real build-scoped server-action ids and the shape of the chunk they live in.
JOURNEY_ACTION_ID = "408c4c27f522c7a964437f2e84693a74e6cce970c8"
PRICING_ACTION_ID = "408146192e55725065187d8492e1e8aa396de93a94"
ACTION_CHUNK = (
    "var t=e.i(95187);let r=(0,t.createServerReference)("
    f'"{JOURNEY_ACTION_ID}",t.callServer,void 0,t.findSourceMapURL,'
    '"getFlightResultsForSingleJourneyAction");'
    f'let q=(0,t.createServerReference)("{PRICING_ACTION_ID}",t.callServer,void 0,'
    't.findSourceMapURL,"fetchPricingAction");'
)

#: A real ``fetchPricingAction`` response, retargeted at the fixture's dates.
PRICING_RESPONSE = (
    '0:{"a":"$@1","f":"","q":"","i":false,"b":"gevC20sT6x7YAmXzn_0Og"}\n'
    "1:"
    + json.dumps(
        [
            {
                "flightNumber": "0031",
                "adult": 121000,
                "child": 0,
                "youngAdult": 0,
                "infant": 0,
                "pricingRowKey": "BA|0031|LHR|HKG|2099-11-05T19:20:00|19:20|J|U",
                "carrier": "BA",
                "departureDate": "2099-11-05T19:20:00",
                "departureTime": "19:20",
                "cabinCode": "J",
                "rbd": "U",
            },
            {
                "flightNumber": "0031",
                "adult": 44000,
                "child": 0,
                "youngAdult": 0,
                "infant": 0,
                "pricingRowKey": "BA|0031|LHR|HKG|2099-11-05T19:20:00|19:20|M|X",
                "carrier": "BA",
                "departureDate": "2099-11-05T19:20:00",
                "departureTime": "19:20",
                "cabinCode": "M",
                "rbd": "X",
            },
        ]
    )
    + "\n"
)

#: A real ``getFlightResultsForSingleJourneyAction`` response, one date, more seats
#: in Business than the calendar advertised.
SINGLE_JOURNEY_RESPONSE = (
    '0:{"a":"$@1","f":"","q":"","i":false,"b":"gevC20sT6x7YAmXzn_0Og"}\n'
    "1:"
    + json.dumps(
        {
            "departureFlights": {
                "2099-11-05": {
                    "availabilityLevel": 2,
                    "journeys": [
                        {
                            "journeyType": "outbound",
                            "totalDuration": 785,
                            "direct": True,
                            "flights": [
                                {
                                    "arrivalAirport": "HKG",
                                    "arrivalTime": "2099-11-06T15:25:00",
                                    "departureAirport": "LHR",
                                    "departureTime": "2099-11-05T19:20:00",
                                    "duration": 785,
                                    "direct": True,
                                    "peak": True,
                                    "marketing": {"flightNumber": "0031", "carrier": "BA"},
                                    "operating": {"flightNumber": "0031", "carrier": "BA"},
                                    "availability": [
                                        {
                                            "cabin": "J",
                                            "rbd": "U",
                                            "state": "7",
                                            "seatsAvailable": 7,
                                            "fareBasisCode": None,
                                        },
                                        {
                                            "cabin": "M",
                                            "rbd": "X",
                                            "state": "9",
                                            "seatsAvailable": 9,
                                            "fareBasisCode": None,
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                }
            },
            "returnFlights": {},
        }
    )
    + "\n"
)


def query(**overrides: Any) -> RewardSearchQuery:
    values: dict[str, Any] = {
        "origin": "lon",
        "destination": "hkg",
        "month": "2099-11",
    }
    values.update(overrides)
    return RewardSearchQuery(**values)


def payload() -> str:
    return FIXTURE.read_text()


# -- query validation --------------------------------------------------------
def test_query_normalises_and_validates() -> None:
    result = query(
        passengers=PassengerCounts(adults=2, children=1),
        cabins=(Cabin.ECONOMY, Cabin.BUSINESS, Cabin.ECONOMY),
    )
    assert result.origin == "LON"
    assert result.destination == "HKG"
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


def test_business_cabin_covers_both_booking_codes() -> None:
    """BA's availability rows use ``J``; the site's map also lists ``C``."""
    assert Cabin.BUSINESS.codes == ("C", "J")
    assert query().cabin_codes == {"M", "Y", "W", "C", "J", "F"}


# -- availability RSC parsing ------------------------------------------------
def test_parses_journey_nested_availability() -> None:
    result = parse_reward_rsc(payload(), query())

    assert result.origin_city_name == "London"
    assert result.destination_city_name == "Hong Kong"
    assert result.months_available == ["2099-11", "2099-12"]
    assert sorted(result.days) == ["2099-11-05", "2099-11-06"]

    day = result.day("2099-11-05")
    assert day.availability_level == 2
    # Flights arrive one level deeper than they used to: day -> journeys -> flights.
    assert len(day.journeys) == 2
    assert [flight.marketing.flight_number for flight in day.flights] == ["0031", "0027"]

    flight = day.flights[0]
    assert flight.departure_airport == "LHR"
    assert flight.arrival_airport == "HKG"
    assert flight.seats_for(Cabin.BUSINESS) == 3
    assert flight.seats_for(Cabin.PREMIUM_ECONOMY) == 2
    assert flight.seats_for(Cabin.ECONOMY) == 9
    assert flight.seats_for(Cabin.FIRST) == 0
    assert flight.peak is True
    assert flight.duration_text() == "13h 5m"


def test_sold_out_and_out_of_window_days_are_distinguishable() -> None:
    result = parse_reward_rsc(payload(), query())

    sold_out = result.day("2099-11-06")
    assert sold_out.flights and not sold_out.available_flights
    assert sold_out.out_of_range is False

    # December's only day is level 3 with no journeys: past BA's booking horizon.
    december = parse_reward_rsc(payload(), query(month="2099-12"))
    assert december.day("2099-12-01").out_of_range is True


def test_companion_voucher_business_seats_are_flagged() -> None:
    """``rbd: I`` Business stock only sells against a voucher, as the site shows."""
    flight = parse_reward_rsc(payload(), query()).day("2099-11-05").flights[1]

    assert flight.seats_for(Cabin.BUSINESS) == 2
    assert flight.voucher_only_for(Cabin.BUSINESS) is True
    assert flight.voucher_only_for(Cabin.ECONOMY) is False


def test_parser_filters_to_selected_cabins() -> None:
    result = parse_reward_rsc(payload(), query(cabins=(Cabin.ECONOMY,)))
    availability = result.day("2099-11-05").flights[0].availability
    assert [item.cabin for item in availability] == ["M"]


def test_parser_distinguishes_expiry_protocol_and_range() -> None:
    with pytest.raises(SessionExpired):
        parse_reward_rsc('0:{"message":"Sign in to continue"}', query())
    with pytest.raises(RewardSearchProtocolError):
        parse_reward_rsc('0:{"message":"unexpected"}', query())
    with pytest.raises(RewardSearchRangeError, match="2099-11.*2099-12"):
        parse_reward_rsc(payload(), query(month="2100-01"))


def test_rsc_carries_action_tokens_build_id_and_chunk_list() -> None:
    text = payload()

    tokens = parse_action_tokens(text)
    assert set(tokens) == {PRICING_ACTION, SINGLE_JOURNEY_ACTION}
    assert all(value and not value.startswith("$") for value in tokens.values())

    assert build_id(text) == "gevC20sT6x7YAmXzn_0Og"
    chunks = chunk_paths(text)
    # Locale-prefixed, or avios.com answers with a 307 to the same file.
    assert all(path.startswith("/en-GB/spend-avios/_next/static/chunks/") for path in chunks)
    assert "/en-GB/spend-avios/_next/static/chunks/208r2s4tfys9r.js" in chunks


def test_unresolved_rsc_back_references_are_not_reported_as_city_names() -> None:
    """``availableCabinClasses`` and friends arrive as ``$4:props:…`` pointers."""
    text = payload().replace('"originCityName":"London"', '"originCityName":"$4:props:origin"')
    assert parse_reward_rsc(text, query()).origin_city_name is None


# -- server actions ----------------------------------------------------------
def test_pricing_rows_match_the_captured_request_body() -> None:
    result = parse_reward_rsc(payload(), query())
    reward_query = query()
    rows = pricing_rows(result.day("2099-11-05").flights, reward_query)

    # Field names, order and formatting all come from the site's own
    # derivePricingRowsFromFlights, which the server validates against.
    assert list(rows[0]) == [
        "origin",
        "departureDate",
        "departureTime",
        "duration",
        "destination",
        "arrivalTime",
        "cabinCode",
        "cabinName",
        "seats",
        "carrier",
        "flightNumber",
        "rbd",
    ]
    assert rows[0] == {
        "origin": "LHR",
        "departureDate": "2099-11-05T19:20:00",
        "departureTime": "19:20",
        "duration": "13h 5m",
        "destination": "HKG",
        "arrivalTime": "15:25",
        "cabinCode": "J",
        "cabinName": "Business",
        "seats": 3,
        "carrier": "BA",
        "flightNumber": "0031",
        "rbd": "U",
    }
    # Sold-out cabins are never priced.
    assert all(row["seats"] > 0 for row in rows)
    assert reward_query.passengers.pricing_payload() == {
        "adults": "1",
        "youngAdults": "0",
        "children": "0",
        "infants": "0",
    }


def test_prices_attach_to_the_availability_they_belong_to() -> None:
    result = parse_reward_rsc(payload(), query())
    flights = result.day("2099-11-05").flights

    prices = parse_pricing_action(PRICING_RESPONSE)
    assert [price.adult for price in prices] == [121000, 44000]
    attach_prices(flights, prices)

    assert flights[0].price_for(Cabin.BUSINESS) is not None
    assert flights[0].price_for(Cabin.BUSINESS).adult == 121000  # type: ignore[union-attr]
    assert flights[0].price_for(Cabin.ECONOMY).adult == 44000  # type: ignore[union-attr]
    # Premium Economy had seats but no price row, and the other flight none at all.
    assert flights[0].price_for(Cabin.PREMIUM_ECONOMY) is None
    assert flights[1].price_for(Cabin.ECONOMY) is None


def test_price_totals_scale_with_the_party() -> None:
    price = parse_pricing_action(PRICING_RESPONSE)[0]
    assert price.total_for(PassengerCounts(adults=1)) == 121000
    assert price.total_for(PassengerCounts(adults=2)) == 242000


def test_single_journey_action_refreshes_one_date() -> None:
    days = parse_single_journey_action(SINGLE_JOURNEY_RESPONSE, query())
    assert list(days) == ["2099-11-05"]
    assert days["2099-11-05"].flights[0].seats_for(Cabin.BUSINESS) == 7


def test_action_responses_surface_protocol_and_expiry_errors() -> None:
    with pytest.raises(RewardSearchProtocolError):
        parse_single_journey_action('0:{"a":"$@1"}\n1:{"nope":true}\n', query())
    with pytest.raises(RewardSearchProtocolError):
        parse_pricing_action('0:{"a":"$@1"}\n1:{"nope":true}\n')
    with pytest.raises(SessionExpired):
        parse_pricing_action('0:{"a":"$@1"}\n1:{"error":"Please sign in"}\n')


def test_server_action_ids_come_from_the_client_chunk() -> None:
    assert parse_server_action_ids(ACTION_CHUNK) == {
        SINGLE_JOURNEY_ACTION: JOURNEY_ACTION_ID,
        PRICING_ACTION: PRICING_ACTION_ID,
    }
    assert parse_server_action_ids("nothing here") == {}


# -- request contract --------------------------------------------------------
def _session(settings: Settings, handler: Any, opco: str = "BAEC") -> Session:
    return Session(
        settings,
        opco=opco,
        base_url="https://www.avios.com",
        cookies=COOKIE,
        transport=httpx.MockTransport(handler),
    )


def test_availability_request_matches_the_captured_contract(settings: Settings) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text=payload(), headers={"content-type": "text/x-component"})

    result = AviosClient(_session(settings, handler)).search_reward_calendar(query())

    assert result.month == "2099-11"
    request = captured[0]
    assert request.url.path == "/en-GB/spend-avios/search-reward-flights/results"

    params = request.url.params.multi_items()
    assert [(key, value) for key, value in params if key != "_rsc"] == [
        ("originAirport", "LON"),
        ("destinationAirport", "HKG"),
        # The site sends the month number, not YYYY-MM.
        ("month", "11"),
        ("adults", "1"),
        ("youngAdults", "0"),
        ("children", "0"),
        ("infants", "0"),
        ("journeyType", "One Way"),
        ("cabin", "Economy"),
        ("cabin", "Premium Economy"),
        ("cabin", "Business"),
        ("cabin", "First"),
    ]
    assert len(request.url.params["_rsc"]) == 16

    assert request.headers["accept"] == "*/*"
    assert request.headers["rsc"] == "1"
    assert request.headers["next-url"] == "/en-GB/BA/spend-avios/search-reward-flights"
    assert request.headers["referer"] == (
        "https://www.avios.com/en-GB/spend-avios/search-reward-flights"
    )
    assert "next-action" not in request.headers
    tree = json.loads(unquote(request.headers["next-router-state-tree"]))
    assert tree[1]["children"][1]["children"][1]["children"][1]["children"][0] == (
        "search-reward-flights"
    )


def test_availability_request_sends_every_captured_browser_header(settings: Settings) -> None:
    """Akamai reads the whole header set, so none of it may be dropped."""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text=payload())

    AviosClient(_session(settings, handler)).search_reward_calendar(query())

    headers = captured[0].headers
    for name, value in (
        ("accept-encoding", "gzip, deflate, br, zstd"),
        ("accept-language", "en-GB,en;q=0.9"),
        ("priority", "u=1, i"),
        ("sec-ch-ua-mobile", "?0"),
        ("sec-ch-ua-platform", '"macOS"'),
        ("sec-fetch-dest", "empty"),
        ("sec-fetch-mode", "cors"),
        ("sec-fetch-site", "same-origin"),
    ):
        assert headers[name] == value, name
    # A user-agent that disagrees with the client hints is itself a bot signal.
    assert "Chrome/148" in headers["user-agent"]
    assert 'v="148"' in headers["sec-ch-ua"]
    # The manage-avios default header must not leak onto this browser route.
    assert "x-avios-opco" not in headers


def test_exact_date_search_refreshes_and_prices_the_day(settings: Settings) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith(".js"):
            return httpx.Response(200, text=ACTION_CHUNK)
        if request.method == "POST":
            action = request.headers["next-action"]
            return httpx.Response(
                200,
                text=(SINGLE_JOURNEY_RESPONSE if action == JOURNEY_ACTION_ID else PRICING_RESPONSE),
            )
        return httpx.Response(200, text=payload())

    result = AviosClient(_session(settings, handler)).search_reward_calendar(
        query(), departure_date="2099-11-05"
    )

    posts = [request for request in requests if request.method == "POST"]
    assert [post.headers["next-action"] for post in posts] == [
        JOURNEY_ACTION_ID,
        PRICING_ACTION_ID,
    ]
    for post in posts:
        assert post.headers["accept"] == "text/x-component"
        assert post.headers["content-type"] == "text/plain;charset=UTF-8"
        assert post.headers["origin"] == "https://www.avios.com"
        # The results page is the referrer for actions, and it carries the date.
        assert post.headers["referer"].endswith("&departureDate=2099-11-05")
        # ``next-url`` is a navigation header; the capture omits it on actions.
        assert "next-url" not in post.headers

    journey_body = json.loads(posts[0].content)[0]
    assert journey_body == {
        "originAirport": "LON",
        "destinationAirport": "HKG",
        "passengerNumber": {"adults": "1", "youngAdults": "0", "children": "0"},
        "cabins": ["Economy", "Premium Economy", "Business", "First"],
        "opco": "BA",
        "dates": {"departure": "2099-11-05"},
        "companionVoucher": "$undefined",
        "actionToken": journey_body["actionToken"],
    }
    pricing_body = json.loads(posts[1].content)[0]
    assert list(pricing_body) == ["flightCabinPricingRows", "passengerCount", "actionToken"]

    # The refreshed day replaces the calendar's, and prices land on it.
    day = result.day("2099-11-05")
    assert result.priced is True
    assert day.flights[0].seats_for(Cabin.BUSINESS) == 7
    assert day.flights[0].price_for(Cabin.BUSINESS).adult == 121000  # type: ignore[union-attr]


def test_pricing_failure_still_returns_availability(settings: Settings) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(500, text="boom")
        if request.url.path.endswith(".js"):
            return httpx.Response(200, text="")
        return httpx.Response(200, text=payload())

    result = AviosClient(_session(settings, handler)).search_reward_calendar(
        query(), departure_date="2099-11-05"
    )

    assert result.priced is False
    assert result.day("2099-11-05").flights[0].seats_for(Cabin.BUSINESS) == 3


def test_a_block_during_pricing_is_not_swallowed(settings: Settings) -> None:
    """Availability failures degrade quietly; a block has to reach the caller."""
    denial = "Access Denied<P>https://errors.edgesuite.net/18.ec61002.1"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(403, text=denial)
        if request.url.path.endswith(".js"):
            return httpx.Response(200, text=ACTION_CHUNK)
        return httpx.Response(200, text=payload())

    with pytest.raises(RewardSearchBlockedError):
        AviosClient(_session(settings, handler)).search_reward_calendar(
            query(), departure_date="2099-11-05"
        )


def test_remaining_legs_are_abandoned_once_blocked(settings: Settings) -> None:
    calls = {"n": 0}
    denial = "Access Denied<P>https://errors.edgesuite.net/18.ec61002.1"

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text=denial)

    results = AviosClient(_session(settings, handler)).search_reward_legs(
        [RewardLeg(query()), RewardLeg(query(origin="HKG", destination="LON"))]
    )

    assert all(isinstance(result, RewardSearchBlockedError) for result in results)
    # The second leg reuses the first leg's verdict instead of hammering Akamai.
    assert calls["n"] == 1


def test_legs_are_searched_over_one_session_and_fail_independently(
    settings: Settings,
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text=payload())
        return httpx.Response(500, text="upstream exploded")

    results = AviosClient(_session(settings, handler)).search_reward_legs(
        [RewardLeg(query()), RewardLeg(query(origin="HKG", destination="LON"))]
    )

    assert results[0].month == "2099-11"  # type: ignore[union-attr]
    assert isinstance(results[1], Exception)


def test_non_ba_session_is_rejected_before_network(settings: Settings) -> None:
    session = Session(settings, opco="IBP", cookies=COOKIE)
    with pytest.raises(UnsupportedProgramme):
        AviosClient(session).search_reward_calendar(query())


def test_403_without_akamai_markers_asks_the_user_to_log_in_again(settings: Settings) -> None:
    session = _session(settings, lambda request: httpx.Response(403, request=request))
    with pytest.raises(RewardSearchAccessError, match="avios login ba"):
        AviosClient(session).search_reward_calendar(query())


def test_akamai_denial_is_reported_as_a_block_not_a_login_problem(settings: Settings) -> None:
    denial = (
        "<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY>You don't have "
        "permission to access this resource.<P>Reference&#32;#18.ec61002.1786447494"
        "<P>https://errors.edgesuite.net/18.ec61002.1786447494.e5f2b7c6</BODY></HTML>"
    )
    session = _session(settings, lambda request: httpx.Response(403, text=denial))
    with pytest.raises(RewardSearchBlockedError, match="switch network"):
        AviosClient(session).search_reward_calendar(query())


def test_rate_limit_is_reported_as_a_block(settings: Settings) -> None:
    session = _session(settings, lambda request: httpx.Response(429, text=""))
    with pytest.raises(RewardSearchBlockedError):
        AviosClient(session).search_reward_calendar(query())


def test_router_shell_response_is_retried_once(settings: Settings) -> None:
    bodies = iter(['0:{"message":"router shell"}', payload()])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=next(bodies))

    result = AviosClient(_session(settings, handler)).search_reward_calendar(query())
    assert result.month == "2099-11"


# -- browser transport -------------------------------------------------------
class _BrowserPage:
    """Minimal stand-in for a Playwright page driving in-page fetches."""

    url = "https://www.avios.com/en-GB/spend-avios/search-reward-flights"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def goto(self, url: str, **kwargs: object) -> Any:
        return type("Response", (), {"status": 200})()

    def set_default_timeout(self, timeout: int) -> None:
        pass

    def wait_for_load_state(self, state: str, **kwargs: object) -> None:
        pass

    def wait_for_timeout(self, milliseconds: int) -> None:
        pass

    def evaluate(self, expression: str, arg: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(arg)
        return {"status": 200, "body": FIXTURE.read_text(), "error": None}


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


class _BrowserFactory:
    def __init__(self, ctx: _BrowserContext) -> None:
        self.chromium = type(
            "Chromium",
            (),
            {"launch_persistent_context": lambda _self, path, **kwargs: ctx},
        )()

    def __call__(self) -> _BrowserFactory:
        return self

    def __enter__(self) -> _BrowserFactory:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def test_browser_session_seeds_app_cookies_and_fetches_in_page(settings: Settings) -> None:
    ctx = _BrowserContext()
    session = Session(settings, opco="BAEC", base_url="https://www.avios.com", cookies=COOKIE)

    with reward_search_session(session, playwright_factory=_BrowserFactory(ctx)) as search:
        result = search.search(query())

    assert result.month == "2099-11"
    # Only the app session travels: replayed Akamai cookies get the profile blocked.
    assert ctx.seeded_cookies == [
        {"name": "appSession", "value": "x", "domain": "www.avios.com", "path": "/"}
    ]
    call = ctx.page.calls[0]
    assert call["method"] == "GET"
    assert call["headers"]["rsc"] == "1"
    assert call["body"] is None
    assert call["referrer"] == ("https://www.avios.com/en-GB/spend-avios/search-reward-flights")
    assert ctx.closed


def test_browser_session_reuses_one_navigation_for_every_leg(settings: Settings) -> None:
    ctx = _BrowserContext()
    navigations: list[str] = []

    def warm(page: Any, base_url: str) -> None:
        navigations.append(base_url)

    session = Session(settings, opco="BAEC", base_url="https://www.avios.com", cookies=COOKIE)
    with reward_search_session(
        session, playwright_factory=_BrowserFactory(ctx), page_factory=warm
    ) as search:
        search.search(query())
        search.search(query(origin="HKG", destination="LON"))

    assert navigations == ["https://www.avios.com"]
    assert len(ctx.page.calls) == 2
