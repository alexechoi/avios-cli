"""Tests for the account store and per-account sessions."""

from __future__ import annotations

import json
import stat

import httpx

from avios.accounts import Account, AccountStore
from avios.config import Settings
from avios.programmes import get_programme

BA_COOKIE = [{"name": "appSession", "value": "x", "domain": "www.avios.com"}]
IB_COOKIE = [{"name": "appSession", "value": "y", "domain": "www.avios.com"}]
EI_COOKIE = [{"name": "appSession", "value": "z", "domain": "www.avios.com"}]


def test_save_and_list_round_trip(settings: Settings) -> None:
    store = AccountStore(settings)
    store.save(Account.from_programme(get_programme("ba"), cookies=BA_COOKIE))
    store.save(Account.from_programme(get_programme("iberia"), cookies=IB_COOKIE))
    store.save(Account.from_programme(get_programme("aerlingus"), cookies=EI_COOKIE))

    assert {a.slug for a in store.list()} == {"aerlingus", "ba", "iberia"}
    ba = store.get("ba")
    assert ba is not None
    assert ba.opco == "BAEC"
    assert ba.base_url == "https://www.avios.com"
    assert ba.cookies == BA_COOKIE
    aerlingus = store.get("aerlingus")
    assert aerlingus is not None
    assert aerlingus.name == "Aer Lingus"
    assert aerlingus.opco == "EI"
    assert aerlingus.base_url == "https://www.avios.com"
    assert aerlingus.cookies == EI_COOKIE


def test_finnair_token_round_trip_and_client(settings: Settings) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["token"] = request.headers.get("oauth_token", "")
        seen["api_key"] = request.headers.get("x-api-key", "")
        seen["host"] = request.url.host
        return httpx.Response(200, json={"profile": {"awardPoints": 42}})

    programme = get_programme("finnair")
    account = Account.from_programme(programme)
    account.token = "finnair-token"
    account.api_key = "finnair-api-key"
    store = AccountStore(settings)
    store.save(account)

    loaded = store.get("finnair")
    assert loaded is not None
    assert loaded.backend == "finnair"
    assert loaded.token == "finnair-token"
    assert loaded.api_key == "finnair-api-key"
    client = loaded.client(settings, transport=httpx.MockTransport(handler))
    assert client.get_balance().balance == 42
    assert seen == {
        "token": "finnair-token",
        "api_key": "finnair-api-key",
        "host": "api.finnair.com",
    }


def test_account_file_is_private(settings: Settings) -> None:
    store = AccountStore(settings)
    store.save(Account.from_programme(get_programme("ba"), cookies=BA_COOKIE))
    mode = stat.S_IMODE((store.dir / "ba.json").stat().st_mode)
    assert mode == 0o600


def test_get_missing_and_remove(settings: Settings) -> None:
    store = AccountStore(settings)
    assert store.get("iberia") is None
    store.save(Account.from_programme(get_programme("iberia"), cookies=IB_COOKIE))
    assert store.remove("iberia") is True
    assert store.get("iberia") is None
    assert store.remove("iberia") is False


def test_migrates_legacy_state_to_ba(settings: Settings) -> None:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.state_path.write_text(json.dumps({"cookies": BA_COOKIE}))

    store = AccountStore(settings)  # migration runs in __init__
    ba = store.get("ba")
    assert ba is not None
    assert ba.opco == "BAEC"
    assert ba.cookies == BA_COOKIE


def test_no_migration_when_ba_exists(settings: Settings) -> None:
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    settings.state_path.write_text(json.dumps({"cookies": BA_COOKIE}))
    AccountStore(settings).save(Account.from_programme(get_programme("ba"), cookies=IB_COOKIE))

    # A second store must NOT overwrite the existing ba.json from the legacy file.
    assert AccountStore(settings).get("ba").cookies == IB_COOKIE  # type: ignore[union-attr]


def test_account_session_sends_opco_and_base_url(settings: Settings) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["opco"] = request.headers.get("x-avios-opco", "")
        seen["host"] = request.url.host
        return httpx.Response(200, json={"ok": True})

    account = Account.from_programme(get_programme("iberia"), cookies=IB_COOKIE)
    session = account.session(settings, transport=httpx.MockTransport(handler))

    assert session.get_json("/shell/api/users/current/accounts") == {"ok": True}
    assert seen["opco"] == "IBP"
    assert seen["host"] == "www.avios.com"
    assert session.opco == "IBP"
    assert session.base_url == "https://www.avios.com"


def test_aerlingus_session_sends_ei_opco(settings: Settings) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["opco"] = request.headers.get("x-avios-opco", "")
        seen["host"] = request.url.host
        return httpx.Response(200, json={"ok": True})

    account = Account.from_programme(get_programme("aerlingus"), cookies=EI_COOKIE)
    session = account.session(settings, transport=httpx.MockTransport(handler))

    assert session.get_json("/shell/api/users/current/accounts") == {"ok": True}
    assert seen == {"opco": "EI", "host": "www.avios.com"}
    assert session.opco == "EI"
