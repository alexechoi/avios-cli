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

from avios import __version__, aggregate
from avios.accounts import Account, AccountStore
from avios.aggregate import AccountBalance, TaggedTransaction
from avios.auth import LoginError, import_from_browser, login_via_browser
from avios.client import AviosClient
from avios.finnair import login_finnair_via_browser
from avios.models import Transaction
from avios.programmes import get_programme, programme_slugs
from avios.session import NotAuthenticated, SessionExpired

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="avios — view your Avios balance and transactions from the terminal.",
)
console = Console()

JSON_OPTION = typer.Option(False, "--json", help="Output raw JSON instead of a table.")
ACCOUNT_OPTION = typer.Option(
    None, "--account", "-a", help="Limit to one programme slug (default: all logged-in accounts)."
)


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


def _accounts(slug: str | None = None) -> list[Account]:
    """Resolve the accounts a command should act on: one (by slug) or all logged-in."""
    accounts = AccountStore().list()
    if not accounts:
        raise NotAuthenticated("No accounts. Run `avios login`.")
    if slug is None:
        return accounts
    match = next((a for a in accounts if a.slug == slug), None)
    if match is None:
        raise NotAuthenticated(f"Not logged in to '{slug}'. Run `avios login {slug}`.")
    return [match]


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

    try:
        if prog.backend == "finnair":
            if from_browser:
                raise LoginError(
                    "Finnair uses an OAuth token rather than browser cookies. "
                    "Run `avios login finnair` without --from-browser."
                )
            console.print(
                "Opening a browser — log in to [bold]Finnair Plus[/] and complete MFA. "
                "Keep the window open; the OAuth session is captured automatically."
            )
            credentials = login_finnair_via_browser(headless=headless)
            account = Account.from_programme(prog)
            account.token = credentials.token
            account.api_key = credentials.api_key
            saved_label = "OAuth session"
        elif from_browser:
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
            account = Account.from_programme(prog, cookies=result.cookies)
            saved_label = f"{result.count} cookie(s){where}"
        else:
            console.print(
                f"Opening a browser — log in to [bold]{prog.name}[/] (password, captcha, "
                "SMS code). Keep the window open; it captures your session automatically."
            )
            cookies = login_via_browser(prog, headless=headless)
            account = Account.from_programme(prog, cookies=cookies)
            saved_label = f"{len(cookies)} cookie(s)"
    except LoginError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(1) from exc

    AccountStore().save(account)
    console.print(f"[green]Logged in to {prog.name}.[/] Saved {saved_label}.")
    try:
        balance = account.client().get_balance()
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
def _balance_json(item: AccountBalance) -> dict[str, Any]:
    data: dict[str, Any] = {"programme": item.account.name, "slug": item.account.slug}
    if item.balance is not None:
        data.update(item.balance.as_dict())
    if item.error is not None:
        data["error"] = item.error
    return data


def _txn_json(item: TaggedTransaction) -> dict[str, Any]:
    return {
        "programme": item.account.name,
        "slug": item.account.slug,
        **item.transaction.as_dict(),
    }


def _render_single_balance(item: AccountBalance) -> None:
    if item.balance is None:
        console.print(f"[red]{item.account.name}: {escape(item.error or 'unavailable')}[/]")
        return
    table = Table(show_header=False, box=None)
    table.add_row("[bold]Avios[/]", f"[bold cyan]{item.balance.balance:,}[/]")
    if item.balance.individual is not None:
        table.add_row("Individual", f"{item.balance.individual:,}")
    if item.balance.household is not None:
        table.add_row("Household", f"{item.balance.household:,}")
    console.print(table)


def _render_combined_balance(balances: list[AccountBalance]) -> None:
    table = Table(title="Avios balance")
    table.add_column("Programme")
    table.add_column("Avios", justify="right")
    for item in balances:
        if item.balance is not None:
            table.add_row(item.account.name, f"[cyan]{item.balance.balance:,}[/]")
        else:
            table.add_row(item.account.name, f"[red]{escape(item.error or 'unavailable')}[/]")
    if sum(1 for b in balances if b.balance is not None) > 1:
        table.add_section()
        total = aggregate.combined_total(balances)
        table.add_row("[bold]Combined[/]", f"[bold cyan]{total:,}[/]")
    console.print(table)


