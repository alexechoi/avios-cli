"""avios.com internal API endpoint paths (relative to ``Settings.base_url``).

These are the JSON endpoints the avios.com web app calls. They are cookie-
authenticated and unofficial — see the README disclaimer.
"""

from __future__ import annotations

# Confirmed lightweight balance endpoint used by the "spend Avios" area.
BALANCE = "/en-GB/spend-avios/api/avios-balance"
# Balance under the manage-avios API — kept as a fallback.
BALANCE_ALT = "/manage-avios/api/user/current/balance"

PROFILE = "/manage-avios/api/user/current"
OVERVIEW = "/manage-avios/api/user/current/dashboard-overview"
TRANSACTIONS = "/manage-avios/api/user/current/transactions"
TRANSACTIONS_PENDING = "/manage-avios/api/user/current/transactions/pending"
ACCOUNTS = "/shell/api/users/current/accounts"
