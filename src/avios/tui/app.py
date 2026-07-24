"""The avios Textual dashboard.

A single screen: a balance header and a scrollable transactions table. Data is
fetched off the event loop (the API client is synchronous httpx) via
``asyncio.to_thread`` inside an exclusive worker, so the UI never blocks.
"""

from __future__ import annotations

import asyncio

import httpx
from textual import work
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Static

from avios.client import AviosClient
from avios.models import Transaction
from avios.session import NotAuthenticated, Session, SessionExpired
from avios.tui.art import banner_text
from avios.tui.widgets import BalanceDisplay

TRANSACTIONS_TO_SHOW = 50


class AviosApp(App[None]):
    """Full-screen Avios dashboard."""

    CSS_PATH = "styles.tcss"
    TITLE = "avios"
    SUB_TITLE = "your Avios, in the terminal"
    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, client: AviosClient | None = None) -> None:
        super().__init__()
        self._client = client or AviosClient(Session())

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
        try:
            balance = await asyncio.to_thread(self._client.get_balance)
        except (NotAuthenticated, SessionExpired) as exc:
            self._show_error(f"{exc}  —  run `avios login`")
            return
        except httpx.HTTPError as exc:
            self._show_error(f"Request failed: {exc}")
            return
        self.query_one("#balance", BalanceDisplay).update_balance(balance)

        # Transactions are experimental (manage-avios session); degrade gracefully
        # so the balance still shows if they're unavailable.
        try:
            transactions = await asyncio.to_thread(
                self._client.get_transactions, TRANSACTIONS_TO_SHOW
            )
        except (NotAuthenticated, SessionExpired, httpx.HTTPError):
            transactions = []
        self._populate_transactions(transactions)

    def _show_error(self, message: str) -> None:
        self.query_one("#balance", BalanceDisplay).update_message(message, error=True)
        self.query_one("#transactions", DataTable).loading = False

    def _populate_transactions(self, transactions: list[Transaction]) -> None:
        table = self.query_one("#transactions", DataTable)
        table.loading = False
        table.clear(columns=True)
        if not transactions:
            table.add_column("info")
            table.add_row("No transactions")
            return
        table.add_columns("Date", "Description", "Avios", "Type")
        for txn in transactions:
            date = (txn.date_processed or "")[:10]
            desc = (txn.description or "").splitlines()[0][:44] if txn.description else ""
            amount = f"{txn.amount:+,}" if txn.amount is not None else ""
            kind = txn.type.value if txn.type else ""
            table.add_row(date, desc, amount, kind)


def run(client: AviosClient | None = None) -> None:
    """Launch the dashboard."""
    AviosApp(client=client).run()