@app.command()
def balance(account: str | None = ACCOUNT_OPTION, json_out: bool = JSON_OPTION) -> None:
    """Show your Avios balance across all accounts, with a combined total."""
    with _handle_errors():
        accts = _accounts(account)
        balances = aggregate.all_balances(accts)
    if json_out:
        _print_json([_balance_json(b) for b in balances])
        return
    if len(balances) == 1:
        _render_single_balance(balances[0])
    else:
        _render_combined_balance(balances)


@app.command()
def accounts(json_out: bool = JSON_OPTION) -> None:
    """List logged-in accounts with each balance and their status."""
    with _handle_errors():
        balances = aggregate.all_balances(_accounts())
    if json_out:
        _print_json([_balance_json(b) for b in balances])
        return
    table = Table(title="Accounts")
    table.add_column("Programme")
    table.add_column("Slug")
    table.add_column("Avios", justify="right")
    table.add_column("Status")
    for item in balances:
        if item.balance is not None:
            table.add_row(
                item.account.name, item.account.slug, f"{item.balance.balance:,}", "[green]ok[/]"
            )
        else:
            table.add_row(
                item.account.name,
                item.account.slug,
                "-",
                f"[red]{escape(item.error or 'error')}[/]",
            )
    if sum(1 for b in balances if b.balance is not None) > 1:
        table.add_section()
        table.add_row(
            "[bold]Combined[/]", "", f"[bold cyan]{aggregate.combined_total(balances):,}[/]", ""
        )
    console.print(table)


def _render_tagged_transactions(
    items: list[TaggedTransaction], title: str, *, show_programme: bool
) -> None:
    if not items:
        console.print(f"[dim]No {title.lower()}.[/]")
        return
    table = Table(title=title)
    if show_programme:
        table.add_column("Programme")
    table.add_column("Date")
    table.add_column("Description")
    table.add_column("Avios", justify="right")
    table.add_column("Type")
    for item in items:
        txn: Transaction = item.transaction
        date = (txn.date_processed or "")[:10]
        desc = (txn.description or "").splitlines()[0][:44] if txn.description else ""
        amount = f"{txn.amount:+,}" if txn.amount is not None else ""
        colour = "green" if (txn.amount or 0) >= 0 else "red"
        kind = txn.type.value if txn.type else ""
        row = [date, desc, f"[{colour}]{amount}[/]", kind or ""]
        if show_programme:
            row.insert(0, item.account.name)
        table.add_row(*row)
    console.print(table)


@app.command()
def transactions(
    account: str | None = ACCOUNT_OPTION,
    limit: int = typer.Option(20, help="Number of transactions to show (across all accounts)."),
    json_out: bool = JSON_OPTION,
) -> None:
    """List recent Avios transactions, merged across accounts (newest first)."""
    with _handle_errors():
        accts = _accounts(account)
        tagged = aggregate.merged_transactions(accts, limit_per=limit)[:limit]
    if json_out:
        _print_json([_txn_json(t) for t in tagged])
        return
    _render_tagged_transactions(tagged, "Transactions", show_programme=len(accts) > 1)


@app.command()
def pending(account: str | None = ACCOUNT_OPTION, json_out: bool = JSON_OPTION) -> None:
    """List pending Avios transactions, merged across accounts."""
    with _handle_errors():
        accts = _accounts(account)
        tagged = aggregate.merged_pending(accts)
    if json_out:
        _print_json([_txn_json(t) for t in tagged])
        return
    _render_tagged_transactions(tagged, "Pending", show_programme=len(accts) > 1)


@app.command()
def overview(account: str | None = ACCOUNT_OPTION) -> None:
    """Show the dashboard overview (raw JSON)."""
    with _handle_errors():
        result = _client(account).get_overview()
    _print_json(result.as_dict())


@app.command()
def whoami(account: str | None = ACCOUNT_OPTION, json_out: bool = JSON_OPTION) -> None:
    """Show your profile (name, tier, membership)."""
    with _handle_errors():
        data = _client(account).get_profile().as_dict()
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
def raw(path: str, account: str | None = ACCOUNT_OPTION) -> None:
    """Fetch any endpoint directly and print the response."""
    with _handle_errors():
        data = _client(account).raw(path)
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
