"""Finnair Plus authentication and API client.

Finnair does not use the avios.com cookie-backed API. Its website signs in
through Finnair CAS/OAuth and sends the resulting access token in an
``oauth_token`` header to ``api.finnair.com``. Balance/profile and transactions
are selected by different JSON request bodies sent to the same legacy endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from avios.config import Settings, get_settings
from avios.models import Balance, Overview, Profile, Transaction
from avios.session import NotAuthenticated, SessionExpired

FINNAIR_API_BASE_URL = "https://api.finnair.com"
FINNAIR_LOGIN_URL = "https://www.finnair.com/gb-en/my-finnair-plus/balance-and-transactions"
FINNAIR_PROFILE_PATH = "/d/loyalty-service/legacy/current/api/profile"
# The loyalty (balance/transactions) endpoints live under this prefix and use a
# DIFFERENT x-api-key than sibling endpoints such as member-service `getgauth`.
# Credentials must be captured from a request under this prefix, or the balance
# call fails with 403 Forbidden (wrong API key).
FINNAIR_LOYALTY_API_PREFIX = "/d/loyalty-service/legacy/current/api/"

PROFILE_REQUEST: dict[str, Any] = {"profileRequest": {"type": "BASIC", "cache": "USE"}}
TRANSACTIONS_REQUEST: dict[str, Any] = {"transactionsRequest": {}}

LOGIN_TIMEOUT_MS = 300_000
LOGIN_POLL_MS = 250


@dataclass(frozen=True)
class FinnairCredentials:
    """Values captured from an authenticated Finnair loyalty API request."""

    token: str
    api_key: str


class FinnairSession:
    """Token-authenticated HTTP session for the Finnair loyalty API."""

    def __init__(
        self,
        token: str,
        api_key: str,
        settings: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.token = token
        self.api_key = api_key
        self.settings = settings or get_settings()
        self._transport = transport

    def client(self) -> httpx.Client:
        if not self.token or not self.api_key:
            raise NotAuthenticated("No saved Finnair session. Run `avios login finnair` first.")
        return httpx.Client(
            base_url=FINNAIR_API_BASE_URL,
            headers={
                "accept": "application/json, text/plain, */*",
                "content-type": "application/json",
                "oauth_token": self.token,
                "origin": "https://www.finnair.com",
                "referer": FINNAIR_LOGIN_URL,
                "user-agent": self.settings.user_agent,
                "x-api-key": self.api_key,
            },
            timeout=self.settings.request_timeout,
            follow_redirects=False,
            transport=self._transport,
        )

    def post_json(self, path: str, payload: dict[str, Any]) -> Any:
        """POST JSON and translate an invalid OAuth token into SessionExpired."""
        try:
            with self.client() as client:
                response = client.post(path, json=payload)
        except httpx.TimeoutException as exc:
            raise SessionExpired(
                "Finnair request timed out. Run `avios login finnair` again."
            ) from exc
        if response.status_code in (301, 302, 401):
            raise SessionExpired("Finnair session expired. Run `avios login finnair` again.")
        if response.status_code == 403:
            # Not an expired session: usually the wrong API key was captured at login
            # (the member-service key instead of the loyalty key). Surface it plainly.
            raise SessionExpired(
                "Finnair refused the request (403 Forbidden) — the saved API key looks "
                "wrong; re-run `avios login finnair`. "
                f"Response: {response.text[:100]}"
            )
        response.raise_for_status()
        return response.json()


def _points(value: Any) -> int | None:
    """Parse Finnair's signed string point amounts."""
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    cleaned = value.replace(",", "").replace(" ", "").strip()
    try:
        return int(cleaned)
    except ValueError:
        return None


def _transaction(payload: dict[str, Any]) -> Transaction:
    """Map a Finnair transaction while preserving every original field."""
    mapped = dict(payload)
    mapped.update(
        {
            "identifier": payload.get("id") or payload.get("transactionId"),
            "dateProcessed": payload.get("date"),
            "description": (
                payload.get("description")
                or payload.get("productName")
                or payload.get("partnerName")
            ),
            "type": {
                "value": payload.get("transactionSubType")
                or payload.get("productType")
                or payload.get("status"),
                "id": payload.get("transactionId"),
            },
            "partner": {
                "value": payload.get("partnerName"),
                "id": payload.get("partnerType"),
            },
            "amount": _points(payload.get("awardPoints")),
            "categories": [
                value
                for value in (payload.get("productType"), payload.get("partnerType"))
                if isinstance(value, str) and value
            ],
        }
    )
    return Transaction.model_validate(mapped)


