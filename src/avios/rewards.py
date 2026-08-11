"""British Airways reward-flight search over the rebuilt avios.com finder.

British Airways replaced the old reward-flight finder with a Next.js App Router
application. Three requests make up its protocol (captured from
``www.avios.com`` in August 2026):

1. ``GET /en-GB/spend-avios/search-reward-flights/results?…&_rsc=<nonce>`` with
   ``rsc: 1`` returns a React Server Components stream. Its largest record holds
   ``flightResults`` — *thirteen* months of departure calendars for the route —
   plus the short-lived ``actionToken``s the page needs for its server actions.
2. ``POST`` to the same route with ``next-action: <id>`` invokes a server action.
   ``getFlightResultsForSingleJourneyAction`` re-reads availability for one date;
   ``fetchPricingAction`` returns the Avios prices.
3. Server-action ids are build-scoped hashes that only appear in the page's
   JavaScript bundles, so they are scraped from the chunk list the RSC payload
   itself advertises and cached per Next.js build id.

Every one of those routes sits behind Akamai Bot Manager, which blocks by IP and
blocks *hard* — a handful of page loads from a shared office address is enough to
earn a site-wide 403. So all traffic goes through one real Chrome profile, one
navigation per run, and in-page ``fetch()`` for everything after that: Chrome
supplies the TLS fingerprint, the ``_abck`` cookie and every automatic header
(``sec-fetch-*``, ``sec-ch-ua*``, ``accept-encoding``, ``priority``) that a
hand-rolled HTTP client gets subtly wrong.
"""

from __future__ import annotations

import contextlib
import json
import random
import re
import secrets
import string
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from avios import endpoints
from avios.config import Settings
from avios.session import Session, SessionExpired

BA_OPCO = "BAEC"
LOCALE = "en-GB"
OPCO_SEGMENT = "BA"
REWARD_SEARCH_PAGE = f"/{LOCALE}/spend-avios/search-reward-flights"
REWARD_FLIGHT_RESULTS = endpoints.REWARD_FLIGHT_RESULTS
NEXT_URL_SEARCH = f"/{LOCALE}/{OPCO_SEGMENT}/spend-avios/search-reward-flights"

#: Server actions used by the results page, in the order we discover them.
PRICING_ACTION = "fetchPricingAction"
SINGLE_JOURNEY_ACTION = "getFlightResultsForSingleJourneyAction"

#: Minimum gap between two app requests in one search session. Akamai rate-limits
#: this route aggressively; pacing costs a second and avoids an IP-wide block.
REQUEST_INTERVAL_S = 1.6
#: How long to let the landing page finish its Akamai sensor round-trip before the
#: first RSC fetch. Requests sent before ``_abck`` is validated come back 403.
SENSOR_SETTLE_MS = 2_500
NAVIGATION_TIMEOUT_MS = 60_000
FETCH_TIMEOUT_MS = 60_000


class RewardSearchError(RuntimeError):
    """Base class for reward-search failures."""


class RewardSearchProtocolError(RewardSearchError):
    """avios.com returned an RSC shape this version does not understand."""


class RewardSearchRangeError(RewardSearchError):
    """The requested month was not present in the current booking window."""


class RewardSearchAccessError(RewardSearchError):
    """The BA flight-search app rejected an otherwise stored account session."""


class RewardSearchBlockedError(RewardSearchError):
    """Akamai Bot Manager blocked the request (403 challenge page)."""


class UnsupportedProgramme(RewardSearchError):
    """Reward search was attempted with a non-BA Avios session."""


class Cabin(str, Enum):
    """Cabin names accepted by avios.com's flight finder."""

    ECONOMY = "Economy"
    PREMIUM_ECONOMY = "Premium Economy"
    BUSINESS = "Business"
    FIRST = "First"

    @property
    def codes(self) -> tuple[str, ...]:
        """Booking-class codes that map to this cabin.

        Mirrors the site's own ``cabinClassCodeMap``. Business is the one that
        bites: BA's availability rows use ``J``, not the ``C`` the map lists first.
        """
        return {
            Cabin.ECONOMY: ("M", "Y"),
            Cabin.PREMIUM_ECONOMY: ("W",),
            Cabin.BUSINESS: ("C", "J"),
            Cabin.FIRST: ("F",),
        }[self]


ALL_CABINS = tuple(Cabin)
#: Reverse of :attr:`Cabin.codes`, matching the site's ``cabinCodeToCabinClassMap``.
CABIN_BY_CODE = {code: cabin for cabin in ALL_CABINS for code in cabin.codes}
#: RBD that only sells against a BA companion voucher — not general availability.
COMPANION_VOUCHER_RBD = "I"

_AIRPORT_RE = re.compile(r"^[A-Z]{3}$")
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_CHUNK_RE = re.compile(r"/spend-avios/_next/static/chunks/[A-Za-z0-9_\-]+\.js")
_SERVER_REFERENCE_RE = re.compile(
    r"""createServerReference\)?\(\s*["']([0-9a-f]{8,})["'].*?["'](\w+Action)["']"""
)


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
        if self.adults + self.young_adults + self.children + self.infants > 9:
            raise ValueError("British Airways allows at most 9 travellers")
        return self

    def query_items(self) -> list[tuple[str, str]]:
        return [
            ("adults", str(self.adults)),
            ("youngAdults", str(self.young_adults)),
            ("children", str(self.children)),
            ("infants", str(self.infants)),
        ]

    def pricing_payload(self) -> dict[str, str]:
        """``passengerCount`` as ``fetchPricingAction`` expects it (strings)."""
        return {
            "adults": str(self.adults),
            "youngAdults": str(self.young_adults),
            "children": str(self.children),
            "infants": str(self.infants),
        }

    def journey_payload(self) -> dict[str, str]:
        """``passengerNumber`` for the single-journey action (no infants field)."""
        return {
            "adults": str(self.adults),
            "youngAdults": str(self.young_adults),
            "children": str(self.children),
        }


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

    @property
    def cabin_codes(self) -> frozenset[str]:
        return frozenset(code for cabin in self.cabins for code in cabin.codes)


