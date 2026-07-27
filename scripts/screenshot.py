"""Generate the TUI screenshot (SVG) used in the README.

Runs the multi-account dashboard headlessly with demo data (two programmes) and
exports an SVG.

    uv run python scripts/screenshot.py
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from avios import aggregate
from avios.accounts import Account
from avios.aggregate import AccountBalance, TaggedTransaction
from avios.models import Balance, Transaction
from avios.programmes import get_programme
from avios.tui.app import AviosApp

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


async def main() -> None:
    DOCS.mkdir(exist_ok=True)
    # Swap the aggregation layer for demo data (no network) before the app loads.
    aggregate.all_balances = _demo_balances  # type: ignore[assignment]
    aggregate.merged_transactions = _demo_transactions  # type: ignore[assignment]
    app = AviosApp(accounts=[_BA, _IB])
    async with app.run_test(size=(96, 30)) as pilot:
        await app._load()
        await pilot.pause()
        app.save_screenshot(filename="dashboard.svg", path=str(DOCS))
    print(f"wrote {DOCS / 'dashboard.svg'}")


if __name__ == "__main__":
    asyncio.run(main())
