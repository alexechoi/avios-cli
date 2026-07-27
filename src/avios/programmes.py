"""Avios programme presets.

Most Avios programmes use the shared ``www.avios.com`` app and are distinguished
by the ``x-avios-opco`` header. Finnair Plus is the exception: it has its own
CAS/OAuth login and loyalty API, represented by the ``finnair`` backend.
"""

from __future__ import annotations

from dataclasses import dataclass

AVIOS_BASE_URL = "https://www.avios.com"
DEFAULT_PROGRAMME = "ba"


@dataclass(frozen=True)
class Programme:
    """A loyalty programme preset."""

    slug: str  # short id, e.g. "ba", "iberia"
    name: str  # display name
    opco: str  # x-avios-opco header value (BAEC, IBP, ...)
    base_url: str = AVIOS_BASE_URL
    login_url: str = AVIOS_BASE_URL  # where the browser login starts
    backend: str = "avios"


PROGRAMMES: dict[str, Programme] = {
    "ba": Programme(
        slug="ba",
        name="British Airways",
        opco="BAEC",
        login_url=f"{AVIOS_BASE_URL}/manage-avios/dashboard",
    ),
    "iberia": Programme(
        slug="iberia",
        name="Iberia",
        opco="IBP",
        login_url="https://www.iberia-avios.com",
    ),
    "aerlingus": Programme(
        slug="aerlingus",
        name="Aer Lingus",
        opco="EI",
        login_url=AVIOS_BASE_URL,
    ),
    "finnair": Programme(
        slug="finnair",
        name="Finnair Plus",
        opco="FINNAIR",
        base_url="https://api.finnair.com",
        login_url="https://www.finnair.com/gb-en/my-finnair-plus/balance-and-transactions",
        backend="finnair",
    ),
}


def get_programme(slug: str) -> Programme:
    key = slug.lower()
    if key not in PROGRAMMES:
        raise KeyError(f"Unknown programme '{slug}'. Choose from: {', '.join(PROGRAMMES)}")
    return PROGRAMMES[key]


def programme_slugs() -> list[str]:
    return list(PROGRAMMES)
