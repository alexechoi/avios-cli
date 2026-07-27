"""Command-line interface for avios.

Thin presentation layer: every command builds an :class:`~avios.client.AviosClient`,
calls one method, and renders it with Rich (or raw JSON via ``--json``). Auth
errors from the session layer are turned into friendly messages in one place.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from avios import __version__
from avios.accounts import Account, AccountStore
from avios.auth import LoginError, import_from_browser, login_via_browser
from avios.client import AviosClient
from avios.models import Balance, Transaction
from avios.programmes import get_programme, programme_slugs
from avios.session import NotAuthenticated, SessionExpired

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="avios — view your Avios balance and transactions from the terminal.",
)
console = Console()

JSON_OPTION = typer.Option(False, "--json", help="Output raw JSON instead of a table.")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"avios {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """avios — a CLI and TUI for avios.com."""


# -- helpers -----------------------------------------------------------------
def _client(slug: str | None = None) -> AviosClient:
    """Client for one account (given slug, else the default = first logged in)."""
    store = AccountStore()
    accounts = store.list()
    if not accounts:
        raise NotAuthenticated("No accounts. Run `avios login`.")
    if slug is not None:
        account = store.get(slug)
        if account is None:
            raise NotAuthenticated(f"Not logged in to '{slug}'. Run `avios login {slug}`.")
    else:
        account = accounts[0]
    return AviosClient(account.session())


@contextmanager
def _handle_errors() -> Iterator[None]:
    """Turn session/HTTP errors into friendly messages + exit codes."""
    try:
        yield
    except NotAuthenticated as exc:
        console.print("[red]Not logged in.[/] Run [bold]avios login[/].")
        raise typer.Exit(1) from exc
    except SessionExpired as exc:
        console.print("[red]Session expired.[/] Run [bold]avios login[/] again.")
        raise typer.Exit(2) from exc
    except httpx.HTTPError as exc:
        console.print(f"[red]Request failed:[/] {escape(str(exc))}")
        raise typer.Exit(1) from exc


def _print_json(data: Any) -> None:
    console.print_json(data=data)


# -- auth commands -----------------------------------------------------------
@app.command()
def login(
    programme: str = typer.Argument(
        "ba", help=f"Programme to log in to: {', '.join(programme_slugs())}."
    ),
    from_browser: bool = typer.Option(
        False, "--from-browser", help="Import the cookie from a running browser (no popup)."
    ),
    browser: str = typer.Option("chrome", help="Browser to import from (with --from-browser)."),
    profile: str | None = typer.Option(
        None, help="Browser profile name for --from-browser (e.g. 'Default', 'Profile 1')."
    ),
    headless: bool = typer.Option(
        False, help="Run the login browser headless (only works without captcha/MFA)."
    ),
) -> None:
    """Log in to an Avios programme (opens a browser once; captures the session)."""
    try:
        prog = get_programme(programme)
    except KeyError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from exc

    where = ""
    try:
        if from_browser:
            result = import_from_browser(prog, browser=browser, profile=profile)
            where = f" from {browser} profile '{result.profile}'" if result.profile else ""
            if not result.authenticated:
                console.print(
                    f"[yellow]Imported {result.count} cookie(s){where}, but they're not a "
                    f"logged-in {prog.name} session.[/]"
                )
                console.print(
                    "Log into that programme in the browser profile, pick another with "
                    "[bold]--profile[/], or use [bold]avios login[/] (browser)."
                )
                raise typer.Exit(2)
            cookies = result.cookies
        else:
            console.print(
                f"Opening a browser — log in to [bold]{prog.name}[/] (password, captcha, "
                "SMS code). Keep the window open; it captures your session automatically."
            )
            cookies = login_via_browser(prog, headless=headless)
    except LoginError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from exc

    account = Account.from_programme(prog, cookies=cookies)
    AccountStore().save(account)
    console.print(f"[green]Logged in to {prog.name}{where}.[/] Saved {len(cookies)} cookie(s).")
    try:
        balance = AviosClient(account.session()).get_balance()
        console.print(f"[green]✓ Works.[/] Balance: [bold cyan]{balance.balance:,}[/] Avios")
    except (NotAuthenticated, SessionExpired, httpx.HTTPError) as exc:
        console.print(f"[yellow]Saved, but a test call failed:[/] {escape(str(exc))}")


@app.command()
def logout(
    programme: str | None = typer.Argument(None, help="Programme to log out (default: all)."),
) -> None:
    """Log out of one programme, or all of them."""
    store = AccountStore()
    if programme is not None:
        try:
            prog = get_programme(programme)
        except KeyError as exc:
            console.print(f"[red]{escape(str(exc))}[/]")
            raise typer.Exit(1) from exc
        removed = store.remove(prog.slug)
        console.print(
            f"[green]Logged out of {prog.name}.[/]" if removed else "[dim]Not logged in.[/]"
        )
        return
    slugs = [account.slug for account in store.list()]
    for slug in slugs:
        store.remove(slug)
    console.print(
        f"[green]Logged out of {len(slugs)} account(s).[/]" if slugs else "[dim]No accounts.[/]"
    )


# -- data commands -----------------------------------------------------------
@app.command()
def balance(json_out: bool = JSON_OPTION) -> None:
    """Show your Avios balance."""
    with _handle_errors():
        result = _client().get_balance()
    if json_out:
        _print_json(result.as_dict())
        return
    _render_balance(result)


def _render_balance(result: Balance) -> None:
    table = Table(show_header=False, box=None)
    table.add_row("[bold]Avios[/]", f"[bold cyan]{result.balance:,}[/]")
    if result.individual is not None:
        table.add_row("Individual", f"{result.individual:,}")
    if result.household is not None:
        table.add_row("Household", f"{result.household:,}")
    console.print(table)


def _render_transactions(items: list[Transaction], title: str) -> None:
    if not items:
        console.print(f"[dim]No {title.lower()}.[/]")
        return
    table = Table(title=title)
    table.add_column("Date")
    table.add_column("Description")
    table.add_column("Avios", justify="right")
    table.add_column("Type")
    for txn in items:
        date = (txn.date_processed or "")[:10]
        desc = (txn.description or "").splitlines()[0][:44] if txn.description else ""
        amount = f"{txn.amount:+,}" if txn.amount is not None else ""
        colour = "green" if (txn.amount or 0) >= 0 else "red"
        kind = txn.type.value if txn.type else ""
        table.add_row(date, desc, f"[{colour}]{amount}[/]", kind or "")
    console.print(table)


@app.command()
def transactions(
    limit: int = typer.Option(20, help="Number of transactions to show."),
    json_out: bool = JSON_OPTION,
) -> None:
    """List recent Avios transactions."""
    with _handle_errors():
        items = _client().get_transactions(limit=limit)
    if json_out:
        _print_json([item.as_dict() for item in items])
        return
    _render_transactions(items, "Transactions")


@app.command()
def pending(json_out: bool = JSON_OPTION) -> None:
    """List pending Avios transactions."""
    with _handle_errors():
        items = _client().get_pending_transactions()
    if json_out:
        _print_json([item.as_dict() for item in items])
        return
    _render_transactions(items, "Pending")


@app.command()
def overview() -> None:
    """Show the dashboard overview (raw JSON)."""
    with _handle_errors():
        result = _client().get_overview()
    _print_json(result.as_dict())


@app.command()
def whoami(json_out: bool = JSON_OPTION) -> None:
    """Show your profile (name, tier, membership)."""
    with _handle_errors():
        data = _client().get_profile().as_dict()
    if json_out:
        _print_json(data)
        return
    claims = data.get("tokenContent", {})

    def claim(key: str) -> str:
        return str(claims.get(f"https://avios.com/{key}", "") or "")

    table = Table(show_header=False, box=None)
    name = claims.get("name") or f"{claims.get('given_name', '')} {claims.get('family_name', '')}"
    for label, value in (
        ("Name", str(name).strip()),
        ("Tier", claim("customer_tier_name")),
        ("Membership", claim("membership_id")),
        ("Email", str(claims.get("email", "") or "")),
    ):
        if value:
            table.add_row(f"[bold]{label}[/]", value)
    if table.row_count:
        console.print(table)
    else:
        _print_json(data)


@app.command()
def raw(path: str) -> None:
    """Fetch any endpoint directly and print the response."""
    with _handle_errors():
        data = _client().raw(path)
    if isinstance(data, str):
        console.print(data)
    else:
        _print_json(data)


@app.command()
def tui() -> None:
    """Launch the full-screen dashboard."""
    from avios.tui.app import run

    run()


if __name__ == "__main__":
    app()
