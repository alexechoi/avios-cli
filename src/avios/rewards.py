"""British Airways reward-flight availability over avios.com's RSC route.

The flight finder is an undocumented Next.js application. Its authenticated GET
returns a React Server Components stream containing a month-indexed availability
calendar. This module keeps that protocol isolated from the regular JSON client.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import date
from enum import Enum
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from avios import endpoints
from avios.session import Session, SessionExpired

BA_OPCO = "BAEC"
LOCALE = "en-GB"
REWARD_SEARCH_PAGE = f"/{LOCALE}/spend-avios/search-reward-flights"
NEXT_URL = f"/{LOCALE}/BA/spend-avios/search-reward-flights"


class RewardSearchError(RuntimeError):
    """Base class for reward-search failures."""


class RewardSearchProtocolError(RewardSearchError):
    """avios.com returned an RSC shape this version does not understand."""


class RewardSearchRangeError(RewardSearchError):
    """The requested month was not present in the current booking window."""


class RewardSearchAccessError(RewardSearchError):
    """The BA flight-search app rejected an otherwise stored account session."""


class UnsupportedProgramme(RewardSearchError):
    """Reward search was attempted with a non-BA Avios session."""


class Cabin(str, Enum):
    """Cabin names accepted by avios.com's flight finder."""

    ECONOMY = "Economy"
    PREMIUM_ECONOMY = "Premium Economy"
    BUSINESS = "Business"
    FIRST = "First"

    @property
    def code(self) -> str:
        return {
            Cabin.ECONOMY: "M",
            Cabin.PREMIUM_ECONOMY: "W",
            Cabin.BUSINESS: "C",
            Cabin.FIRST: "F",
        }[self]