class RewardPrice(RewardModel):
    """One row of ``fetchPricingAction`` output: the Avios cost of a cabin."""

    pricing_row_key: str | None = Field(default=None, alias="pricingRowKey")
    flight_number: str | None = Field(default=None, alias="flightNumber")
    carrier: str | None = None
    departure_date: str | None = Field(default=None, alias="departureDate")
    departure_time: str | None = Field(default=None, alias="departureTime")
    cabin_code: str | None = Field(default=None, alias="cabinCode")
    rbd: str | None = None
    adult: int = 0
    young_adult: int = Field(default=0, alias="youngAdult")
    child: int = 0
    infant: int = 0

    def total_for(self, passengers: PassengerCounts) -> int:
        """Avios for the whole party, before any companion-voucher discount."""
        return (
            self.adult * passengers.adults
            + self.young_adult * passengers.young_adults
            + self.child * passengers.children
            + self.infant * passengers.infants
        )


class MarketingFlight(RewardModel):
    flight_number: str = Field(alias="flightNumber")
    carrier: str


class CabinAvailability(RewardModel):
    cabin: str
    rbd: str | None = None
    state: str | None = None
    seats_available: int = Field(default=0, alias="seatsAvailable")
    fare_basis_code: str | None = Field(default=None, alias="fareBasisCode")
    #: Filled in by :func:`attach_prices` once ``fetchPricingAction`` has replied.
    price: RewardPrice | None = None

    @property
    def cabin_class(self) -> Cabin | None:
        return CABIN_BY_CODE.get(self.cabin)

    @property
    def companion_voucher_only(self) -> bool:
        """Business ``I`` inventory only sells against a companion voucher."""
        return self.cabin_class is Cabin.BUSINESS and self.rbd == COMPANION_VOUCHER_RBD


class RewardFlight(RewardModel):
    departure_airport: str = Field(alias="departureAirport")
    arrival_airport: str = Field(alias="arrivalAirport")
    departure_city: str | None = Field(default=None, alias="departureCity")
    arrival_city: str | None = Field(default=None, alias="arrivalCity")
    departure_terminal: str | None = Field(default=None, alias="departureTerminal")
    arrival_terminal: str | None = Field(default=None, alias="arrivalTerminal")
    departure_time: str = Field(alias="departureTime")
    arrival_time: str = Field(alias="arrivalTime")
    duration: int
    direct: bool = True
    peak: bool = False
    marketing: MarketingFlight
    operating: MarketingFlight | None = None
    availability: list[CabinAvailability] = Field(default_factory=list)

    @property
    def has_availability(self) -> bool:
        return any(item.seats_available > 0 for item in self.availability)

    def availability_for(self, cabin: Cabin) -> list[CabinAvailability]:
        """Rows the website would show for ``cabin``.

        Reproduces its Business-cabin rule: ``I`` inventory is companion-voucher
        stock and is only offered when there is no general (``U``) availability.
        """
        rows = [item for item in self.availability if item.cabin in cabin.codes]
        if cabin is not Cabin.BUSINESS:
            return rows
        general = [
            item for item in rows if item.rbd != COMPANION_VOUCHER_RBD and item.seats_available > 0
        ]
        return general or [item for item in rows if item.rbd == COMPANION_VOUCHER_RBD]

    def seats_for(self, cabin: Cabin) -> int:
        return max((item.seats_available for item in self.availability_for(cabin)), default=0)

    def voucher_only_for(self, cabin: Cabin) -> bool:
        """Whether ``cabin``'s only seats need a BA companion voucher."""
        bookable = [item for item in self.availability_for(cabin) if item.seats_available > 0]
        return bool(bookable) and all(item.companion_voucher_only for item in bookable)

    def price_for(self, cabin: Cabin) -> RewardPrice | None:
        """Cheapest priced row for ``cabin``, once prices have been attached."""
        priced = [item.price for item in self.availability_for(cabin) if item.price is not None]
        return min(priced, key=lambda price: price.adult) if priced else None

    def duration_text(self) -> str:
        """``12h 55m`` — the site's own ``formatMinutesToHours`` output."""
        return _duration_text(self.duration)


class RewardJourney(RewardModel):
    """One itinerary for a date. Direct journeys carry exactly one flight."""

    journey_type: str | None = Field(default=None, alias="journeyType")
    total_duration: int | None = Field(default=None, alias="totalDuration")
    direct: bool = True
    flights: list[RewardFlight] = Field(default_factory=list)


