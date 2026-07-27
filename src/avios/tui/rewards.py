"""Textual reward-flight search form and result tables."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date as Date
from typing import Protocol

from pydantic import ValidationError
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Input,
    Label,
    Select,
    Static,
)

from avios.rewards import Cabin, PassengerCounts, RewardCalendar, RewardSearchQuery


class RewardSearchClient(Protocol):
    """Small client seam used by the TUI and its offline tests."""

    def search_reward_calendar(self, query: RewardSearchQuery) -> RewardCalendar: ...


@dataclass(frozen=True)
class FlightSearchSpec:
    mode: str
    outbound_query: RewardSearchQuery
    inbound_query: RewardSearchQuery | None
    outbound_value: str
    inbound_value: str | None
    cabins: tuple[Cabin, ...]
    show_unavailable: bool


class RewardFlightsPane(Vertical):
    """Search form plus independent outbound and inbound result tables."""

    def __init__(self, client: RewardSearchClient | None = None) -> None:
        super().__init__(id="reward-flights-pane")
        self._client = client
        self.last_status = ""

    @staticmethod
    def _titled_input(
        title: str,
        *,
        widget_id: str,
        value: str = "",
        placeholder: str = "",
    ) -> Input:
        widget = Input(value=value, placeholder=placeholder, id=widget_id)
        widget.border_title = title
        return widget

    def compose(self) -> ComposeResult:
        yield Label("British Airways reward-flight availability", id="flight-title")
        with Vertical(id="flight-form"):
            with Horizontal(classes="flight-form-row"):
                mode = Select(
                    [("Exact date", "date"), ("Month calendar", "calendar")],
                    value="date",
                    allow_blank=False,
                    id="flight-mode",
                )
                mode.border_title = "Mode"
                yield mode
                yield self._titled_input("Origin", widget_id="flight-origin", placeholder="LON")
                yield self._titled_input(
                    "Destination", widget_id="flight-destination", placeholder="ABZ"
                )
                yield self._titled_input(
                    "Outbound",
                    widget_id="flight-outbound",
                    placeholder="YYYY-MM-DD",
                )
                yield self._titled_input(
                    "Return",
                    widget_id="flight-return",
                    placeholder="optional",
                )
            with Horizontal(classes="flight-form-row"):
                yield self._titled_input("Adults", widget_id="flight-adults", value="1")
                yield self._titled_input("Young adults", widget_id="flight-young-adults", value="0")
                yield self._titled_input("Children", widget_id="flight-children", value="0")
                yield self._titled_input("Infants", widget_id="flight-infants", value="0")
                yield Checkbox("Show unavailable", id="flight-show-unavailable")
                yield Button("Search", variant="primary", id="flight-search")
            with Horizontal(id="flight-cabins"):
                yield Checkbox("Economy", True, id="cabin-economy")
                yield Checkbox("Premium Economy", True, id="cabin-premium")
                yield Checkbox("Business", True, id="cabin-business")
                yield Checkbox("First", True, id="cabin-first")
        yield Static(
            "Enter three-letter airport/city codes and an outbound date.",
            id="flight-status",
        )
        yield Label("Outbound", id="outbound-heading", classes="flight-result-heading")
        yield DataTable(id="outbound-flights", zebra_stripes=True, cursor_type="row")
        yield Label("Inbound", id="inbound-heading", classes="flight-result-heading")
        yield DataTable(id="inbound-flights", zebra_stripes=True, cursor_type="row")

    def on_mount(self) -> None:
        self.query_one("#inbound-heading").display = False
        self.query_one("#inbound-flights").display = False

    @on(Select.Changed, "#flight-mode")
    def update_mode_placeholders(self, event: Select.Changed) -> None:
        calendar = event.value == "calendar"
        self.query_one("#flight-outbound", Input).placeholder = (
            "Outbound YYYY-MM" if calendar else "Outbound YYYY-MM-DD"
        )
        self.query_one("#flight-return", Input).placeholder = (
            "Return month (optional)" if calendar else "Return date (optional)"
        )

    @on(Button.Pressed, "#flight-search")
    def submit_search(self) -> None:
        self.start_search()

    @on(Input.Submitted)
    def submit_search_from_input(self) -> None:
        self.start_search()

    def start_search(self) -> None:
        if self._client is None:
            self._set_status("No British Airways account — run `avios login ba`", error=True)
            return
        try:
            spec = self._build_spec()
        except (ValueError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                message = str(exc.errors()[0].get("msg", "Invalid search"))
            else:
                message = str(exc)
            self._set_status(message, error=True)
            return
        self.search(spec)

    def _input(self, selector: str) -> str:
        return self.query_one(selector, Input).value.strip()

    def _count(self, selector: str, label: str, *, minimum: int = 0) -> int:
        raw = self._input(selector)
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{label} must be a number") from exc
        if value < minimum:
            raise ValueError(f"{label} must be at least {minimum}")
        return value

    def _build_spec(self) -> FlightSearchSpec:
        mode = str(self.query_one("#flight-mode", Select).value)
        origin = self._input("#flight-origin")
        destination = self._input("#flight-destination")
        outbound = self._input("#flight-outbound")
        inbound = self._input("#flight-return") or None
        if not outbound:
            raise ValueError("Enter an outbound date or month")

        cabins = tuple(
            cabin
            for selector, cabin in (
                ("#cabin-economy", Cabin.ECONOMY),
                ("#cabin-premium", Cabin.PREMIUM_ECONOMY),
                ("#cabin-business", Cabin.BUSINESS),
                ("#cabin-first", Cabin.FIRST),
            )
            if self.query_one(selector, Checkbox).value
        )
        if not cabins:
            raise ValueError("Select at least one cabin")
        passengers = PassengerCounts(
            adults=self._count("#flight-adults", "Adults", minimum=1),
            young_adults=self._count("#flight-young-adults", "Young adults"),
            children=self._count("#flight-children", "Children"),
            infants=self._count("#flight-infants", "Infants"),
        )

        if mode == "date":
            outbound_date = self._date(outbound, "Outbound date")
            inbound_date = self._date(inbound, "Return date") if inbound else None
            if inbound_date is not None and inbound_date < outbound_date:
                raise ValueError("Return date cannot be earlier than outbound date")
            outbound_month = outbound_date.strftime("%Y-%m")
            inbound_month = inbound_date.strftime("%Y-%m") if inbound_date else None
            outbound_value = outbound_date.isoformat()
            inbound_value = inbound_date.isoformat() if inbound_date else None
        else:
            outbound_month = outbound
            inbound_month = inbound
            outbound_value = outbound
            inbound_value = inbound
            if inbound_month is not None and inbound_month < outbound_month:
                raise ValueError("Return month cannot be earlier than outbound month")

        outbound_query = RewardSearchQuery(
            origin=origin,
            destination=destination,
            month=outbound_month,
            passengers=passengers,
            cabins=cabins,
        )
        inbound_query = (
            RewardSearchQuery(
                origin=destination,
                destination=origin,
                month=inbound_month,
                passengers=passengers,
                cabins=cabins,
            )
            if inbound_month is not None
            else None
        )
        return FlightSearchSpec(
            mode=mode,
            outbound_query=outbound_query,
            inbound_query=inbound_query,
            outbound_value=outbound_value,
            inbound_value=inbound_value,
            cabins=cabins,
            show_unavailable=self.query_one("#flight-show-unavailable", Checkbox).value,
        )

    @staticmethod
    def _date(value: str | None, label: str) -> Date:
        try:
            parsed = Date.fromisoformat(value or "")
        except ValueError as exc:
            raise ValueError(f"{label} must use YYYY-MM-DD") from exc
        if parsed < Date.today():
            raise ValueError(f"{label} cannot be in the past")
        return parsed

    @work(exclusive=True)
    async def search(self, spec: FlightSearchSpec) -> None:
        button = self.query_one("#flight-search", Button)
        button.disabled = True
        self._set_status("Searching…")
        assert self._client is not None
        outbound = await self._fetch(spec.outbound_query)
        self._show_leg(
            "Outbound",
            self.query_one("#outbound-heading", Label),
            self.query_one("#outbound-flights", DataTable),
            outbound,
            spec,
            spec.outbound_value,
        )

        inbound: RewardCalendar | Exception | None = None
        if spec.inbound_query is not None and spec.inbound_value is not None:
            self.query_one("#inbound-heading").display = True
            self.query_one("#inbound-flights").display = True
            inbound = await self._fetch(spec.inbound_query)
            self._show_leg(
                "Inbound",
                self.query_one("#inbound-heading", Label),
                self.query_one("#inbound-flights", DataTable),
                inbound,
                spec,
                spec.inbound_value,
            )
        else:
            self.query_one("#inbound-heading").display = False
            self.query_one("#inbound-flights").display = False

        failures = sum(isinstance(result, Exception) for result in (outbound, inbound))
        self._set_status(
            "Search complete"
            if failures == 0
            else f"Search completed with {failures} failed leg(s)",
            error=failures > 0,
        )
        button.disabled = False

    async def _fetch(self, query: RewardSearchQuery) -> RewardCalendar | Exception:
        assert self._client is not None
        try:
            return await asyncio.to_thread(self._client.search_reward_calendar, query)
        except Exception as exc:
            return exc

    def _show_leg(
        self,
        title: str,
        heading: Label,
        table: DataTable[str],
        result: RewardCalendar | Exception,
        spec: FlightSearchSpec,
        requested: str,
    ) -> None:
        table.clear(columns=True)
        if isinstance(result, Exception):
            heading.update(title)
            table.add_column("Error")
            table.add_row(str(result))
            return
        heading.update(f"{title} · {result.origin} → {result.destination} · {requested}")
        if spec.mode == "date":
            self._populate_date(table, result, requested, spec)
        else:
            self._populate_calendar(table, result, spec)

    @staticmethod
    def _duration(minutes: int) -> str:
        hours, remainder = divmod(minutes, 60)
        return f"{hours}h {remainder:02d}m" if hours else f"{remainder}m"

    def _populate_date(
        self,
        table: DataTable[str],
        calendar: RewardCalendar,
        requested: str,
        spec: FlightSearchSpec,
    ) -> None:
        day = calendar.day(requested)
        flights = day.flights if spec.show_unavailable else day.available_flights
        if not flights:
            table.add_column("Info")
            table.add_row("No reward seats found")
            return
        table.add_columns("Flight", "Route", "Depart", "Arrive", "Duration")
        for cabin in spec.cabins:
            table.add_column(cabin.value)
        table.add_column("Peak")
        for flight in flights:
            row = [
                f"{flight.marketing.carrier}{flight.marketing.flight_number}",
                f"{flight.departure_airport}→{flight.arrival_airport}",
                flight.departure_time[11:16],
                flight.arrival_time[11:16],
                self._duration(flight.duration),
                *[
                    str(flight.seats_for(cabin)) if flight.seats_for(cabin) else "—"
                    for cabin in spec.cabins
                ],
                "yes" if flight.peak else "no",
            ]
            table.add_row(*row)

    def _populate_calendar(
        self, table: DataTable[str], calendar: RewardCalendar, spec: FlightSearchSpec
    ) -> None:
        days = [
            day
            for key, day in sorted(calendar.days.items())
            if key >= Date.today().isoformat()
            and (spec.show_unavailable or any(flight.has_availability for flight in day.flights))
        ]
        if not days:
            table.add_column("Info")
            table.add_row("No reward seats found")
            return
        table.add_column("Date")
        for cabin in spec.cabins:
            table.add_column(cabin.value)
        for day in days:
            cells = []
            for cabin in spec.cabins:
                seats = [
                    flight.seats_for(cabin) for flight in day.flights if flight.seats_for(cabin) > 0
                ]
                cells.append(f"{len(seats)} flights · up to {max(seats)}" if seats else "—")
            table.add_row(day.date, *cells)

    def _set_status(self, message: str, *, error: bool = False) -> None:
        self.last_status = message
        self.query_one("#flight-status", Static).update(
            f"[bold red]{message}[/]" if error else message
        )
