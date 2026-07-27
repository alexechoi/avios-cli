"""Fetch and combine data across every logged-in account.

Shared by the CLI (``avios accounts`` / ``balance`` / ``transactions``) and the
TUI so both present the same combined view. Each account is fetched with its own
session concurrently; a per-account failure is captured on the result rather than
aborting the whole view (one expired session shouldn't hide the others).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol, TypeVar

import httpx

from avios.accounts import Account
from avios.config import Settings
from avios.models import Balance, Transaction
from avios.session import NotAuthenticated, SessionExpired


class _Client(Protocol):
    """The client surface aggregation needs (satisfied by AviosClient and FinnairClient)."""

    def get_balance(self) -> Balance: ...
    def get_transactions(self, limit: int = 50) -> list[Transaction]: ...
    def get_pending_transactions(self) -> list[Transaction]: ...


# Per-account failures we tolerate (captured on the result, not raised).
_FETCH_ERRORS = (NotAuthenticated, SessionExpired, httpx.HTTPError)
_MAX_WORKERS = 8

_T = TypeVar("_T")


@dataclass
class AccountBalance:
    """One account's balance, or the error that prevented fetching it."""

    account: Account
    balance: Balance | None = None
    error: str | None = None


@dataclass
class TaggedTransaction:
    """A transaction tagged with the account it belongs to."""

    account: Account
    transaction: Transaction


def _map(accounts: Sequence[Account], fn: Callable[[Account], _T]) -> list[_T]:
    """Run ``fn(account)`` for each account concurrently, preserving order."""
    if not accounts:
        return []
    with ThreadPoolExecutor(max_workers=min(_MAX_WORKERS, len(accounts))) as pool:
        return list(pool.map(fn, accounts))


def all_balances(
    accounts: Sequence[Account], *, settings: Settings | None = None
) -> list[AccountBalance]:
    """Fetch each account's balance concurrently."""

    def fetch(account: Account) -> AccountBalance:
        try:
            balance = account.client(settings).get_balance()
            return AccountBalance(account=account, balance=balance)
        except _FETCH_ERRORS as exc:
            return AccountBalance(account=account, error=str(exc) or exc.__class__.__name__)

    return _map(accounts, fetch)


def combined_total(balances: Sequence[AccountBalance]) -> int:
    """Sum of the successfully-fetched balances (Avios is one shared currency)."""
    return sum(b.balance.balance for b in balances if b.balance is not None)


def _merged(
    accounts: Sequence[Account],
    fetch_one: Callable[[_Client], list[Transaction]],
    *,
    settings: Settings | None,
) -> list[TaggedTransaction]:
    """Fetch each account's transactions, tag them, and merge newest-first.

    A failing account contributes no rows (its error surfaces via ``all_balances``).
    """

    def fetch(account: Account) -> list[TaggedTransaction]:
        try:
            txns = fetch_one(account.client(settings))
            return [TaggedTransaction(account=account, transaction=t) for t in txns]
        except _FETCH_ERRORS:
            return []

    tagged = [item for group in _map(accounts, fetch) for item in group]
    tagged.sort(key=lambda t: t.transaction.date_processed or "", reverse=True)
    return tagged


def merged_transactions(
    accounts: Sequence[Account], *, limit_per: int = 50, settings: Settings | None = None
) -> list[TaggedTransaction]:
    """Merge completed transactions across accounts, newest first."""
    return _merged(accounts, lambda c: c.get_transactions(limit=limit_per), settings=settings)


def merged_pending(
    accounts: Sequence[Account], *, settings: Settings | None = None
) -> list[TaggedTransaction]:
    """Merge pending transactions across accounts, newest first."""
    return _merged(accounts, lambda c: c.get_pending_transactions(), settings=settings)
