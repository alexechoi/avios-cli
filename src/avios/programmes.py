"""Avios programme presets.

Avios is one currency shared across several loyalty programmes (British Airways
Executive Club, Iberia Plus, ...). They are the *same* web app served from
``www.avios.com``; a programme is distinguished by the ``x-avios-opco`` header and
the Auth0 tenant you log in through. A user holds one account per programme.
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
}


def get_programme(slug: str) -> Programme:
    key = slug.lower()
    if key not in PROGRAMMES:
        raise KeyError(f"Unknown programme '{slug}'. Choose from: {', '.join(PROGRAMMES)}")
    return PROGRAMMES[key]


def programme_slugs() -> list[str]:
    return list(PROGRAMMES)