class RewardDay(RewardModel):
    date: str
    availability_level: int = Field(alias="availabilityLevel")
    journeys: list[RewardJourney] = Field(default_factory=list)

    @property
    def flights(self) -> list[RewardFlight]:
        """Direct flights for the day (the site's ``getDirectFlightsFromJourneys``)."""
        return [journey.flights[0] for journey in self.journeys if len(journey.flights) == 1]

    @property
    def available_flights(self) -> list[RewardFlight]:
        return [flight for flight in self.flights if flight.has_availability]

    @property
    def out_of_range(self) -> bool:
        """Level 3 means the date sits outside BA's rolling booking window."""
        return self.availability_level == 3 and not self.journeys


class RewardCalendar(RewardModel):
    origin: str
    destination: str
    month: str
    origin_city_name: str | None = Field(default=None, alias="originCityName")
    destination_city_name: str | None = Field(default=None, alias="destinationCityName")
    days: dict[str, RewardDay]
    #: Every month the same response carried, for a useful out-of-range message.
    months_available: list[str] = Field(default_factory=list, alias="monthsAvailable")
    priced: bool = False

    def day(self, departure_date: date | str) -> RewardDay:
        key = departure_date.isoformat() if isinstance(departure_date, date) else departure_date
        return self.days.get(key, RewardDay(date=key, availability_level=3, journeys=[]))


@dataclass(frozen=True)
class RewardLeg:
    """One leg to search: a month/route query and, optionally, an exact date.

    Passing ``departure_date`` makes the search do what the website does when you
    click a day — re-read that date's availability and price it in Avios.
    """

    query: RewardSearchQuery
    departure_date: str | None = None


def _duration_text(minutes: int) -> str:
    hours, remainder = divmod(minutes, 60)
    if hours == 0:
        return f"{remainder}m"
    if remainder == 0:
        return f"{hours}h"
    return f"{hours}h {remainder}m"


# -- request construction ----------------------------------------------------
def _state_tree(*, results_segment: bool) -> str:
    """Reproduce the ``next-router-state-tree`` header byte for byte.

    Two variants appear in the capture: the tree the *search* page sends when it
    navigates to ``/results``, and the tree the *results* page sends with its
    server-action POSTs. The trailing integers are Next.js segment flags; they are
    copied verbatim rather than guessed.
    """
    page: list[Any] = ["__PAGE__", {}, None, None, 4096 if results_segment else 4256]
    leaf: list[Any] = ["results", {"children": page}, None, None, 4096]
    inner: dict[str, Any] = {"children": leaf if results_segment else page}
    tree: list[Any] = [
        "",
        {
            "children": [
                ["locale", LOCALE, "d", None],
                {
                    "children": [
                        ["opco", OPCO_SEGMENT, "d", None],
                        {
                            "children": [
                                "spend-avios",
                                {
                                    "children": [
                                        "search-reward-flights",
                                        inner,
                                        None,
                                        None,
                                        4164,
                                    ]
                                },
                                None,
                                None,
                                4136,
                            ]
                        },
                        None,
                        None,
                        4200,
                    ]
                },
                None,
                None,
                4168,
            ]
        },
        None,
        None,
        4120,
    ]
    return quote(json.dumps(tree, separators=(",", ":")), safe="")


def _rsc_nonce() -> str:
    """A 16-character cache-buster shaped like the one Next.js emits."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def _query_items(
    query: RewardSearchQuery,
    *,
    departure_date: str | None = None,
    nonce: bool = False,
) -> list[tuple[str, str]]:
    items = [
        ("originAirport", query.origin),
        ("destinationAirport", query.destination),
        # The site sends the month *number*; the response spans the whole window.
        ("month", str(int(query.month[5:]))),
        *query.passengers.query_items(),
        ("journeyType", "One Way"),
    ]
    items.extend(("cabin", cabin.value) for cabin in query.cabins)
    if departure_date is not None:
        items.append(("departureDate", departure_date))
    if nonce:
        items.append(("_rsc", _rsc_nonce()))
    return items


def _results_url(base_url: str, items: Sequence[tuple[str, str]]) -> str:
    return f"{base_url}{REWARD_FLIGHT_RESULTS}?{urlencode(list(items))}"


def availability_headers() -> dict[str, str]:
    """Headers for the availability RSC GET, in captured order."""
    return {
        "accept": "*/*",
        "next-router-state-tree": _state_tree(results_segment=False),
        "next-url": NEXT_URL_SEARCH,
        "rsc": "1",
    }


def action_headers(action_id: str) -> dict[str, str]:
    """Headers for a server-action POST, in captured order.

    Note the differences from the GET: ``accept`` narrows to ``text/x-component``,
    ``next-url`` is absent, and the state tree is the results page's.
    """
    return {
        "accept": "text/x-component",
        "content-type": "text/plain;charset=UTF-8",
        "next-action": action_id,
        "next-router-state-tree": _state_tree(results_segment=True),
    }


def pricing_rows(flights: Sequence[RewardFlight], query: RewardSearchQuery) -> list[dict[str, Any]]:
    """Build ``flightCabinPricingRows`` exactly as the page's own helper does.

    Mirrors ``derivePricingRowsFromFlights``: one row per bookable cabin with
    seats, keys in the site's field order, durations pre-formatted.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for flight in flights:
        for item in flight.availability:
            if item.seats_available <= 0 or item.cabin not in query.cabin_codes:
                continue
            cabin = CABIN_BY_CODE.get(item.cabin)
            row = {
                "origin": flight.departure_airport,
                "departureDate": flight.departure_time,
                "departureTime": flight.departure_time[11:16],
                "duration": _duration_text(flight.duration),
                "destination": flight.arrival_airport,
                "arrivalTime": flight.arrival_time[11:16],
                "cabinCode": item.cabin,
                "cabinName": cabin.value if cabin else item.cabin,
                "seats": item.seats_available,
                "carrier": flight.marketing.carrier,
                "flightNumber": flight.marketing.flight_number,
                "rbd": item.rbd,
            }
            key = pricing_row_key(row)
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    return rows


