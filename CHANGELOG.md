# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres
to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-08-11

### Fixed
- **BA reward-flight search returned nothing after British Airways rebuilt the
  flight finder.** The new Next.js app moved each day's flights one level deeper
  (`departureJourneys.<date>.journeys[].flights[]` instead of
  `departureJourneys.<date>.flights[]`), so every day parsed as empty. Availability
  parsing now follows the new shape, and the old flat shape still parses if BA
  rolls back.
- **Business-class seats were always reported as zero.** BA's availability rows use
  cabin code `J`; the client only looked for `C`. Both are now recognised, matching
  the site's own `cabinClassCodeMap`.
- The request contract was re-derived from a live capture and is now reproduced
  byte for byte — query parameters, both `next-router-state-tree` variants, `rsc`,
  `next-url`, `accept`, and the full browser header set. A user-agent that
  disagreed with the `sec-ch-ua` client hints (Chrome 126 vs 148) is fixed, since
  the mismatch is itself a bot signal.

### Added
- **Avios prices.** Searching an exact `--date` now calls the finder's
  `fetchPricingAction` server action and shows the Avios cost per cabin for your
  party, in both the CLI table and the TUI. Calendar (month) searches stay
  seats-only so they remain one request per leg.
- Exact-date searches also re-read that date through
  `getFlightResultsForSingleJourneyAction`, exactly as the website does when you
  click a day, so seat counts are fresh rather than served from the month cache.
- Seats that only sell against a BA companion voucher (Business `rbd: I`) are
  marked `†` instead of being counted as general availability.
- `AviosClient.search_reward_legs()` searches every leg of a trip over **one**
  browser session, and reports failures per leg so one bad leg does not lose the
  other. The CLI and TUI both use it.
- Akamai denials are now distinguished from an expired session: a bot-manager 403
  or a 429 raises `RewardSearchBlockedError` telling you to wait and switch
  network, rather than sending you to log in again.

### Changed
- **Reward searches make far fewer requests.** Previously each leg launched its own
  Chrome, navigated to the finder, and drove the search form. A search now warms
  one browser navigation and issues in-page `fetch()` calls for everything after
  it — a return trip costs one navigation and two fetches — with pacing between
  requests. British Airways blocks by IP address, and page loads were what
  triggered it.
- One availability request now returns the whole 13-month booking window, so an
  out-of-range month reports the window BA actually offers.
- Dates beyond BA's rolling booking horizon are recognised (`availabilityLevel: 3`)
  and no longer render as "no reward seats".
- `flights --json` reports days as `journeys[].flights[]`, following the upstream
  shape, and includes the `price` attached to each cabin.

## [0.4.3] - 2026-07-27

### Changed
- **`uvx avios-cli login` now works with no extra flags.** Browser-assisted login
  (Playwright + browser-cookie3) moved from the optional `[login]` extra into the
  base package, so `uvx avios-cli login iberia` just works — no more
  `uvx --from 'avios-cli[login]' …`. The `login` extra is kept as a no-op alias
  for backward compatibility.
- If you have no system Chrome, `avios login` now **downloads Playwright's Chromium
  automatically** on first use (~150 MB, one-time) instead of failing with an
  install hint.

## [0.4.2] - 2026-07-27

### Fixed
- **BA reward-flight searches failed with 403 after a successful login.** The
  flight finder has a separate Auth0 session and rejects plain HTTP or automated
  headless requests at Akamai. BA login now waits for the second reward-flight
  prompt, and searches drive the site's own form in a background Chrome window
  before parsing the returned RSC availability.
- Stored Akamai cookies are no longer replayed over Chrome's fresher browser jar,
  and a transient Next.js shell response is retried once.

## [0.4.1] - 2026-07-27

### Fixed
- **Finnair balance/transactions failed with 403 immediately after login.** Login
  captured the API key from the first `api.finnair.com` request (`getgauth`), which
  uses a *different* `x-api-key` than the loyalty balance/transactions endpoints —
  so every data call sent the wrong key and was refused. Login now captures
  credentials only from a loyalty API request (`/d/loyalty-service/legacy/current/api/`),
  which carries the key those endpoints accept. Fixes #28.
- A Finnair `403 Forbidden` is no longer mislabelled "session expired"; the message
  now names the real cause (likely a stale/wrong API key) and includes the response.
  Fixes #29.

## [0.4.0] - 2026-07-27

### Added
- **British Airways reward-flight availability.** `avios flights ORIGIN DEST`
  supports exact-date and month-calendar searches, cabin/passenger filters,
  available-only output by default, `--show-unavailable`, and scriptable JSON.
- Return journeys run two independent one-way searches with the route reversed
  for the inbound leg.
- Typed reward calendar, day, flight, and cabin-availability models plus an
  isolated parser for avios.com's authenticated React Server Components stream.
- The Textual app now has Dashboard and Reward Flights tabs. The search tab keeps
  network work off the event loop and renders outbound/inbound results separately.

### Changed
- BA browser-assisted login warms the separate `spend-avios` flight-search app
  before saving cookies.

### Security
- Reward-search action tokens from the private RSC response are ignored and never
  stored; tests use a small sanitized fixture rather than the captured HAR.

## [0.3.0] - 2026-07-27

### Added
- **Multiple accounts, one per programme.** Log into several Avios programmes and
  keep their sessions side by side: `avios login [programme]` (default `ba`),
  `avios logout [programme]` (or `avios logout` for all), and a new `avios accounts`
  roster showing each balance and status. Programmes: British Airways (`ba`),
  Iberia (`iberia`), Aer Lingus (`aerlingus`), Finnair Plus (`finnair`).
- **Combined views.** `balance` sums every account into a combined total (a single
  account keeps the individual/household detail); `transactions`/`pending` merge
  across accounts newest-first with a Programme column. `--account/-a` filters any
  data command to one programme.
- **Multi-account TUI.** `avios tui` shows a combined balance header (per-programme
  breakdown) and a Programme column, loading every account concurrently.
- **Finnair Plus** via Finnair's CAS/OAuth browser login + loyalty API (balance,
  profile, transactions); token-backed account storage alongside cookie accounts.
- Programme-aware browser login (per-programme login URL + isolated Chrome profile)
  and a shared, backend-aware aggregation layer (`aggregate.py`) used by the CLI
  and TUI. Per-account failures are captured, so one expired session never hides
  the others.

### Changed
- Sessions are now stored per programme at `~/.config/avios/accounts/<programme>.json`
  (mode `600`); the old single `state.json` is migrated to `accounts/ba.json` on
  first run.

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

[Unreleased]: https://github.com/alexechoi/avios-cli/compare/v0.4.3...HEAD
[0.4.3]: https://github.com/alexechoi/avios-cli/compare/v0.4.2...v0.4.3
[0.4.2]: https://github.com/alexechoi/avios-cli/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/alexechoi/avios-cli/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/alexechoi/avios-cli/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/alexechoi/avios-cli/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/alexechoi/avios-cli/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/alexechoi/avios-cli/compare/v0.1.6...v0.2.0
[0.1.6]: https://github.com/alexechoi/avios-cli/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/alexechoi/avios-cli/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/alexechoi/avios-cli/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/alexechoi/avios-cli/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/alexechoi/avios-cli/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/alexechoi/avios-cli/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/alexechoi/avios-cli/releases/tag/v0.1.0