ALL_CABINS = tuple(Cabin)
_AIRPORT_RE = re.compile(r"^[A-Z]{3}$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class RewardModel(BaseModel):
    """Forward-compatible base for the private flight-search payload."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class PassengerCounts(RewardModel):
    adults: int = Field(default=1, ge=1)
    young_adults: int = Field(default=0, ge=0, alias="youngAdults")
    children: int = Field(default=0, ge=0)
    infants: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def infants_need_adults(self) -> PassengerCounts:
        if self.infants > self.adults:
            raise ValueError("infants cannot exceed adults")
        return self

    def query_items(self) -> list[tuple[str, str]]:
        return [
            ("adults", str(self.adults)),
            ("youngAdults", str(self.young_adults)),
            ("children", str(self.children)),
            ("infants", str(self.infants)),
        ]


class RewardSearchQuery(RewardModel):
    origin: str
    destination: str
    month: str
    passengers: PassengerCounts = Field(default_factory=PassengerCounts)
    cabins: tuple[Cabin, ...] = ALL_CABINS

    @field_validator("origin", "destination", mode="before")
    @classmethod
    def normalise_airport(cls, value: Any) -> str:
        code = str(value).strip().upper()
        if not _AIRPORT_RE.fullmatch(code):
            raise ValueError("must be a three-letter IATA or city code")
        return code

    @field_validator("month")
    @classmethod
    def validate_month(cls, value: str) -> str:
        if not _MONTH_RE.fullmatch(value):
            raise ValueError("month must use YYYY-MM")
        year, month = (int(part) for part in value.split("-"))
        today = date.today()
        if (year, month) < (today.year, today.month):
            raise ValueError("month cannot be in the past")
        return value

    @field_validator("cabins")
    @classmethod
    def require_cabin(cls, value: tuple[Cabin, ...]) -> tuple[Cabin, ...]:
        if not value:
            raise ValueError("select at least one cabin")
        return tuple(dict.fromkeys(value))

    @model_validator(mode="after")
    def airports_must_differ(self) -> RewardSearchQuery:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        return self


class MarketingFlight(RewardModel):
    flight_number: str = Field(alias="flightNumber")
    carrier: str


class CabinAvailability(RewardModel):
    cabin: str
    rbd: str | None = None
    state: str | None = None
    seats_available: int = Field(default=0, alias="seatsAvailable")
    fare_basis_code: str | None = Field(default=None, alias="fareBasisCode")


class RewardFlight(RewardModel):
    departure_airport: str = Field(alias="departureAirport")
    arrival_airport: str = Field(alias="arrivalAirport")
    departure_terminal: str | None = Field(default=None, alias="departureTerminal")
    arrival_terminal: str | None = Field(default=None, alias="arrivalTerminal")
    departure_time: str = Field(alias="departureTime")
    arrival_time: str = Field(alias="arrivalTime")
    duration: int
    direct: bool = True
    peak: bool = False
    marketing: MarketingFlight
    availability: list[CabinAvailability] = Field(default_factory=list)

    @property
    def has_availability(self) -> bool:
        return any(item.seats_available > 0 for item in self.availability)

    def seats_for(self, cabin: Cabin) -> int:
        return max(
            (item.seats_available for item in self.availability if item.cabin == cabin.code),
            default=0,
        )


class RewardDay(RewardModel):
    date: str
    availability_level: int = Field(alias="availabilityLevel")
    flights: list[RewardFlight] = Field(default_factory=list)

    @property
    def available_flights(self) -> list[RewardFlight]:
        return [flight for flight in self.flights if flight.has_availability]


class RewardCalendar(RewardModel):
    origin: str
    destination: str
    month: str
    origin_city_name: str | None = Field(default=None, alias="originCityName")
    destination_city_name: str | None = Field(default=None, alias="destinationCityName")
    days: dict[str, RewardDay]

    def day(self, departure_date: date | str) -> RewardDay:
        key = departure_date.isoformat() if isinstance(departure_date, date) else departure_date
        return self.days.get(key, RewardDay(date=key, availability_level=3, flights=[]))


def _router_state_header() -> str:
    state: list[Any] = [
        "",
        {
            "children": [
                ["locale", LOCALE, "d", None],
                {
                    "children": [
                        ["opco", "BA", "d", None],
                        {
                            "children": [
                                "spend-avios",
                                {
                                    "children": [
                                        "search-reward-flights",
                                        {"children": ["__PAGE__", {}, None, None, 0]},
                                        None,
                                        None,
                                        4,
                                    ]
                                },
                                None,
                                None,
                                8,
                            ]
                        },
                        None,
                        None,
                        8,
                    ]
                },
                None,
                None,
                8,
            ]
        },
        None,
        None,
        24,
    ]
    return quote(json.dumps(state, separators=(",", ":")), safe="")


def _query_items(query: RewardSearchQuery, *, include_nonce: bool) -> list[tuple[str, str]]:
    items = [
        ("originAirport", query.origin),
        ("destinationAirport", query.destination),
        ("month", str(int(query.month[5:]))),
        *query.passengers.query_items(),
        ("journeyType", "One Way"),
    ]
    items.extend(("cabin", cabin.value) for cabin in query.cabins)
    if include_nonce:
        items.append(("_rsc", secrets.token_urlsafe(12)))
    return items


def _find_results_container(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        if isinstance(value.get("flightResults"), dict):
            return value
        for child in value.values():
            found = _find_results_container(child)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_results_container(child)
            if found is not None:
                return found
    return None


def parse_reward_rsc(payload: str, query: RewardSearchQuery) -> RewardCalendar:
    """Parse one RSC response and return the requested month's availability."""
    container: dict[str, Any] | None = None
    for line in payload.splitlines():
        _, separator, value = line.partition(":")
        if not separator or not value.startswith(("{", "[")):
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            continue
        container = _find_results_container(decoded)
        if container is not None:
            break

    if container is None:
        lowered = payload.lower()
        if "sign in" in lowered or "silent_auth" in lowered:
            raise SessionExpired("Session expired. Run `avios login ba` again.")
        raise RewardSearchProtocolError(
            "avios.com returned an unfamiliar flight-search response; "
            "the private RSC endpoint may have changed"
        )

    flight_results = container["flightResults"]
    month_data = flight_results.get(query.month)
    if not isinstance(month_data, dict):
        raise RewardSearchRangeError(
            f"{query.month} is outside the reward-flight booking window returned by avios.com"
        )
    journeys = month_data.get("departureJourneys")
    if not isinstance(journeys, dict):
        raise RewardSearchProtocolError("reward-flight response has no departure journeys")

    selected_codes = {cabin.code for cabin in query.cabins}
    days: dict[str, RewardDay] = {}
    for day_key, raw_day in journeys.items():
        if not isinstance(raw_day, dict):
            continue
        raw_flights = raw_day.get("flights", [])
        flights: list[RewardFlight] = []
        if isinstance(raw_flights, list):
            for raw_flight in raw_flights:
                if not isinstance(raw_flight, dict):
                    continue
                flight = RewardFlight.model_validate(raw_flight)
                flight.availability = [
                    item for item in flight.availability if item.cabin in selected_codes
                ]
                flights.append(flight)
        days[day_key] = RewardDay(
            date=day_key,
            availability_level=int(raw_day.get("availabilityLevel", 3)),
            flights=flights,
        )

    return RewardCalendar(
        origin=query.origin,
        destination=query.destination,
        month=query.month,
        originCityName=container.get("originCityName"),
        destinationCityName=container.get("destinationCityName"),
        days=days,
    )


def search_reward_calendar(session: Session, query: RewardSearchQuery) -> RewardCalendar:
    """Fetch and parse one month of BA reward-flight availability."""
    if session.opco != BA_OPCO:
        raise UnsupportedProgramme("reward-flight search currently supports British Airways only")

    request_items = _query_items(query, include_nonce=True)
    referer_query = urlencode(_query_items(query, include_nonce=False))
    referer = f"{session.base_url}{REWARD_SEARCH_PAGE}?{referer_query}"
    headers = {
        "accept": "*/*",
        "rsc": "1",
        "next-router-state-tree": _router_state_header(),
        "next-url": NEXT_URL,
        "referer": referer,
    }
    try:
        payload = session.get_text(
            endpoints.REWARD_FLIGHT_RESULTS,
            params=request_items,
            headers=headers,
        )
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            raise RewardSearchAccessError(
                "British Airways rejected the flight-search session. "
                "Run `avios login ba` again to refresh its browser cookies."
            ) from exc
        raise
    return parse_reward_rsc(payload, query)
