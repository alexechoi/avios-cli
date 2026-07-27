# Security Policy

## How avios-cli handles your credentials

- **No passwords are ever handled or stored.** Login happens in a real browser
  (the programme's normal login and MFA); avios-cli only captures the resulting
  **session cookie or OAuth token**.
- Sessions are stored locally under `~/.config/avios/accounts/` with file mode
  `600` (owner read/write only). Each credential is sent only to its programme's
  API in the requests you make.
- `avios logout` deletes stored sessions. They also expire on the server side.
- `AVIOS_COOKIE` may be set to supply a cookie via the environment instead of the
  stored file — avoid committing it or storing it in shell history.

## Unofficial software

avios-cli is **not affiliated with Avios, British Airways, Finnair or IAG
Loyalty** and uses private, undocumented endpoints. Using it may breach the
provider's terms of service and could affect your account. Use at your own risk.

## Reporting a vulnerability

Please **do not** open a public issue for security problems. Instead, use GitHub's
private vulnerability reporting:
**Security → Report a vulnerability** on the repository, or open a private draft
advisory. We'll acknowledge within a few days.

## Supported versions

This is pre-1.0 software; only the latest release on `main` is supported.