class FinnairClient:
    """Typed access to the balance, profile, and transactions in Finnair Plus."""

    def __init__(self, session: FinnairSession) -> None:
        self.session = session

    def _profile_payload(self) -> dict[str, Any]:
        payload = self.session.post_json(FINNAIR_PROFILE_PATH, PROFILE_REQUEST)
        profile = payload.get("profile", {}) if isinstance(payload, dict) else {}
        return profile if isinstance(profile, dict) else {}

    def _transaction_payloads(self) -> list[dict[str, Any]]:
        payload = self.session.post_json(FINNAIR_PROFILE_PATH, TRANSACTIONS_REQUEST)
        wrapper = payload.get("transactions", {}) if isinstance(payload, dict) else {}
        items = wrapper.get("transactions", []) if isinstance(wrapper, dict) else []
        return [item for item in items if isinstance(item, dict)]

    def get_balance(self) -> Balance:
        profile = self._profile_payload()
        balance = _points(profile.get("awardPoints"))
        if balance is None:
            raise ValueError("Finnair profile response did not contain an Avios balance")
        return Balance(balance=balance, individual=balance)

    def get_profile(self) -> Profile:
        profile = self._profile_payload()
        first = str(profile.get("firstname") or "")
        last = str(profile.get("lastname") or "")
        token_content = {
            "name": f"{first} {last}".strip(),
            "email": profile.get("email"),
            "https://avios.com/customer_tier_name": (
                profile.get("tier") or profile.get("cardTier")
            ),
            "https://avios.com/membership_id": profile.get("memberNumber"),
        }
        return Profile.model_validate({"tokenContent": token_content, "finnairProfile": profile})

    def get_overview(self) -> Overview:
        return Overview.model_validate({"profile": self._profile_payload()})

    def get_transactions(self, limit: int = 50) -> list[Transaction]:
        transactions = [_transaction(item) for item in self._transaction_payloads()]
        return transactions[:limit] if limit and limit > 0 else transactions

    def get_pending_transactions(self) -> list[Transaction]:
        return [
            _transaction(item)
            for item in self._transaction_payloads()
            if str(item.get("status") or "").lower() not in {"processed", "completed"}
        ]

    def raw(self, path: str) -> Any:
        normalised = path if path.startswith("/") else f"/{path}"
        return self.session.post_json(normalised, {})


def _loyalty_credentials(url: str, headers: dict[str, str]) -> FinnairCredentials | None:
    """Return credentials iff this is a *loyalty* API request carrying both auth headers.

    Only the loyalty endpoints (balance/transactions) carry the API key the client
    needs. Sibling endpoints like member-service ``getgauth`` use a different
    x-api-key, so capturing from them yields a key that 403s on the balance call.
    """
    if not url.startswith(FINNAIR_API_BASE_URL + FINNAIR_LOYALTY_API_PREFIX):
        return None
    token = headers.get("oauth_token")
    api_key = headers.get("x-api-key")
    if token and api_key:
        return FinnairCredentials(token=str(token), api_key=str(api_key))
    return None


def login_finnair_via_browser(
    *,
    settings: Settings | None = None,
    headless: bool = False,
    timeout_ms: int = LOGIN_TIMEOUT_MS,
    playwright_factory: Any = None,
) -> FinnairCredentials:
    """Capture the OAuth token and API key from a loyalty API request.

    A real browser completes password and MFA. Capturing the request header is
    more robust than reading page storage because it verifies that both values
    have reached the loyalty API in the exact form required by later calls. Only
    requests under the loyalty API prefix are used, so we get the API key the
    balance/transactions endpoints accept (see :func:`_loyalty_credentials`).
    """
    from avios.auth import LoginError, _import_sync_playwright, _open_login_context

    settings = settings or get_settings()
    factory = playwright_factory or _import_sync_playwright()
    profile_dir = str(settings.config_dir / "chrome-profile" / "finnair")
    captured: list[FinnairCredentials] = []

    with factory() as pw:
        ctx = _open_login_context(pw, headless=headless, user_data_dir=profile_dir)
        page = ctx.new_page()

        def capture(request: Any) -> None:
            credentials = _loyalty_credentials(str(request.url), request.headers)
            if credentials is not None and credentials not in captured:
                captured.append(credentials)

        page.on("request", capture)
        page.goto(FINNAIR_LOGIN_URL)
        for _ in range(max(1, timeout_ms // LOGIN_POLL_MS)):
            if captured:
                break
            page.wait_for_timeout(LOGIN_POLL_MS)
        ctx.close()

    if not captured:
        raise LoginError("Timed out waiting for Finnair login. Run `avios login finnair` again.")
    return captured[0]
