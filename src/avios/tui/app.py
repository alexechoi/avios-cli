"""The avios Textual dashboard.

A single screen: a combined balance header and a scrollable transactions table
merged across every logged-in account. Data is fetched off the event loop (the
API client is synchronous httpx) via ``asyncio.to_thread`` inside an exclusive
worker, so the UI never blocks. Per-account failures are captured by the
aggregation layer rather than blanking the whole dashboard.
"""

from __future__ import annotations

import asyncio

from textual import work
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Static

from avios import aggregate
from avios.accounts import Account, AccountStore
from avios.aggregate import TaggedTransaction
from avios.tui.art import banner_text
from avios.tui.widgets import BalanceDisplay

TRANSACTIONS_TO_SHOW = 50


class AviosApp(App[None]):
    """Full-screen Avios dashboard across all logged-in accounts."""

    CSS_PATH = "styles.tcss"
    TITLE = "avios"
    SUB_TITLE = "your Avios, in the terminal"
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, accounts: list[Account] | None = None) -> None:
        super().__init__()
        self._accounts = accounts if accounts is not None else AccountStore().list()

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(banner_text(), id="banner")
        yield BalanceDisplay("Loading…", id="balance")
        yield DataTable(id="transactions", zebra_stripes=True, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#transactions", DataTable).loading = True
        self.refresh_data()

    def action_refresh(self) -> None:
        self.query_one("#transactions", DataTable).loading = True
        self.refresh_data()

    @work(exclusive=True)
    async def refresh_data(self) -> None:
        await self._load()

    async def _load(self) -> None:
        if not self._accounts:
            self._show_error("No accounts — run `avios login`")
            return

        balances = await asyncio.to_thread(aggregate.all_balances, self._accounts)
        self.query_one("#balance", BalanceDisplay).update_balances(balances)

        tagged = await asyncio.to_thread(aggregate.merged_transactions, self._accounts)
        self._populate_transactions(tagged, show_programme=len(self._accounts) > 1)

    def _show_error(self, message: str) -> None:
        self.query_one("#balance", BalanceDisplay).update_message(message, error=True)
        self.query_one("#transactions", DataTable).loading = False

    def _populate_transactions(
        self, transactions: list[TaggedTransaction], *, show_programme: bool
    ) -> None:
        table = self.query_one("#transactions", DataTable)
        table.loading = False
        table.clear(columns=True)
        if not transactions:
            table.add_column("info")
            table.add_row("No transactions")
            return
        if show_programme:
            table.add_column("Programme")
        table.add_columns("Date", "Description", "Avios", "Type")
        for item in transactions:
            txn = item.transaction
            date = (txn.date_processed or "")[:10]
            desc = (txn.description or "").splitlines()[0][:44] if txn.description else ""
            amount = f"{txn.amount:+,}" if txn.amount is not None else ""
            kind = txn.type.value if txn.type else ""
            row = [date, desc, amount, kind]
            if show_programme:
                row.insert(0, item.account.name)
            table.add_row(*row)


def run(accounts: list[Account] | None = None) -> None:
    """Launch the dashboard."""
    AviosApp(accounts=accounts).run()
