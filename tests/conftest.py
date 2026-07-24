"""Shared pytest fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from avios.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure a raw AVIOS_COOKIE from the real environment never leaks into tests."""
    monkeypatch.delenv("AVIOS_COOKIE", raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings pointing at an isolated, temporary config directory."""
    return Settings(config_dir=tmp_path / "avios")


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    def _load(name: str) -> Any:
        return json.loads((FIXTURES / name).read_text())

    return _load
