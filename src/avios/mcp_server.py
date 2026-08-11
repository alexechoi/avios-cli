"""MCP server exposing Avios balances, transactions and BA reward availability.

Run it with ``avios-mcp`` (stdio transport). It reads the sessions ``avios login``
already created, so there is no login flow here: logging in stays a deliberate
human action.

Every tool is read-only. Nothing books, spends, or mutates a session, and no
cookie, token or API key ever leaves the process — each tool returns an explicit
response model rather than a raw API payload.

Reward search is the one tool with teeth. British Airways guards its flight finder
with Akamai Bot Manager, which blocks by *IP address*, so an agent that fans out a
dozen route/month combinations can get the user banned from the site. Two
safeguards follow from that:

* every leg of a trip goes through one batched, single-browser-session search
  (:func:`~avios.rewards.search_reward_legs`), never a loop of one-leg calls;
* the server enforces its own cooldown between searches, and reports a block as a
  terminal condition so an agent does not treat it as retryable.
"""

from __future__ import annotations

import time
from datetime import date as Date
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field, ValidationError

from avios import __version__, aggregate
from avios.accounts import Account, AccountStore
from avios.client import AviosClient
from avios.programmes import programme_slugs
from avios.rewards import (
    ALL_CABINS,
    Cabin,
    PassengerCounts,
    RewardCalendar,
    RewardFlight,
    RewardLeg,
    RewardSearchBlockedError,
    RewardSearchQuery,
    UnsupportedProgramme,
)
from avios.session import NotAuthenticated, SessionExpired

#: Minimum gap between two reward searches. Availability barely moves minute to
#: minute, and an agent retrying in a loop is exactly what gets an IP blocked.
REWARD_COOLDOWN_S = 30.0
#: Legs per search. A return trip is two; more than that is an agent fanning out.
MAX_LEGS = 2
#: Transactions a single call may return.
MAX_TRANSACTIONS = 200

_CABIN_INPUTS = {
    "economy": Cabin.ECONOMY,
    "premium-economy": Cabin.PREMIUM_ECONOMY,
    "premium economy": Cabin.PREMIUM_ECONOMY,
    "premium": Cabin.PREMIUM_ECONOMY,
    "business": Cabin.BUSINESS,
    "club": Cabin.BUSINESS,
    "first": Cabin.FIRST,
}

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False)

mcp = MCPServer(
    name="avios",
    title="Avios",
    version=__version__,
    # stdio carries JSON-RPC on stdout, so logs must stay on stderr and stay quiet:
    # httpx logs a line per request at INFO, which is noise in a client's log pane.
    log_level="WARNING",
    instructions=(
        "Read-only access to the user's Avios loyalty accounts (British Airways, "
        "Iberia, Aer Lingus, Finnair) and to British Airways reward-flight "
        "availability.\n\n"
        "Sessions come from `avios login`, run by the user in a terminal. If a tool "
        "reports that no account is logged in, or that a session expired, tell the "
        "user to run `avios login <programme>` — you cannot log in for them.\n\n"
        "search_reward_flights is expensive and rate-limited: it drives a real "
        "browser, and British Airways blocks by IP address. Search both directions "
        "of a trip in ONE call using the return_date/return_month arguments. Never "
        "loop over routes, months or dates to explore options, and never retry a "
        "search that reports being blocked."
    ),
)


# -- response models ---------------------------------------------------------
class AccountInfo(BaseModel):
    """A logged-in account. Deliberately carries no cookies or tokens."""

    slug: str
    name: str
    opco: str
    backend: str


class AccountsResult(BaseModel):
    accounts: list[AccountInfo]
    hint: str | None = None


class BalanceEntry(BaseModel):
    account: str
    name: str
    balance: int | None = None
    individual: int | None = None
    household: int | None = None
    error: str | None = None


class BalanceResult(BaseModel):
    balances: list[BalanceEntry]
    combined_total: int = Field(description="Sum of balances that fetched successfully.")
    currency: str = "Avios"


class TransactionEntry(BaseModel):
    account: str
    date: str | None = None
    description: str | None = None
    type: str | None = None
    partner: str | None = None
    amount: int | None = None


class TransactionsResult(BaseModel):
    transactions: list[TransactionEntry]
    count: int
    truncated: bool = False


