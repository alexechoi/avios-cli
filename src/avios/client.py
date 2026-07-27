"""High-level, typed client for the avios.com internal API.

:class:`AviosClient` wraps a :class:`~avios.session.Session` and returns pydantic
models. Session/expiry errors from the session layer propagate unchanged so the
CLI/TUI can render a single, friendly "please log in again" message.
"""

from __future__ import annotations

from typing import Any

from avios import endpoints
from avios.models import Balance, Overview, Profile, Transaction
from avios.rewards import RewardCalendar, RewardSearchQuery, search_reward_calendar
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
        # /shell/api/users/current/accounts authenticates with the SSO session and
        # returns {balance, individual, household}.
        return Balance.model_validate(self.session.get_json(endpoints.ACCOUNTS))

    def get_profile(self) -> Profile:
        # /auth-gateway/user returns the SSO user (idToken + tokenContent claims).
        return Profile.model_validate(self.session.get_json(endpoints.AUTH_USER))

    def get_overview(self) -> Overview:
        return Overview.model_validate(self.session.get_json(endpoints.OVERVIEW))

    def get_transactions(self, limit: int = 50) -> list[Transaction]:
        # `offset` is a server-side page hint; slice to `limit` for an exact count.
        offset = limit if limit and limit > 0 else 1000
        path = f"{endpoints.TRANSACTIONS}?startRecord=1&offset={offset}&status=completed"
        payload = self.session.get_json(path)
        items = (
            payload.get("transactions", []) if isinstance(payload, dict) else _extract_list(payload)
        )
        transactions = [Transaction.model_validate(item) for item in items]
        return transactions[:limit] if limit and limit > 0 else transactions

    def get_pending_transactions(self) -> list[Transaction]:
        payload = self.session.get_json(endpoints.TRANSACTIONS_PENDING)
        return [Transaction.model_validate(item) for item in _extract_list(payload)]

    def search_reward_calendar(self, query: RewardSearchQuery) -> RewardCalendar:
        """Search one month of direct BA reward-flight availability."""
        return search_reward_calendar(self.session, query)

    def raw(self, path: str) -> Any:
        """Fetch an arbitrary endpoint (escape hatch), returning parsed JSON."""
        return self.session.get_json(path if path.startswith("/") else f"/{path}")
