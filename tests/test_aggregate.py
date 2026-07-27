"""Tests for the multi-account aggregation layer.

The fake ``AviosClient`` reads ``session.opco`` to tell accounts apart and never
makes a network call, so these exercise the concurrency/merge/error-capture logic
without a real session.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from avios.accounts import Account
from avios.aggregate import (
    all_balances,
    combined_total,
    merged_pending,
    merged_transactions,
)
from avios.models import Balance, Transaction
from avios.programmes import get_programme
from avios.session import SessionExpired


def _account(slug: str) -> Account:
    return Account.from_programme(get_programme(slug), cookies=[{"name": "s", "value": slug}])


def _txn(date: str, amount: int) -> Transaction:
    return Transaction.model_validate({"dateProcessed": date, "amount": amount})


class _FakeClient:
    """Stand-in whose responses are keyed by the session's opco."""

    def __init__(
        self,
        session: Any,
        *,
        balances: dict[str, int],
        errors: frozenset[str],
        txns: dict[str, list[Transaction]],
    ) -> None:
        self._opco = session.opco
        self._balances = balances
        self._errors = errors
        self._txns = txns

    def _guard(self) -> None:
        if self._opco in self._errors:
            raise SessionExpired("session expired")

    def get_balance(self) -> Balance:
        self._guard()
        return Balance(balance=self._balances[self._opco])

    def get_transactions(self, limit: int = 50) -> list[Transaction]:
        self._guard()
        return self._txns.get(self._opco, [])[:limit]

    def get_pending_transactions(self) -> list[Transaction]:
        self._guard()
        return self._txns.get(self._opco, [])


@pytest.fixture
def patch_clients(monkeypatch: pytest.MonkeyPatch) -> Callable[..., None]:
    def _apply(
        *,
        balances: dict[str, int] | None = None,
        errors: frozenset[str] = frozenset(),
        txns: dict[str, list[Transaction]] | None = None,
    ) -> None:
        monkeypatch.setattr(
            "avios.aggregate.AviosClient",
            lambda session: _FakeClient(
                session, balances=balances or {}, errors=errors, txns=txns or {}
            ),
        )

    return _apply


def test_all_balances_success(patch_clients: Callable[..., None]) -> None:
    patch_clients(balances={"BAEC": 100, "IBP": 200})
    result = all_balances([_account("ba"), _account("iberia")])
    assert [r.account.slug for r in result] == ["ba", "iberia"]  # order preserved
    assert [r.balance.balance for r in result if r.balance] == [100, 200]
    assert combined_total(result) == 300


def test_all_balances_captures_per_account_error(patch_clients: Callable[..., None]) -> None:
    patch_clients(balances={"BAEC": 100}, errors=frozenset({"IBP"}))
    result = all_balances([_account("ba"), _account("iberia")])
    assert result[0].balance is not None and result[0].balance.balance == 100
    assert result[1].balance is None
    assert result[1].error is not None and "expired" in result[1].error.lower()
    assert combined_total(result) == 100  # a failed account is excluded, not fatal


def test_combined_total_empty() -> None:
    assert combined_total([]) == 0


def test_all_balances_empty_accounts(patch_clients: Callable[..., None]) -> None:
    patch_clients(balances={})
    assert all_balances([]) == []


def test_merged_transactions_sorted_and_tagged(patch_clients: Callable[..., None]) -> None:
    patch_clients(
        balances={},
        txns={
            "BAEC": [_txn("2026-07-01T00:00:00Z", 10)],
            "IBP": [_txn("2026-07-15T00:00:00Z", 20), _txn("2026-05-01T00:00:00Z", 5)],
        },
    )
    merged = merged_transactions([_account("ba"), _account("iberia")], limit_per=50)
    # Newest first, across accounts, each tagged with its programme.
    assert [m.transaction.amount for m in merged] == [20, 10, 5]
    assert merged[0].account.slug == "iberia"
    assert merged[1].account.slug == "ba"


def test_merged_transactions_skips_failed_account(patch_clients: Callable[..., None]) -> None:
    patch_clients(
        balances={},
        errors=frozenset({"IBP"}),
        txns={"BAEC": [_txn("2026-07-01T00:00:00Z", 10)]},
    )
    merged = merged_transactions([_account("ba"), _account("iberia")])
    assert [m.account.slug for m in merged] == ["ba"]  # Iberia failed → contributes nothing


def test_merged_pending(patch_clients: Callable[..., None]) -> None:
    patch_clients(balances={}, txns={"BAEC": [_txn("2026-07-01T00:00:00Z", -100)]})
    pending = merged_pending([_account("ba")])
    assert len(pending) == 1
    assert pending[0].transaction.amount == -100