class ProfileResult(BaseModel):
    """Profile fields the CLI already shows. The raw id token is never included."""

    account: str
    name: str | None = None
    tier: str | None = None
    membership_id: str | None = None
    email: str | None = None


class CabinSeats(BaseModel):
    cabin: str
    seats: int
    avios_per_adult: int | None = None
    avios_total: int | None = Field(
        default=None, description="Avios for the whole party, before any voucher discount."
    )
    companion_voucher_only: bool = False


class FlightResult(BaseModel):
    flight: str
    route: str
    departs: str
    arrives: str
    duration: str
    peak: bool
    cabins: list[CabinSeats]


class DayResult(BaseModel):
    date: str
    flights: list[FlightResult]


class LegResult(BaseModel):
    direction: str
    origin: str
    destination: str
    origin_city: str | None = None
    destination_city: str | None = None
    month: str
    requested_date: str | None = None
    priced: bool = False
    days: list[DayResult]
    error: str | None = None


class RewardSearchResult(BaseModel):
    legs: list[LegResult]
    passengers: dict[str, int]
    cabins: list[str]
    notes: list[str] = Field(default_factory=list)


# -- helpers -----------------------------------------------------------------
def _accounts(account: str | None) -> list[Account]:
    """Resolve which accounts a tool should act on, or explain why it can't."""
    store = AccountStore()
    available = store.list()
    if not available:
        raise ToolError(
            "No Avios account is logged in. Ask the user to run `avios login` "
            f"(programmes: {', '.join(programme_slugs())})."
        )
    if account is None:
        return available
    match = next((item for item in available if item.slug == account), None)
    if match is None:
        logged_in = ", ".join(item.slug for item in available)
        raise ToolError(
            f"Not logged in to '{account}'. Logged-in accounts: {logged_in}. "
            f"Ask the user to run `avios login {account}`."
        )
    return [match]


def _one_account(account: str | None) -> Account:
    resolved = _accounts(account)
    return resolved[0]


def _named(value: Any) -> str | None:
    return getattr(value, "value", None) if value is not None else None


def _transaction_entries(
    tagged: list[aggregate.TaggedTransaction],
) -> list[TransactionEntry]:
    return [
        TransactionEntry(
            account=item.account.slug,
            date=item.transaction.date_processed,
            description=item.transaction.description,
            type=_named(item.transaction.type),
            partner=_named(item.transaction.partner),
            amount=item.transaction.amount,
        )
        for item in tagged
    ]


def _parse_cabins(cabins: list[str] | None) -> tuple[Cabin, ...]:
    if not cabins:
        return ALL_CABINS
    parsed: list[Cabin] = []
    for value in cabins:
        cabin = _CABIN_INPUTS.get(value.strip().lower().replace("_", "-"))
        if cabin is None:
            raise ToolError(
                f"Unknown cabin '{value}'. Choose from: economy, premium-economy, business, first."
            )
        if cabin not in parsed:
            parsed.append(cabin)
    return tuple(parsed)


def _parse_day(value: str, label: str) -> Date:
    try:
        parsed = Date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"{label} must be a date in YYYY-MM-DD form, got '{value}'.") from exc
    if parsed < Date.today():
        raise ToolError(f"{label} is in the past ({value}).")
    return parsed


def _cabin_rows(flight: RewardFlight, cabins: tuple[Cabin, ...]) -> list[CabinSeats]:
    rows: list[CabinSeats] = []
    for cabin in cabins:
        seats = flight.seats_for(cabin)
        if not seats:
            continue
        price = flight.price_for(cabin)
        rows.append(
            CabinSeats(
                cabin=cabin.value,
                seats=seats,
                avios_per_adult=price.adult if price else None,
                avios_total=None,
                companion_voucher_only=flight.voucher_only_for(cabin),
            )
        )
    return rows


