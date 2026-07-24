"""Smoke tests for the CLI entry point."""

from __future__ import annotations

from typer.testing import CliRunner

from avios import __version__
from avios.cli import app

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_help_lists_the_app() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "avios" in result.stdout.lower()


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    # no_args_is_help exits with code 0 and prints usage
    assert "Usage" in result.stdout
