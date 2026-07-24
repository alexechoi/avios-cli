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
from textual.widgets import DataTable, Footer, Header

from avios.client import AviosClient
from avios.models import Transaction
from avios.session import NotAuthenticated, Session, SessionExpired
from avios.tui.widgets import BalanceDisplay

TRANSACTIONS_TO_SHOW = 50
MAX_COLUMNS = 6


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
            transactions = await asyncio.to_thread(
                self._client.get_transactions, TRANSACTIONS_TO_SHOW
            )
        except (NotAuthenticated, SessionExpired) as exc:
            self._show_error(f"{exc}  —  run `avios login`")
            return
        except httpx.HTTPError as exc:
            self._show_error(f"Request failed: {exc}")
            return
        self.query_one("#balance", BalanceDisplay).update_balance(balance)
        self._populate_transactions(transactions)

    def _show_error(self, message: str) -> None:
        self.query_one("#balance", BalanceDisplay).update_message(message, error=True)
        self.query_one("#transactions", DataTable).loading = False

    def _populate_transactions(self, transactions: list[Transaction]) -> None:
        table = self.query_one("#transactions", DataTable)
        table.loading = False
        table.clear(columns=True)
        records = [txn.as_dict() for txn in transactions]
        if not records:
            table.add_column("info")
            table.add_row("No transactions")
            return
        keys = [k for k, v in records[0].items() if not isinstance(v, dict | list)][:MAX_COLUMNS]
        table.add_columns(*keys)
        for record in records:
            table.add_row(*[str(record.get(key, "")) for key in keys])


def run(client: AviosClient | None = None) -> None:
    """Launch the dashboard."""
    AviosApp(client=client).run()
