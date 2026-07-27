"""Generate the TUI screenshot (SVG) used in the README.

Runs the multi-account dashboard headlessly with demo data (two programmes) and
exports an SVG.

    uv run python scripts/screenshot.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from textual.widgets import Input, Select, TabbedContent

from avios import aggregate
from avios.accounts import Account
from avios.aggregate import AccountBalance, TaggedTransaction
from avios.models import Balance, Transaction
from avios.programmes import get_programme
from avios.rewards import RewardCalendar, RewardSearchQuery
from avios.tui.app import AviosApp
from avios.tui.rewards import RewardFlightsPane

DOCS = Path(__file__).resolve().parent.parent / "docs"

_BA = Account.from_programme(get_programme("ba"), cookies=[{"name": "s", "value": "ba"}])
_IB = Account.from_programme(get_programme("iberia"), cookies=[{"name": "s", "value": "ib"}])

_BALANCES = {"ba": 75751, "iberia": 22040}
_DEMO = [
    (_BA, "2026-07-18", "Flight BA286 SFO-LHR", 8500, "Collection"),
    (_IB, "2026-07-12", "Iberia IB3170 MAD-LHR", 4200, "Reward Flight"),
    (_BA, "2026-07-02", "Tesco groceries", 240, "BA Avios eStore"),
    (_IB, "2026-06-21", "NH Hotels Madrid", 1600, "Hotel"),
    (_BA, "2026-06-09", "Redemption LHR-JFK", -50000, "Reward Flight"),
]


def _demo_balances(accounts: list[Account], **_: Any) -> list[AccountBalance]:
    return [
        AccountBalance(
            a,
            Balance(
                balance=_BALANCES[a.slug],
                individual=_BALANCES[a.slug],
                household=112430 if a.slug == "ba" else None,
            ),
        )
        for a in accounts
    ]


def _demo_transactions(accounts: list[Account], **_: Any) -> list[TaggedTransaction]:
    items = [
        TaggedTransaction(
            acc,
            Transaction.model_validate(
                {
                    "dateProcessed": f"{date}T09:00:00Z",
                    "description": desc,
                    "amount": amount,
                    "type": {"value": kind},
                }
            ),
        )
        for acc, date, desc, amount, kind in _DEMO
    ]
    items.sort(key=lambda t: t.transaction.date_processed or "", reverse=True)
    return items


class _DemoRewardClient:
    def search_reward_calendar(self, query: RewardSearchQuery) -> RewardCalendar:
        day = f"{query.month}-05"
        return RewardCalendar.model_validate(
            {
                "origin": query.origin,
                "destination": query.destination,
                "month": query.month,
                "originCityName": "London",
                "destinationCityName": "Aberdeen",
                "days": {
                    day: {
                        "date": day,
                        "availabilityLevel": 2,
                        "flights": [
                            {
                                "departureAirport": "LHR",
                                "arrivalAirport": "ABZ",
                                "departureTime": f"{day}T06:35:00",
                                "arrivalTime": f"{day}T08:10:00",
                                "duration": 95,
                                "direct": True,
                                "peak": False,
                                "marketing": {"carrier": "BA", "flightNumber": "1300"},
                                "availability": [
                                    {"cabin": "C", "state": "9", "seatsAvailable": 9},
                                    {"cabin": "M", "state": "6", "seatsAvailable": 6},
                                ],
                            },
                            {
                                "departureAirport": "LHR",
                                "arrivalAirport": "ABZ",
                                "departureTime": f"{day}T10:55:00",
                                "arrivalTime": f"{day}T12:35:00",
                                "duration": 100,
                                "direct": True,
                                "peak": True,
                                "marketing": {"carrier": "BA", "flightNumber": "1306"},
                                "availability": [
                                    {"cabin": "C", "state": "4", "seatsAvailable": 4},
                                    {"cabin": "M", "state": "9", "seatsAvailable": 9},
                                ],
                            },
                        ],
                    }
                },
            }
        )


async def main() -> None:
    DOCS.mkdir(exist_ok=True)
    # Swap the aggregation layer for demo data (no network) before the app loads.
    aggregate.all_balances = _demo_balances  # type: ignore[assignment]
    aggregate.merged_transactions = _demo_transactions  # type: ignore[assignment]
    app = AviosApp(accounts=[_BA, _IB], reward_client=_DemoRewardClient())
    async with app.run_test(size=(96, 36)) as pilot:
        await app._load()
        await pilot.pause()
        app.save_screenshot(filename="dashboard.svg", path=str(DOCS))
        app.query_one("#main-tabs", TabbedContent).active = "rewards-tab"
        app.query_one("#flight-mode", Select).value = "date"
        app.query_one("#flight-origin", Input).value = "LON"
        app.query_one("#flight-destination", Input).value = "ABZ"
        app.query_one("#flight-outbound", Input).value = "2026-11-05"
        pane = app.query_one(RewardFlightsPane)
        pane.start_search()
        await app.workers.wait_for_complete()
        await pilot.pause()
        app.save_screenshot(filename="reward-flights.svg", path=str(DOCS))
    print(f"wrote {DOCS / 'dashboard.svg'}")
    print(f"wrote {DOCS / 'reward-flights.svg'}")


if __name__ == "__main__":
    asyncio.run(main())
