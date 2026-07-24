"""Tests for the pydantic models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from avios.models import Balance, Transaction


def test_balance_parses_shell_shape(load_fixture: Callable[[str], Any]) -> None:
    balance = Balance.model_validate(load_fixture("balance.json"))
    assert balance.balance == 75751
    assert balance.individual == 75751
    assert balance.household == 112430


def test_balance_round_trips() -> None:
    balance = Balance(balance=100, individual=100, household=200)
    assert balance.as_dict() == {"balance": 100, "individual": 100, "household": 200}


def test_balance_extras_are_optional() -> None:
    balance = Balance.model_validate({"balance": 42})
    assert balance.individual is None
    assert balance.household is None


def test_extra_fields_are_preserved() -> None:
    txn = Transaction.model_validate({"date": "2026-07-01", "avios": 500, "unknown": "x"})
    dumped = txn.as_dict()
    assert dumped["date"] == "2026-07-01"
    assert dumped["avios"] == 500
    assert dumped["unknown"] == "x"
