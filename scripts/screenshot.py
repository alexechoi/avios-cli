"""Generate the TUI screenshot (SVG) used in the README.

Runs the dashboard headlessly with demo data and exports an SVG.

    uv run python scripts/screenshot.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from avios.models import Balance, Transaction
from avios.tui.app import AviosApp

DOCS = Path(__file__).resolve().parent.parent / "docs"

_COLUMNS = ("date", "description", "avios", "status")
_DEMO_TRANSACTIONS = [
    ("2026-07-18", "Flight BA286 SFO-LHR", 8500, "Posted"),
    ("2026-07-02", "Tesco groceries", 240, "Posted"),
    ("2026-06-21", "Hilton London", 3120, "Posted"),
    ("2026-06-09", "Redemption LHR-JFK", -50000, "Posted"),
    ("2026-05-30", "Avios eStore - Nike", 900, "Pending"),
]


class DemoClient:
    """A client returning attractive demo data (no network)."""

    def get_balance(self) -> Balance:
        return Balance(balance=75751, household_avios_balance=112430)

    def get_transactions(self, limit: int | None = None) -> list[Transaction]:
        return [
            Transaction.model_validate(dict(zip(_COLUMNS, row, strict=True)))
            for row in _DEMO_TRANSACTIONS
        ]


async def main() -> None:
    DOCS.mkdir(exist_ok=True)
    app = AviosApp(client=DemoClient())  # type: ignore[arg-type]
    async with app.run_test(size=(96, 30)) as pilot:
        await app._load()
        await pilot.pause()
        app.save_screenshot(filename="dashboard.svg", path=str(DOCS))
    print(f"wrote {DOCS / 'dashboard.svg'}")


if __name__ == "__main__":
    asyncio.run(main())
