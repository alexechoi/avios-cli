"""Pydantic models for avios.com API payloads.

Only :class:`Balance` has a fully-confirmed shape (observed in captured traffic).
The remaining payloads were redacted in the capture, so their models keep
``extra='allow'`` to preserve every field until concrete shapes are pinned from
live responses. This means nothing is silently dropped and the client can already
return typed objects today.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AviosModel(BaseModel):
    """Base model: camelCase-JSON aware, forward-compatible with unknown fields."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    def as_dict(self) -> dict[str, Any]:
        """Serialise back to the API's camelCase JSON shape."""
        return self.model_dump(by_alias=True)


class Balance(AviosModel):
    """Avios balance (from ``/en-GB/spend-avios/api/avios-balance``)."""

    balance: int
    household_avios_balance: int | None = Field(default=None, alias="householdAviosBalance")


class Transaction(AviosModel):
    """A single Avios statement entry.

    Concrete fields are pinned once live transaction payloads are observed; until
    then every key is preserved via ``extra='allow'``.
    """


class Account(AviosModel):
    """A linked loyalty account."""


class Profile(AviosModel):
    """The current user's profile."""


class Overview(AviosModel):
    """Dashboard overview summary."""