def pricing_row_key(row: Mapping[str, Any]) -> str:
    """The site's ``buildFlightPricingRowKey``: eight fields joined by ``|``."""
    return "|".join(
        str(row.get(field, ""))
        for field in (
            "carrier",
            "flightNumber",
            "origin",
            "destination",
            "departureDate",
            "departureTime",
            "cabinCode",
            "rbd",
        )
    )


def _availability_key(flight: RewardFlight, item: CabinAvailability) -> str:
    return "|".join(
        (
            flight.marketing.carrier,
            flight.marketing.flight_number,
            flight.departure_airport,
            flight.arrival_airport,
            flight.departure_time,
            flight.departure_time[11:16],
            item.cabin,
            str(item.rbd),
        )
    )


def attach_prices(flights: Sequence[RewardFlight], prices: Sequence[RewardPrice]) -> None:
    """Index ``fetchPricingAction`` rows onto the availability they belong to."""
    index = {price.pricing_row_key: price for price in prices if price.pricing_row_key}
    for flight in flights:
        for item in flight.availability:
            price = index.get(_availability_key(flight, item))
            if price is not None:
                item.price = price


# -- response parsing --------------------------------------------------------
def _rsc_records(payload: str) -> Iterator[tuple[str, Any]]:
    """Yield ``(reference, value)`` for every JSON record in an RSC stream."""
    for line in payload.splitlines():
        reference, separator, value = line.partition(":")
        if not separator or not value.startswith(("{", "[")):
            continue
        try:
            yield reference, json.loads(value)
        except json.JSONDecodeError:
            continue


