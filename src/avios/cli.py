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
from avios.auth import LoginError, import_from_browser, login_via_browser
from avios.client import AviosClient
from avios.models import Balance
from avios.session import NotAuthenticated, Session, SessionExpired

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
def _client() -> AviosClient:
    return AviosClient(Session())


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


def _render_records(records: list[dict[str, Any]], title: str) -> None:
    if not records:
        console.print(f"[dim]No {title.lower()}.[/]")
        return
    keys = [k for k, v in records[0].items() if not isinstance(v, dict | list)][:6]
    table = Table(title=title)
    for key in keys:
        table.add_column(key)
    for record in records:
        table.add_row(*[str(record.get(key, "")) for key in keys])
    console.print(table)


# -- auth commands -----------------------------------------------------------
@app.command()
def login(
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
    """Log in to avios.com (opens a browser once; captures your session cookie)."""
    session = Session()
    try:
        if from_browser:
            result = import_from_browser(session, browser, profile=profile)
            where = f" from {browser} profile '{result.profile}'" if result.profile else ""
            if not result.authenticated:
                console.print(
                    f"[yellow]Imported {result.count} cookie(s){where}, but they're not a "
                    "logged-in avios session.[/]"
                )
                console.print(
                    "Log into avios.com in that profile, pick another with "
                    "[bold]--profile[/], or use [bold]avios login[/] (browser)."
                )
                raise typer.Exit(2)
            console.print(f"[green]Logged in{where}.[/] Saved {result.count} cookie(s).")
        else:
            console.print(
                "Opening a browser — log in normally (password, captcha, SMS code). "
                "Keep the window open; it waits until you reach the dashboard, then "
                "captures your session automatically."
            )
            count = login_via_browser(session, headless=headless)
            console.print(f"[green]Logged in.[/] Saved {count} avios cookie(s).")
    except LoginError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from exc

    try:
        balance = AviosClient(session).get_balance()
        console.print(
            f"[green]✓ Session works.[/] Balance: [bold cyan]{balance.balance:,}[/] Avios"
        )
    except (NotAuthenticated, SessionExpired, httpx.HTTPError) as exc:
        console.print(f"[yellow]Saved, but a test call failed:[/] {escape(str(exc))}")


@app.command()
def logout() -> None:
    """Remove the stored session."""
    Session().clear()
    console.print("[green]Logged out.[/]")


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


@app.command()
def transactions(
    limit: int = typer.Option(20, help="Number of transactions to show."),
    json_out: bool = JSON_OPTION,
) -> None:
    """List recent Avios transactions (experimental; needs the manage-avios session)."""
    with _handle_errors():
        items = _client().get_transactions(limit=limit)
    records = [item.as_dict() for item in items]
    if json_out:
        _print_json(records)
        return
    _render_records(records, "Transactions")


@app.command()
def pending(json_out: bool = JSON_OPTION) -> None:
    """List pending Avios transactions (experimental; needs the manage-avios session)."""
    with _handle_errors():
        items = _client().get_pending_transactions()
    records = [item.as_dict() for item in items]
    if json_out:
        _print_json(records)
        return
    _render_records(records, "Pending")


@app.command()
def overview() -> None:
    """Show the dashboard overview (experimental; needs the manage-avios session)."""
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
