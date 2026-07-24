"""Tests for the pydantic models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from avios.models import Balance, Transaction


def test_balance_parses_confirmed_shape(load_fixture: Callable[[str], Any]) -> None:
    balance = Balance.model_validate(load_fixture("balance.json"))
    assert balance.balance == 75751
    assert balance.household_avios_balance == 75752


def test_balance_alias_round_trips() -> None:
    balance = Balance(balance=100, household_avios_balance=200)
    assert balance.as_dict() == {"balance": 100, "householdAviosBalance": 200}


def test_balance_household_is_optional() -> None:
    balance = Balance.model_validate({"balance": 42})
    assert balance.household_avios_balance is None


def test_extra_fields_are_preserved() -> None:
    txn = Transaction.model_validate({"date": "2026-07-01", "avios": 500, "unknown": "x"})
    dumped = txn.as_dict()
    assert dumped["date"] == "2026-07-01"
    assert dumped["avios"] == 500
    assert dumped["unknown"] == "x"