def _find_dict(value: Any, matches: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    """Depth-first search for the first dict satisfying ``matches``.

    The interesting records sit at an unstable depth inside the RSC tree, so we
    search by shape rather than by a hard-coded path.
    """
    if isinstance(value, dict):
        if matches(value):
            return value
        for child in value.values():
            found = _find_dict(child, matches)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_dict(child, matches)
            if found is not None:
                return found
    return None


def _guard_not_signed_out(payload: str) -> None:
    lowered = payload.lower()
    if "sign in" in lowered or "silent_auth" in lowered:
        raise SessionExpired("Session expired. Run `avios login ba` again.")


def _parse_day(day_key: str, raw_day: Any, cabin_codes: frozenset[str]) -> RewardDay:
    if not isinstance(raw_day, dict):
        return RewardDay(date=day_key, availability_level=3, journeys=[])

    raw_journeys = raw_day.get("journeys")
    if raw_journeys is None and isinstance(raw_day.get("flights"), list):
        # Tolerate the pre-2026 flat shape in case BA rolls the change back.
        raw_journeys = [{"journeyType": "outbound", "flights": raw_day["flights"]}]

    journeys: list[RewardJourney] = []
    for raw_journey in raw_journeys if isinstance(raw_journeys, list) else []:
        if not isinstance(raw_journey, dict):
            continue
        journey = RewardJourney.model_validate(raw_journey)
        for flight in journey.flights:
            flight.availability = [
                item for item in flight.availability if item.cabin in cabin_codes
            ]
        journeys.append(journey)

    return RewardDay(
        date=day_key,
        availability_level=int(raw_day.get("availabilityLevel", 3)),
        journeys=journeys,
    )


def parse_reward_rsc(payload: str, query: RewardSearchQuery) -> RewardCalendar:
    """Parse the availability RSC stream and return the requested month."""
    container: dict[str, Any] | None = None
    for _, record in _rsc_records(payload):
        container = _find_dict(record, lambda node: isinstance(node.get("flightResults"), dict))
        if container is not None:
            break

    if container is None:
        _guard_not_signed_out(payload)
        raise RewardSearchProtocolError(
            "avios.com returned an unfamiliar flight-search response; "
            "the private RSC endpoint may have changed"
        )

    flight_results: dict[str, Any] = container["flightResults"]
    months = sorted(key for key in flight_results if _MONTH_RE.fullmatch(key))
    month_data = flight_results.get(query.month)
    if not isinstance(month_data, dict):
        window = f" avios.com currently returns {months[0]}–{months[-1]}." if months else ""
        raise RewardSearchRangeError(
            f"{query.month} is outside the reward-flight booking window.{window}"
        )
    journeys = month_data.get("departureJourneys")
    if not isinstance(journeys, dict):
        raise RewardSearchProtocolError("reward-flight response has no departure journeys")

    cabin_codes = query.cabin_codes
    days = {
        day_key: _parse_day(day_key, raw_day, cabin_codes) for day_key, raw_day in journeys.items()
    }

    return RewardCalendar(
        origin=query.origin,
        destination=query.destination,
        month=query.month,
        origin_city_name=_string_or_none(container.get("originCityName")),
        destination_city_name=_string_or_none(container.get("destinationCityName")),
        days=days,
        months_available=months,
    )


def _string_or_none(value: Any) -> str | None:
    """Drop RSC back-references (``$4:props:…``) that stand in for real strings."""
    if not isinstance(value, str) or value.startswith("$"):
        return None
    return value


def parse_action_tokens(payload: str) -> dict[str, str]:
    """Pull the signed per-action tokens out of the availability RSC stream."""
    for _, record in _rsc_records(payload):
        container = _find_dict(record, lambda node: isinstance(node.get("tokens"), dict))
        if container is None:
            continue
        tokens = container["tokens"]
        return {
            str(name): str(value)
            for name, value in tokens.items()
            if isinstance(value, str) and not value.startswith("$")
        }
    return {}


def _action_result(payload: str) -> Any:
    """Return the resolved value of a server-action response.

    The stream opens with a ``0:`` navigation record whose ``a`` field points at
    the record holding the action's return value (normally ``1``).
    """
    records = dict(_rsc_records(payload))
    reference = "1"
    header = records.get("0")
    if isinstance(header, dict) and isinstance(header.get("a"), str):
        reference = header["a"].lstrip("$@") or "1"
    if reference in records:
        return records[reference]
    for key, value in records.items():
        if key != "0":
            return value
    return None


def parse_single_journey_action(payload: str, query: RewardSearchQuery) -> dict[str, RewardDay]:
    """Parse ``getFlightResultsForSingleJourneyAction`` into day → availability."""
    result = _action_result(payload)
    if not isinstance(result, dict) or "departureFlights" not in result:
        _guard_not_signed_out(payload)
        raise RewardSearchProtocolError(
            "British Airways returned an unfamiliar single-date availability response"
        )
    departures = result.get("departureFlights") or {}
    if not isinstance(departures, dict):
        return {}
    cabin_codes = query.cabin_codes
    return {
        day_key: _parse_day(day_key, raw_day, cabin_codes)
        for day_key, raw_day in departures.items()
    }


def parse_pricing_action(payload: str) -> list[RewardPrice]:
    """Parse ``fetchPricingAction`` into Avios prices."""
    result = _action_result(payload)
    if not isinstance(result, list):
        _guard_not_signed_out(payload)
        raise RewardSearchProtocolError("British Airways returned an unfamiliar pricing response")
    return [RewardPrice.model_validate(row) for row in result if isinstance(row, dict)]


def parse_server_action_ids(script: str) -> dict[str, str]:
    """Map action name → build-scoped id from a Next.js client chunk."""
    return {name: action_id for action_id, name in _SERVER_REFERENCE_RE.findall(script)}


def chunk_paths(payload: str) -> list[str]:
    """Client chunks the RSC payload advertises, locale-prefixed to skip a 307."""
    seen: dict[str, None] = {}
    for path in _CHUNK_RE.findall(payload):
        seen.setdefault(f"/{LOCALE}{path}", None)
    return list(seen)


def build_id(payload: str) -> str | None:
    """Next.js build id (``b``) from an RSC stream, used to cache action ids."""
    for reference, record in _rsc_records(payload):
        if reference == "0" and isinstance(record, dict):
            value = record.get("b")
            if isinstance(value, str) and value:
                return value
    return None


#: Fingerprints of an Akamai Bot Manager denial page, as opposed to an app-level
#: rejection of our session. The reference-id link is the one BA's 403 shows.
_AKAMAI_MARKERS = (
    "edgesuite.net",
    "access denied",
    "reference&#32;#",
    "reference #",
    "you don't have permission to access",
)


def _blocked(status: int, body: str) -> bool:
    """Recognise an Akamai Bot Manager denial rather than an app error."""
    if status == 429:
        return True
    if status != 403:
        return False
    marker = body[:4000].lower()
    return any(needle in marker for needle in _AKAMAI_MARKERS)


_BLOCKED_ADVICE = (
    "Akamai blocked this request from your IP address. British Airways rate-limits "
    "the reward finder hard — wait a few minutes, switch network (a VPN exit works), "
    "and search fewer legs at a time."
)


# -- transports --------------------------------------------------------------
class RewardTransport(Protocol):
    """How a search session actually reaches avios.com."""

    base_url: str

    def rsc_get(
        self, items: Sequence[tuple[str, str]], *, headers: Mapping[str, str], referrer: str
    ) -> str: ...

    def action_post(
        self,
        items: Sequence[tuple[str, str]],
        *,
        headers: Mapping[str, str],
        body: str,
        referrer: str,
    ) -> str: ...

    def get_script(self, path: str) -> str: ...


class _Throttle:
    """Keep at least ``REQUEST_INTERVAL_S`` between app requests, with jitter."""

    def __init__(self, interval: float = REQUEST_INTERVAL_S) -> None:
        self._interval = interval
        self._last = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        gap = self._interval + random.uniform(0, 0.4) - (time.monotonic() - self._last)
        if gap > 0 and self._last:
            time.sleep(gap)
        self._last = time.monotonic()


#: Runs inside the page so Chrome owns the TLS handshake, the cookie jar and every
#: header a client must not set by hand (``sec-fetch-*``, ``origin``, ``priority``,
#: ``accept-encoding``, ``user-agent``, ``sec-ch-ua*``).
_PAGE_FETCH_JS = """
async ({url, method, headers, body, referrer}) => {
  const init = {
    method,
    headers,
    credentials: 'include',
    mode: 'cors',
    referrer,
    referrerPolicy: 'strict-origin-when-cross-origin',
  };
  if (body !== null && body !== undefined) { init.body = body; }
  try {
    const response = await fetch(url, init);
    return {status: response.status, body: await response.text(), error: null};
  } catch (err) {
    return {status: 0, body: '', error: String(err)};
  }
}
"""


class _BrowserTransport:
    """Chrome-backed transport: one navigation, then in-page ``fetch()``."""

    def __init__(self, page: Any, base_url: str, throttle: _Throttle) -> None:
        self._page = page
        self.base_url = base_url
        self._throttle = throttle

    def _fetch(
        self,
        url: str,
        *,
        method: str,
        headers: Mapping[str, str],
        body: str | None,
        referrer: str,
    ) -> str:
        self._throttle.wait()
        result = self._page.evaluate(
            _PAGE_FETCH_JS,
            {
                "url": url,
                "method": method,
                "headers": dict(headers),
                "body": body,
                "referrer": referrer,
            },
        )
        if not isinstance(result, dict):
            raise RewardSearchProtocolError("Chrome returned an unreadable fetch result")
        if result.get("error"):
            raise RewardSearchAccessError(
                f"Chrome could not reach the British Airways reward finder: {result['error']}"
            )
        return _check_response(int(result.get("status", 0)), str(result.get("body", "")), url)

    def rsc_get(
        self, items: Sequence[tuple[str, str]], *, headers: Mapping[str, str], referrer: str
    ) -> str:
        return self._fetch(
            _results_url(self.base_url, items),
            method="GET",
            headers=headers,
            body=None,
            referrer=referrer,
        )

    def action_post(
        self,
        items: Sequence[tuple[str, str]],
        *,
        headers: Mapping[str, str],
        body: str,
        referrer: str,
    ) -> str:
        return self._fetch(
            _results_url(self.base_url, items),
            method="POST",
            headers=headers,
            body=body,
            referrer=referrer,
        )

    def get_script(self, path: str) -> str:
        return self._fetch(
            f"{self.base_url}{path}",
            method="GET",
            headers={"accept": "*/*"},
            body=None,
            referrer=f"{self.base_url}{REWARD_SEARCH_PAGE}",
        )


#: Headers Chrome adds for us in the browser transport and that a bare HTTP client
#: has to state explicitly. Copied from the capture; order is part of the
#: fingerprint, so keep them in this sequence.
_CHROME_FETCH_HEADERS = {
    "accept-encoding": "gzip, deflate, br, zstd",
    "accept-language": "en-GB,en;q=0.9",
    "priority": "u=1, i",
    "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


class _HttpxTransport:
    """Direct-HTTP transport: the test seam, and an escape hatch behind a proxy.

    It sends every header the browser does, but cannot reproduce Chrome's TLS and
    HTTP/2 fingerprint, so Akamai will normally reject it from a plain network.
    Real runs go through :class:`_BrowserTransport`.
    """

    def __init__(self, session: Session, throttle: _Throttle) -> None:
        self._session = session
        self.base_url = session.base_url
        self._throttle = throttle

    def _headers(self, headers: Mapping[str, str], referrer: str, *, post: bool) -> dict[str, str]:
        """Rebuild the captured header set, in the captured order."""
        merged: dict[str, str] = {}
        for name, value in headers.items():
            merged[name] = value
            # ``accept`` leads the captured order; the browser-supplied block sorts
            # in right after it, before the Next.js headers.
            if name == "accept":
                merged.update(_CHROME_FETCH_HEADERS)
        if post:
            merged["origin"] = self.base_url
        merged["referer"] = referrer
        merged["user-agent"] = self._session.settings.user_agent
        return merged

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: Sequence[tuple[str, str]] | None,
        headers: Mapping[str, str],
        body: str | None,
    ) -> httpx.Response:
        self._throttle.wait()
        with self._session.exact_client(headers) as client:
            return client.request(
                method,
                path,
                params=httpx.QueryParams(tuple(params)) if params is not None else None,
                content=body.encode() if body is not None else None,
            )

    def rsc_get(
        self, items: Sequence[tuple[str, str]], *, headers: Mapping[str, str], referrer: str
    ) -> str:
        response = self._send(
            "GET",
            REWARD_FLIGHT_RESULTS,
            params=items,
            headers=self._headers(headers, referrer, post=False),
            body=None,
        )
        return _check_response(response.status_code, response.text, str(response.url))

    def action_post(
        self,
        items: Sequence[tuple[str, str]],
        *,
        headers: Mapping[str, str],
        body: str,
        referrer: str,
    ) -> str:
        response = self._send(
            "POST",
            REWARD_FLIGHT_RESULTS,
            params=items,
            headers=self._headers(headers, referrer, post=True),
            body=body,
        )
        return _check_response(response.status_code, response.text, str(response.url))

    def get_script(self, path: str) -> str:
        response = self._send(
            "GET",
            path,
            params=None,
            headers={"accept": "*/*", "user-agent": self._session.settings.user_agent},
            body=None,
        )
        return response.text if response.status_code == 200 else ""


def _status_error(status: int, body: str) -> Exception:
    if _blocked(status, body):
        return RewardSearchBlockedError(_BLOCKED_ADVICE)
    if status in (301, 302, 307):
        return SessionExpired("Session expired. Run `avios login ba` again.")
    if status in (401, 403):
        return RewardSearchAccessError(
            "British Airways rejected the flight-search session. "
            "Run `avios login ba` again to refresh its browser cookies."
        )
    return RewardSearchError(f"British Airways reward search returned HTTP {status}")


def _check_response(status: int, body: str, url: str) -> str:
    if status == 200:
        return body
    if status == 0:
        raise RewardSearchAccessError(f"No response from {url}")
    raise _status_error(status, body)


# -- search session ----------------------------------------------------------
class RewardSearchSession:
    """Search one or more legs over a single, already-warmed transport."""

    def __init__(self, transport: RewardTransport, settings: Settings | None = None) -> None:
        self._transport = transport
        self._settings = settings
        self._tokens: dict[str, str] = {}
        self._actions: dict[str, str] = {}
        self._chunks: list[str] = []
        self._build_id: str | None = None

    @property
    def base_url(self) -> str:
        return self._transport.base_url

    def search(self, query: RewardSearchQuery, departure_date: str | None = None) -> RewardCalendar:
        """Availability for ``query``'s month, priced for ``departure_date``."""
        payload = self._transport.rsc_get(
            _query_items(query, nonce=True),
            headers=availability_headers(),
            referrer=f"{self.base_url}{REWARD_SEARCH_PAGE}",
        )
        try:
            calendar = parse_reward_rsc(payload, query)
        except RewardSearchProtocolError:
            # The router sometimes answers a rapid second search with only its
            # shell. One retry with a fresh cache-buster recovers.
            payload = self._transport.rsc_get(
                _query_items(query, nonce=True),
                headers=availability_headers(),
                referrer=f"{self.base_url}{REWARD_SEARCH_PAGE}",
            )
            calendar = parse_reward_rsc(payload, query)

        self._tokens = parse_action_tokens(payload) or self._tokens
        self._build_id = build_id(payload) or self._build_id
        self._chunks = chunk_paths(payload)

        if departure_date is not None:
            self._refresh_day(query, calendar, departure_date)
            self._price_day(query, calendar, departure_date)
        return calendar

    # -- exact-date extras (best effort: availability must survive their failure)
    def _refresh_day(
        self, query: RewardSearchQuery, calendar: RewardCalendar, departure_date: str
    ) -> None:
        """Re-read one date the way the site does when you click a day."""
        token = self._tokens.get(SINGLE_JOURNEY_ACTION)
        action_id = self._action_id(SINGLE_JOURNEY_ACTION)
        if not token or not action_id:
            return
        body = json.dumps(
            [
                {
                    "originAirport": query.origin,
                    "destinationAirport": query.destination,
                    "passengerNumber": query.passengers.journey_payload(),
                    "cabins": [cabin.value for cabin in query.cabins],
                    "opco": OPCO_SEGMENT,
                    "dates": {"departure": departure_date},
                    "companionVoucher": "$undefined",
                    "actionToken": token,
                }
            ],
            separators=(",", ":"),
        )
        items = _query_items(query, departure_date=departure_date)
        try:
            payload = self._transport.action_post(
                items,
                headers=action_headers(action_id),
                body=body,
                referrer=_results_url(self.base_url, items),
            )
            fresh = parse_single_journey_action(payload, query)
        except RewardSearchBlockedError:
            # Being blocked must not be swallowed: the caller has to stop searching.
            raise
        except RewardSearchError:
            return
        for day_key, day in fresh.items():
            calendar.days[day_key] = day

    def _price_day(
        self, query: RewardSearchQuery, calendar: RewardCalendar, departure_date: str
    ) -> None:
        """Attach Avios prices for one date via ``fetchPricingAction``."""
        token = self._tokens.get(PRICING_ACTION)
        action_id = self._action_id(PRICING_ACTION)
        flights = calendar.day(departure_date).flights
        rows = pricing_rows(flights, query)
        if not token or not action_id or not rows:
            return
        body = json.dumps(
            [
                {
                    "flightCabinPricingRows": rows,
                    "passengerCount": query.passengers.pricing_payload(),
                    "actionToken": token,
                }
            ],
            separators=(",", ":"),
        )
        items = _query_items(query, departure_date=departure_date)
        try:
            payload = self._transport.action_post(
                items,
                headers=action_headers(action_id),
                body=body,
                referrer=_results_url(self.base_url, items),
            )
            prices = parse_pricing_action(payload)
        except RewardSearchBlockedError:
            # Being blocked must not be swallowed: the caller has to stop searching.
            raise
        except RewardSearchError:
            return
        attach_prices(flights, prices)
        calendar.priced = bool(prices)

    # -- server-action id discovery ------------------------------------------
    def _action_id(self, name: str) -> str | None:
        if name in self._actions:
            return self._actions[name]
        self._actions.update(self._cached_action_ids())
        if name in self._actions:
            return self._actions[name]
        # Results-page chunks are advertised last in the RSC payload, so scan from
        # the end and stop as soon as both actions are known.
        for path in reversed(self._chunks):
            found = parse_server_action_ids(self._transport.get_script(path))
            if not found:
                continue
            self._actions.update(found)
            if PRICING_ACTION in self._actions and SINGLE_JOURNEY_ACTION in self._actions:
                break
        self._store_action_ids()
        return self._actions.get(name)

    def _action_cache_path(self) -> Path | None:
        if self._settings is None or not self._build_id:
            return None
        return self._settings.config_dir / "reward-actions.json"

    def _cached_action_ids(self) -> dict[str, str]:
        path = self._action_cache_path()
        if path is None or not path.exists():
            return {}
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict) or data.get("buildId") != self._build_id:
            return {}
        actions = data.get("actions")
        return {str(k): str(v) for k, v in actions.items()} if isinstance(actions, dict) else {}

    def _store_action_ids(self) -> None:
        path = self._action_cache_path()
        if path is None or not self._actions:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"buildId": self._build_id, "actions": self._actions}))
        except OSError:
            return


