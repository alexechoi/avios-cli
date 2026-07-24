"""Reusable TUI widgets."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from avios.models import Balance


class BalanceDisplay(Static):
    """A one-line header showing the Avios balance (or a status message).

    The rendered plain text is mirrored on :attr:`last_text` for easy assertions.
    """

    last_text: str = ""

    def update_balance(self, balance: Balance) -> None:
        text = Text()
        text.append("Avios  ", style="bold")
        text.append(f"{balance.balance:,}", style="bold cyan")
        if balance.household_avios_balance is not None:
            text.append(f"     Household  {balance.household_avios_balance:,}", style="dim")
        self.last_text = text.plain
        self.update(text)

    def update_message(self, message: str, *, error: bool = False) -> None:
        self.last_text = message
        self.update(Text(message, style="bold red" if error else "dim"))