def _leg_result(
    direction: str,
    query: RewardSearchQuery,
    departure_date: str | None,
    outcome: RewardCalendar | Exception,
    cabins: tuple[Cabin, ...],
    passengers: PassengerCounts,
) -> LegResult:
    base = LegResult(
        direction=direction,
        origin=query.origin,
        destination=query.destination,
        month=query.month,
        requested_date=departure_date,
        days=[],
    )
    if isinstance(outcome, Exception):
        base.error = str(outcome) or outcome.__class__.__name__
        return base

    base.origin_city = outcome.origin_city_name
    base.destination_city = outcome.destination_city_name
    base.priced = outcome.priced

    wanted = [departure_date] if departure_date else sorted(outcome.days)
    today = Date.today().isoformat()
    for key in wanted:
        day = outcome.day(key)
        if key < today or day.out_of_range:
            continue
        flights = [
            FlightResult(
                flight=f"{flight.marketing.carrier}{flight.marketing.flight_number}",
                route=f"{flight.departure_airport}-{flight.arrival_airport}",
                departs=flight.departure_time,
                arrives=flight.arrival_time,
                duration=flight.duration_text(),
                peak=flight.peak,
                cabins=rows,
            )
            for flight in day.flights
            if (rows := _cabin_rows(flight, cabins))
        ]
        if flights:
            base.days.append(DayResult(date=key, flights=flights))

    for result_day in base.days:
        for result_flight in result_day.flights:
            for row in result_flight.cabins:
                if row.avios_per_adult is not None:
                    row.avios_total = row.avios_per_adult * passengers.adults
    return base


class _Cooldown:
    """Rate-limits reward searches for the lifetime of the server process."""

    def __init__(self, interval: float = REWARD_COOLDOWN_S) -> None:
        self._interval = interval
        self._last: float | None = None

    def check(self) -> None:
        if self._last is None:
            return
        remaining = self._interval - (time.monotonic() - self._last)
        if remaining > 0:
            raise ToolError(
                f"Reward search is rate-limited: wait {remaining:.0f}s before searching "
                "again. British Airways blocks by IP address, so repeated searches risk "
                "locking the user out of the site. Search both directions of a trip in "
                "one call rather than several."
            )

    def mark(self) -> None:
        self._last = time.monotonic()


_cooldown = _Cooldown()


# -- tools -------------------------------------------------------------------
@mcp.tool(
    title="List Avios accounts",
    description=(
        "List the loyalty programme accounts the user is logged in to. Call this "
        "first when you need to know which `account` values the other tools accept."
    ),
    annotations=READ_ONLY,
)
def list_accounts() -> AccountsResult:
    accounts = _accounts(None)
    return AccountsResult(
        accounts=[
            AccountInfo(slug=item.slug, name=item.name, opco=item.opco, backend=item.backend)
            for item in accounts
        ],
        hint=(
            "Reward-flight search needs the 'ba' account."
            if not any(item.slug == "ba" for item in accounts)
            else None
        ),
    )


@mcp.tool(
    title="Get Avios balance",
    description=(
        "Avios balance per account plus a combined total. Avios is one shared "
        "currency across programmes, so the total is meaningful. Omit `account` for "
        "every logged-in account. An account that fails to fetch is reported with an "
        "`error` rather than failing the whole call."
    ),
    annotations=READ_ONLY,
)
def get_balance(
    account: Annotated[
        str | None, Field(description="Programme slug, e.g. 'ba'. Omit for all accounts.")
    ] = None,
) -> BalanceResult:
    balances = aggregate.all_balances(_accounts(account))
    return BalanceResult(
        balances=[
            BalanceEntry(
                account=item.account.slug,
                name=item.account.name,
                balance=item.balance.balance if item.balance else None,
                individual=item.balance.individual if item.balance else None,
                household=item.balance.household if item.balance else None,
                error=item.error,
            )
            for item in balances
        ],
        combined_total=aggregate.combined_total(balances),
    )


@mcp.tool(
    title="Get Avios transactions",
    description=(
        "Completed Avios transactions, newest first, merged across accounts and "
        "tagged with the account each came from."
    ),
    annotations=READ_ONLY,
)
def get_transactions(
    limit: Annotated[int, Field(description="Maximum rows to return.", ge=1)] = 25,
    account: Annotated[
        str | None, Field(description="Programme slug. Omit for all accounts.")
    ] = None,
) -> TransactionsResult:
    capped = min(limit, MAX_TRANSACTIONS)
    tagged = aggregate.merged_transactions(_accounts(account), limit_per=capped)
    entries = _transaction_entries(tagged)
    return TransactionsResult(
        transactions=entries[:capped],
        count=min(len(entries), capped),
        truncated=len(entries) > capped,
    )