# -- browser plumbing --------------------------------------------------------
def _playwright_cookies(session: Session) -> list[dict[str, Any]]:
    """Return only app-session cookies safe to seed into the Chrome profile.

    Akamai cookies are bound to browser state and are refreshed by Chrome. Replaying
    stored ``_abck``/``bm_*`` values over the profile's current jar triggers a 403.
    """
    allowed = {
        "name",
        "value",
        "domain",
        "path",
        "expires",
        "httpOnly",
        "secure",
        "sameSite",
        "partitionKey",
    }
    normalised: list[dict[str, Any]] = []
    for stored in session.browser_cookies():
        name = str(stored.get("name", ""))
        if name != "__session" and not name.startswith("appSession"):
            continue
        cookie = {key: value for key, value in stored.items() if key in allowed}
        if not cookie.get("name") or not cookie.get("value") or not cookie.get("domain"):
            continue
        cookie.setdefault("path", "/")
        normalised.append(cookie)
    return normalised


def _warm_search_page(page: Any, base_url: str) -> None:
    """Load the finder once so Akamai issues a valid ``_abck`` for this profile."""
    response = page.goto(
        f"{base_url}{REWARD_SEARCH_PAGE}",
        wait_until="domcontentloaded",
        timeout=NAVIGATION_TIMEOUT_MS,
    )
    status = getattr(response, "status", 200) if response is not None else 200
    if status in (403, 429):
        raise RewardSearchBlockedError(_BLOCKED_ADVICE)
    if "accounts.britishairways.com" in str(page.url):
        raise RewardSearchAccessError(
            "British Airways reward-flight login is incomplete. Run `avios login ba` "
            "again and complete the second British Airways prompt."
        )
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=20_000)
    # The Akamai sensor POST has to land before our first RSC fetch, or the
    # not-yet-validated cookie earns a 403.
    page.wait_for_timeout(SENSOR_SETTLE_MS)


