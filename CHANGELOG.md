# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Finnair Plus integration using Finnair's CAS/OAuth browser login and loyalty
  API, including balance, profile and transaction mapping.
- Token-backed account storage alongside the existing avios.com cookie accounts.

## [0.2.1] - 2026-07-27

### Fixed
- An expired session made avios.com hang the request, surfacing as a confusing
  "read operation timed out". Request timeouts are now treated as an expired
  session with a clear "run `avios login` again" message.

## [0.2.0] - 2026-07-24

### Added
- **`transactions`, `pending` and `overview` now work.** The manage-avios API
  only needed an `x-avios-opco` header (not a separate session, as previously
  thought) — it's now sent automatically. `transactions` renders date /
  description / Avios (coloured +/-) / type and respects `--limit`; the TUI shows
  real transactions too.
- Typed `Transaction` model (identifier, dateProcessed, description, type,
  partner, amount, categories) and a `NamedRef` helper.
- `AVIOS_OPCO` setting (default `BAEC` = British Airways Executive Club) for other
  Avios programmes.

## [0.1.6] - 2026-07-24

### Fixed
- **`balance` finally works after login.** It (and `whoami`) were querying the
  `manage-avios`/`spend-avios` micro-app APIs, which need a separate per-app
  session and returned 401 ("Session expired") even when you were logged in.
  Probing a real session showed the balance is served by
  `/shell/api/users/current/accounts` (`{balance, individual, household}`) and the
  profile by `/auth-gateway/user` — both of which authenticate with the browser
  session. `balance`/`whoami` now use those.

### Changed
- `balance` shows total / individual / household; `whoami` shows name, tier,
  membership and email.
- The TUI shows your balance even if transactions are unavailable.
- `transactions`, `pending` and `overview` are marked **experimental** — they need
  the `manage-avios` app session, which the cookie/SSO login can't yet establish.
- Removed the `accounts` command (its endpoint is the balance source, now shown by
  `balance`).

## [0.1.5] - 2026-07-24

### Fixed
- **`--from-browser` only read Chrome's `Default` profile.** If you were logged
  into avios.com in another profile (Profile 1, a work profile, ...), it grabbed
  the wrong profile's cookies and every call failed with "Session expired". It now
  **scans all Chrome profiles, verifies which one actually authenticates**
  (against `/auth-gateway/user`), and uses that — and reports which profile it used.

### Added
- `avios login --from-browser --profile "Profile 1"` to target a specific browser
  profile, and a clear warning when the imported cookies aren't a logged-in session.

## [0.1.4] - 2026-07-24

### Fixed
- **Endless hCaptcha loop during `avios login`.** The login browser opened a fresh,
  empty context with automation flags on, which hCaptcha/Akamai flag as a bot and
  answer with infinite challenges. Login now opens a **persistent Chrome profile**
  (cookies/reputation persist between attempts) with the automation fingerprint
  disabled (`--disable-blink-features=AutomationControlled`, no `--enable-automation`).
  If you still hit captchas, use `avios login --from-browser` to import the cookie
  from your normal Chrome instead.

## [0.1.3] - 2026-07-24

### Fixed
- **Login never actually captured an authenticated session.** The dashboard is a
  client-side SPA, so `wait_for_url("**/manage-avios/**")` matched the initial
  navigation immediately and the browser closed before login — saving anonymous
  cookies, so every call then failed with "Session expired". Login now polls
  `/auth-gateway/user` until it returns 200 (i.e. you're actually logged in) and
  only then captures the cookies.
- Login launches your **system Chrome** (`channel="chrome"`) when available, so no
  150 MB Chromium download is needed; falls back to bundled Chromium.
- Corrected the Chromium install command to `uvx --from playwright playwright
  install chromium`.

## [0.1.2] - 2026-07-24

### Fixed
- Login error messages: `[login]` was swallowed by Rich markup (showed
  `avios-cli` instead of `avios-cli[login]`) — error text is now escaped.
- Login guidance is uvx-aware: suggests `uvx --from 'avios-cli[login]' avios login`,
  the one-time `playwright install chromium`, and the `--from-browser` alternative;
  a missing Chromium at launch now gives a clear install hint.

## [0.1.1] - 2026-07-24

### Fixed
- Added an `avios-cli` console-script alias so `uvx avios-cli` works (matching the
  package name). The primary command is still `avios`.

## [0.1.0] - 2026-07-24

### Added
- Project scaffolding: src-layout `avios` package, hatchling packaging, `avios`
  console script, ruff + mypy + pytest tooling, GitHub Actions CI (Python
  3.10–3.13).
- Config layer (`pydantic-settings`, XDG paths) and pydantic models.
- Session/cookie layer: `state.json` storage (mode `600`), `AVIOS_COOKIE`
  override, authenticated `httpx` client, expiry detection.
- Typed `AviosClient`: balance, transactions (+`--limit`), pending, accounts,
  profile, overview, and a `raw` escape hatch.
- Browser-assisted login: `avios login` (Playwright) and
  `avios login --from-browser` (browser-cookie3); `avios logout`.
- CLI commands with Rich rendering and `--json` output.
- Textual TUI dashboard (`avios tui`): gradient ASCII "AVIOS" banner + balance
  header + scrollable transactions table, non-blocking refresh.
- OSS project files: CONTRIBUTING, SECURITY, issue/PR templates, PyPI publish
  workflow, Dependabot.
- README screenshot of the TUI, generated by `scripts/screenshot.py`.

### Fixed
- TUI balance line rendered blank: `#balance` padding + border consumed its whole
  height (content height 0). Adjusted the box so the balance is visible.

### Noted
- Reward-flight **availability** search is not yet implemented (needs a British
  Airways capture).

[Unreleased]: https://github.com/alexechoi/avios-cli/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/alexechoi/avios-cli/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/alexechoi/avios-cli/compare/v0.1.6...v0.2.0
[0.1.6]: https://github.com/alexechoi/avios-cli/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/alexechoi/avios-cli/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/alexechoi/avios-cli/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/alexechoi/avios-cli/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/alexechoi/avios-cli/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/alexechoi/avios-cli/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/alexechoi/avios-cli/releases/tag/v0.1.0
