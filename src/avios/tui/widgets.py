"""Reusable TUI widgets."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static

from avios.aggregate import AccountBalance


class BalanceDisplay(Static):
    """Header showing the combined Avios balance across accounts (or a status message).

    The rendered plain text is mirrored on :attr:`last_text` for easy assertions.
    """

    last_text: str = ""

    def update_balances(self, balances: list[AccountBalance]) -> None:
        ok = [b for b in balances if b.balance is not None]
        if not ok:
            self.update_message("No balances available — run `avios login`", error=True)
            return

        text = Text()
        if len(balances) == 1:
            # Single account keeps the individual/household detail.
            bal = ok[0].balance
            assert bal is not None
            text.append("Avios  ", style="bold")
            text.append(f"{bal.balance:,}", style="bold cyan")
            if bal.household is not None:
                text.append(f"     Household  {bal.household:,}", style="dim")
        else:
            total = sum(b.balance.balance for b in ok if b.balance is not None)
            text.append("Combined  ", style="bold")
            text.append(f"{total:,}", style="bold cyan")
            parts = [
                f"{b.account.name} {b.balance.balance:,}"
                if b.balance is not None
                else f"{b.account.name} ⚠"
                for b in balances
            ]
            text.append("     " + "   ·   ".join(parts), style="dim")

        self.last_text = text.plain
        self.update(text)

    def update_message(self, message: str, *, error: bool = False) -> None:
        self.last_text = message
        self.update(Text(message, style="bold red" if error else "dim"))