@contextmanager
def reward_search_session(
    session: Session,
    *,
    playwright_factory: Any = None,
    page_factory: Any = None,
) -> Iterator[RewardSearchSession]:
    """Open one reward-search session, reusing a single Chrome navigation.

    Batch every leg of a trip through the returned session: one warmed browser
    serving many in-page fetches is what keeps this under Akamai's radar.
    """
    if session.opco != BA_OPCO:
        raise UnsupportedProgramme("reward-flight search currently supports British Airways only")

    if session.has_custom_transport:
        # An injected transport means tests or a caller-supplied proxy, neither of
        # which needs the pacing that protects the real endpoint.
        yield RewardSearchSession(_HttpxTransport(session, _Throttle(0)), session.settings)
        return

    throttle = _Throttle()

    from avios.auth import LoginError, _import_sync_playwright, _open_login_context

    factory = playwright_factory or _import_sync_playwright()
    warm = page_factory or _warm_search_page
    profile_dir = str(session.settings.config_dir / "chrome-profile" / "ba")

    try:
        with factory() as pw:
            ctx = _open_login_context(
                pw,
                headless=False,
                user_data_dir=profile_dir,
                user_agent=session.settings.user_agent,
                background=True,
            )
            try:
                cookies = _playwright_cookies(session)
                profile_names = {str(cookie.get("name", "")) for cookie in ctx.cookies()}
                profile_has_reward_session = "__session" in profile_names or any(
                    name.startswith("appSession") for name in profile_names
                )
                if cookies and not profile_has_reward_session:
                    ctx.add_cookies(cookies)
                page = ctx.new_page()
                page.set_default_timeout(FETCH_TIMEOUT_MS)
                warm(page, session.base_url)
                yield RewardSearchSession(
                    _BrowserTransport(page, session.base_url, throttle),
                    session.settings,
                )
            finally:
                ctx.close()
    except LoginError as exc:
        raise RewardSearchAccessError(str(exc)) from exc
    except (RewardSearchError, SessionExpired):
        raise
    except Exception as exc:
        raise RewardSearchAccessError(
            "Chrome could not fetch British Airways reward flights. "
            "Run `avios login ba` again, then retry."
        ) from exc


def search_reward_legs(
    session: Session,
    legs: Sequence[RewardLeg],
    *,
    playwright_factory: Any = None,
    page_factory: Any = None,
) -> list[RewardCalendar | Exception]:
    """Search several legs over one browser session, isolating per-leg failures.

    A block is the exception to that isolation: once Akamai has refused us there is
    nothing to gain from sending the remaining legs, so they inherit the same error.
    """
    results: list[RewardCalendar | Exception] = []
    with reward_search_session(
        session, playwright_factory=playwright_factory, page_factory=page_factory
    ) as search:
        blocked: RewardSearchBlockedError | None = None
        for leg in legs:
            if blocked is not None:
                results.append(blocked)
                continue
            try:
                results.append(search.search(leg.query, leg.departure_date))
            except RewardSearchBlockedError as exc:
                blocked = exc
                results.append(exc)
            except Exception as exc:  # one bad leg should not lose the others
                results.append(exc)
    return results


def search_reward_calendar(
    session: Session,
    query: RewardSearchQuery,
    *,
    departure_date: str | None = None,
) -> RewardCalendar:
    """Fetch one month of BA reward-flight availability."""
    with reward_search_session(session) as search:
        return search.search(query, departure_date)