@mcp.tool(
    title="Get pending Avios",
    description=("Avios that have been earned but not yet credited, merged across accounts."),
    annotations=READ_ONLY,
)
def get_pending_transactions(
    account: Annotated[
        str | None, Field(description="Programme slug. Omit for all accounts.")
    ] = None,
) -> TransactionsResult:
    entries = _transaction_entries(aggregate.merged_pending(_accounts(account)))
    return TransactionsResult(transactions=entries, count=len(entries))


@mcp.tool(
    title="Get Avios profile",
    description=(
        "The account holder's name, tier, membership number and email for one "
        "programme. Does not return any credential."
    ),
    annotations=READ_ONLY,
)
def whoami(
    account: Annotated[
        str | None, Field(description="Programme slug. Defaults to the first account.")
    ] = None,
) -> ProfileResult:
    resolved = _one_account(account)
    client = resolved.client()
    if not isinstance(client, AviosClient):
        raise ToolError(f"{resolved.name} does not expose a profile endpoint.")
    try:
        claims = client.get_profile().as_dict().get("tokenContent", {})
    except (NotAuthenticated, SessionExpired) as exc:
        raise ToolError(
            f"The {resolved.name} session has expired. Ask the user to run "
            f"`avios login {resolved.slug}` again."
        ) from exc
    if not isinstance(claims, dict):
        claims = {}

    def claim(key: str) -> str | None:
        value = claims.get(f"https://avios.com/{key}")
        return str(value) if value else None

    name = claims.get("name") or " ".join(
        part for part in (claims.get("given_name", ""), claims.get("family_name", "")) if part
    )
    return ProfileResult(
        account=resolved.slug,
        name=str(name).strip() or None,
        tier=claim("customer_tier_name"),
        membership_id=claim("membership_id"),
        email=str(claims.get("email")) if claims.get("email") else None,
    )


