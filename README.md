# avios-cli

A **CLI and TUI for [avios.com](https://www.avios.com)** — check your Avios balance,
browse your transactions and manage your account without leaving the terminal.

> ⚠️ **Unofficial.** This project is not affiliated with, authorised by, or endorsed by
> Avios, British Airways or IAG Loyalty. It drives the same private endpoints the
> avios.com website uses, with your own logged-in session. Use at your own risk; it may
> break at any time and may be against the provider's terms of service.

## Status

Early alpha, built in the open. See the [roadmap](#roadmap).

## Install

Requires Python 3.10+. Until the first PyPI release, install from source with
[`uv`](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/alexechoi/avios-cli
cd avios-cli
uv sync
uv run avios --help
```

## Usage

```bash
avios --version
avios --help
```

Account commands (`login`, `balance`, `transactions`, `tui`, ...) land in subsequent
releases — see the roadmap below.

## Roadmap

- [x] Project scaffolding, packaging and CI
- [ ] Session + cookie storage layer
- [ ] Typed API client (balance, transactions, accounts, profile)
- [ ] Browser-assisted `avios login`
- [ ] CLI commands
- [ ] Textual TUI dashboard
- [ ] Reward-flight **availability** search _(coming soon — needs a British Airways capture)_

## Development

```bash
uv sync            # installs the project + the `dev` dependency-group
uv run ruff check .
uv run mypy src
uv run pytest
```

## License

[MIT](LICENSE)
