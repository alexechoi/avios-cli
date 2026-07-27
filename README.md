# avios-cli

[![CI](https://github.com/alexechoi/avios-cli/actions/workflows/ci.yml/badge.svg)](https://github.com/alexechoi/avios-cli/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)

A **CLI and TUI for your Avios programmes** — check balances and browse transactions
without leaving the terminal. British Airways and Iberia use avios.com; Finnair Plus
uses Finnair's own OAuth and loyalty API.

> ⚠️ **Unofficial.** This project is not affiliated with, authorised by, or endorsed by
> Avios, British Airways, Finnair or IAG Loyalty. It drives the same private endpoints
> the providers' websites use, with your own logged-in session. Use at your own risk;
> it may break at any time and may be against the provider's terms of service.

![avios TUI dashboard](docs/dashboard.svg)

<sub>The <code>avios tui</code> dashboard (demo data). Regenerate with <code>uv run python scripts/screenshot.py</code>.</sub>

## Status

Early alpha, built in the open. See the [roadmap](#roadmap).

## Install

Requires Python 3.10+.

```bash
uvx avios-cli --help     # run without installing
# or install it, then use the `avios` command:
pip install avios-cli
avios --help
```

Or from source, for development:

```bash
git clone https://github.com/alexechoi/avios-cli
cd avios-cli
uv sync
uv run avios --help
```

## Log in

avios.com has no credential API — login is Auth0 Universal Login behind hCaptcha and
SMS/passkey MFA — so `avios login` opens a real browser, **waits while you finish
logging in** (password, captcha, SMS code), and captures the session once you land
on the dashboard. It needs the **`login` extra**:

```bash
uvx --from 'avios-cli[login]' avios login
uvx --from 'avios-cli[login]' avios login finnair
```

It launches your installed **Google Chrome** (no extra download). If you don't have
Chrome, install Playwright's browser once with
`uvx --from playwright playwright install chromium`.

Prefer not to open a browser? Import the cookie from a Chrome you're already logged
into avios.com with:

```bash
uvx --from 'avios-cli[login]' avios login --from-browser
```

This scans **all your Chrome profiles** and uses whichever one is actually logged
into avios.com (it tells you which). If you have several profiles, you can target
one directly:

```bash
uvx --from 'avios-cli[login]' avios login --from-browser --profile "Profile 1"
```

Plain `uvx avios-cli login` won't work — the isolated environment doesn't include
the `login` extra.

**Stuck in an endless captcha loop?** That's bot detection on the automated
browser. Use `--from-browser` instead: log into avios.com in your normal Chrome
(you'll get one normal captcha), then run
`uvx --from 'avios-cli[login]' avios login --from-browser`.

Finnair uses a different CAS/OAuth flow. `avios login finnair` opens the Finnair Plus
balance page, lets you complete password and MFA in the real browser, and captures the
OAuth session from the authenticated loyalty request. `--from-browser` is not available
for Finnair because browser-cookie import cannot read that token.

Each programme session is stored at `~/.config/avios/accounts/<programme>.json`
(mode `600`). Sessions expire; just log in to that programme again. Use
`avios logout <programme>` for one account or `avios logout` for all accounts.

## Usage

Commands span **all logged-in accounts** by default and show a combined total; add
`--account/-a <programme>` to focus on one.

```bash
avios accounts                 # every logged-in account: balance + status
avios balance                  # per-account balances + combined total
avios transactions --limit 20  # recent transactions, merged across accounts
avios pending                  # pending Avios, merged across accounts
avios balance --account iberia # just one programme
avios overview                 # dashboard summary
avios whoami                   # name, tier, membership, email
avios raw /shell/api/users/current/accounts   # hit any endpoint directly
```

Add `--json` to `accounts`, `balance`, `transactions`, `pending` or `whoami` for
scriptable output. With a single account, `balance` keeps the individual/household
breakdown.

## TUI

```bash
avios tui
```

A full-screen dashboard across all your accounts: a combined balance header (with a
per-programme breakdown) and a scrollable transactions table with a Programme column,
merged newest-first. Press `r` to refresh, `q` to quit.

## Roadmap

- [x] Project scaffolding, packaging and CI
- [x] Session + cookie storage layer
- [x] Typed API client (balance, transactions, accounts, profile)
- [x] Browser-assisted `avios login`
- [x] CLI commands
- [x] Textual TUI dashboard
- [x] Multiple accounts (BA, Iberia, Aer Lingus, Finnair) with combined views
- [ ] Reward-flight **availability** search _(coming soon — needs a British Airways capture)_

## Development

```bash
uv sync            # installs the project + the `dev` dependency-group
uv run ruff check .
uv run mypy src
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow and
[CHANGELOG.md](CHANGELOG.md) for release notes.

## Security

No passwords are handled or stored — only programme session cookies or OAuth tokens,
kept locally under `~/.config/avios/accounts/` (mode `600`). See
[SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
