"""High-level, typed client for the avios.com internal API.

:class:`AviosClient` wraps a :class:`~avios.session.Session` and returns pydantic
models. Session/expiry errors from the session layer propagate unchanged so the
CLI/TUI can render a single, friendly "please log in again" message.
"""

from __future__ import annotations

from typing import Any

from avios import endpoints
from avios.models import Account, Balance, Overview, Profile, Transaction
from avios.session import Session


def _extract_list(payload: Any) -> list[dict[str, Any]]:
    """Return the list of items from a payload.

    Handles both a bare JSON array and an object that wraps the array under some
    key (e.g. ``{"transactions": [...]}``). Returns ``[]`` if none is found.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for value in payload.values():
            if isinstance(value, list):
                return value
    return []


class AviosClient:
    """Typed access to a user's Avios account."""

    def __init__(self, session: Session | None = None) -> None:
        self.session = session or Session()

    def get_balance(self) -> Balance:
        return Balance.model_validate(self.session.get_json(endpoints.BALANCE))

    def get_profile(self) -> Profile:
        return Profile.model_validate(self.session.get_json(endpoints.PROFILE))

    def get_overview(self) -> Overview:
        return Overview.model_validate(self.session.get_json(endpoints.OVERVIEW))

    def get_accounts(self) -> list[Account]:
        payload = self.session.get_json(endpoints.ACCOUNTS)
        return [Account.model_validate(item) for item in _extract_list(payload)]

    def get_transactions(self, limit: int | None = None) -> list[Transaction]:
        payload = self.session.get_json(endpoints.TRANSACTIONS)
        items = _extract_list(payload)
        transactions = [Transaction.model_validate(item) for item in items]
        return transactions[:limit] if limit is not None else transactions

    def get_pending_transactions(self) -> list[Transaction]:
        payload = self.session.get_json(endpoints.TRANSACTIONS_PENDING)
        return [Transaction.model_validate(item) for item in _extract_list(payload)]

    def raw(self, path: str) -> Any:
        """Fetch an arbitrary endpoint (escape hatch), returning parsed JSON."""
        return self.session.get_json(path if path.startswith("/") else f"/{path}")
