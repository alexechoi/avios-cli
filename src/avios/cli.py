"""Command-line entry point for avios.

This module intentionally stays thin: it wires up the Typer application and the
top-level ``--version`` flag. Individual commands (balance, transactions, login,
the TUI, ...) are added in later changes.
"""

from __future__ import annotations

import typer

from avios import __version__

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="avios — view your Avios balance and transactions from the terminal.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"avios {__version__}")
        raise typer.Exit


@app.callback()
def main(
    version: bool = typer.Option(  # noqa: B008 (Typer relies on the default as a marker)
        False,
        "--version",
        "-V",
        help="Show the version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """avios — a CLI and TUI for avios.com."""


if __name__ == "__main__":
    app()