@mcp.tool(
    title="Search BA reward flights",
    description=(
        "Direct British Airways reward-seat availability, with the Avios price per "
        "cabin when an exact date is given.\n\n"
        "EXPENSIVE AND RATE-LIMITED. This drives a real Chrome window on the user's "
        "machine, and British Airways guards the flight finder with bot protection "
        "that blocks by IP ADDRESS — over-searching can lock the user out of "
        "britishairways.com entirely. Therefore:\n"
        "- Search a whole trip in ONE call: pass return_date or return_month for the "
        "reverse leg instead of calling twice.\n"
        "- Prefer `month` to scan for availability, then one `departure_date` call to "
        "price the day you picked.\n"
        "- Do NOT loop over routes, dates or months, and do NOT retry a search that "
        "reports being blocked — tell the user instead.\n\n"
        "Give exactly one of departure_date or month. Only direct BA flights are "
        "covered; connecting flights, cash fares and taxes are not modelled."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True),
)
def search_reward_flights(
    origin: Annotated[str, Field(description="Three-letter airport or city code, e.g. LON.")],
    destination: Annotated[str, Field(description="Three-letter airport or city code.")],
    departure_date: Annotated[
        str | None,
        Field(description="Exact outbound date, YYYY-MM-DD. Also returns Avios prices."),
    ] = None,
    month: Annotated[
        str | None,
        Field(description="Outbound month, YYYY-MM. Returns seat counts for the month."),
    ] = None,
    return_date: Annotated[
        str | None, Field(description="Return date, YYYY-MM-DD. Requires departure_date.")
    ] = None,
    return_month: Annotated[
        str | None, Field(description="Return month, YYYY-MM. Requires month.")
    ] = None,
    cabins: Annotated[
        list[str] | None,
        Field(description="Cabins: economy, premium-economy, business, first. Omit for all."),
    ] = None,
    adults: Annotated[int, Field(description="Adult passengers.", ge=1)] = 1,
    young_adults: Annotated[int, Field(description="Young-adult passengers.", ge=0)] = 0,
    children: Annotated[int, Field(description="Child passengers.", ge=0)] = 0,
    infants: Annotated[int, Field(description="Infant passengers.", ge=0)] = 0,
) -> RewardSearchResult:
    if (departure_date is None) == (month is None):
        raise ToolError("Give exactly one of departure_date or month.")
    if return_date is not None and departure_date is None:
        raise ToolError("return_date needs departure_date. Use return_month with month.")
    if return_month is not None and month is None:
        raise ToolError("return_month needs month. Use return_date with departure_date.")

    chosen_cabins = _parse_cabins(cabins)
    try:
        passengers = PassengerCounts(
            adults=adults, young_adults=young_adults, children=children, infants=infants
        )
    except ValidationError as exc:
        raise ToolError(str(exc.errors()[0].get("msg", "invalid passenger counts"))) from exc

    outbound_month: str
    inbound_month: str | None
    inbound_value: str | None = None
    if departure_date is not None:
        outbound_day = _parse_day(departure_date, "departure_date")
        outbound_month = outbound_day.strftime("%Y-%m")
        inbound_day = _parse_day(return_date, "return_date") if return_date else None
        if inbound_day is not None and inbound_day < outbound_day:
            raise ToolError("return_date is before departure_date.")
        inbound_month = inbound_day.strftime("%Y-%m") if inbound_day else None
        inbound_value = inbound_day.isoformat() if inbound_day else None
    else:
        assert month is not None  # guaranteed by the exactly-one check above
        outbound_month, inbound_month = month, return_month
        if inbound_month is not None and inbound_month < outbound_month:
            raise ToolError("return_month is before month.")

    def build(from_code: str, to_code: str, month_value: str) -> RewardSearchQuery:
        try:
            return RewardSearchQuery(
                origin=from_code,
                destination=to_code,
                month=month_value,
                passengers=passengers,
                cabins=chosen_cabins,
            )
        except ValidationError as exc:
            raise ToolError(str(exc.errors()[0].get("msg", "invalid search"))) from exc

    legs = [RewardLeg(build(origin, destination, outbound_month), departure_date)]
    if inbound_month is not None:
        legs.append(RewardLeg(build(destination, origin, inbound_month), inbound_value))
    if len(legs) > MAX_LEGS:  # defensive: the signature cannot express more today
        raise ToolError(f"At most {MAX_LEGS} legs per search.")

    ba = next((item for item in _accounts(None) if item.slug == "ba"), None)
    if ba is None:
        raise ToolError(
            "Reward-flight search needs a British Airways account. Ask the user to "
            "run `avios login ba`."
        )

    _cooldown.check()
    try:
        outcomes = AviosClient(ba.session()).search_reward_legs(legs)
    except RewardSearchBlockedError as exc:
        raise ToolError(
            f"British Airways has blocked this IP address: {exc} Do not retry — tell "
            "the user to wait and switch network."
        ) from exc
    except UnsupportedProgramme as exc:
        raise ToolError(str(exc)) from exc
    except (NotAuthenticated, SessionExpired) as exc:
        raise ToolError(
            "The British Airways reward-flight session has expired. Ask the user to "
            "run `avios login ba` again."
        ) from exc
    finally:
        _cooldown.mark()

    directions = ("outbound", "inbound")
    results = [
        _leg_result(
            directions[index],
            leg.query,
            leg.departure_date,
            outcome,
            chosen_cabins,
            passengers,
        )
        for index, (leg, outcome) in enumerate(zip(legs, outcomes, strict=False))
    ]

    notes: list[str] = []
    if any(
        row.companion_voucher_only
        for leg in results
        for day in leg.days
        for flight in day.flights
        for row in flight.cabins
    ):
        notes.append(
            "Some Business seats are companion-voucher availability only "
            "(companion_voucher_only), not general reward inventory."
        )
    if departure_date is None:
        notes.append("Month searches return seat counts only. Use departure_date for Avios prices.")
    if any(leg.error for leg in results):
        notes.append("A leg failed; see its `error` field.")

    return RewardSearchResult(
        legs=results,
        passengers={
            "adults": passengers.adults,
            "youngAdults": passengers.young_adults,
            "children": passengers.children,
            "infants": passengers.infants,
        },
        cabins=[cabin.value for cabin in chosen_cabins],
        notes=notes,
    )


def main() -> None:
    """Serve over stdio. The ``avios-mcp`` script goes through :mod:`avios.mcp_entry`."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
