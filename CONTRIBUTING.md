# Contributing to avios-cli

Thanks for your interest! avios-cli is a small, unofficial project — contributions
of all sizes are welcome.

## Dev setup

Requires Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/alexechoi/avios-cli
cd avios-cli
uv sync                       # installs the project + the `dev` dependency-group
uv run pre-commit install     # optional: auto-lint on commit
```

For the login flow (Playwright / browser-cookie3):

```bash
uv sync --extra login
uv run playwright install chromium
```

## Checks (run before pushing)

```bash
uv run ruff check .
uv run ruff format .
uv run mypy src
uv run pytest
```

CI runs the same checks on Python 3.10–3.13; all must pass.

## Pull requests

- **Keep PRs small** — under ~1000 lines of diff, and keep individual files under
  1000 lines. Split larger work into a sequence of focused PRs.
- Write a clear title and description, and include the test commands you ran.
- Add or update tests for behaviour changes. Network/browser calls are mocked —
  see `tests/` for patterns (`httpx.MockTransport`, faked `browser_cookie3`,
  Textual `Pilot`).
- Commits use a light [Conventional Commits](https://www.conventionalcommits.org/)
  style (`feat:`, `fix:`, `docs:`, `chore:`, `test:`, `ci:`).
- PRs are merged with a **merge commit** (history is not squashed).

## Releasing (maintainers)

Releases publish to [PyPI](https://pypi.org/project/avios-cli/) via
`.github/workflows/publish.yml` using **Trusted Publishing (OIDC)** — no API token.

One-time setup on PyPI (Account → Publishing → *Add a pending publisher*):

| Field | Value |
| --- | --- |
| PyPI Project Name | `avios-cli` |
| Owner | `alexechoi` |
| Repository name | `avios-cli` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

To cut a release:

1. Bump `__version__` in `src/avios/__init__.py` and update `CHANGELOG.md`.
2. Merge to `main`.
3. Create a GitHub Release with tag `vX.Y.Z` — the workflow builds and publishes.
   (Or run the **Publish** workflow manually via *workflow_dispatch*.)

## Reverse-engineering note

avios-cli talks to avios.com's private, undocumented endpoints. Shapes can change
without notice. If you observe a new/changed response, please pin the model in
`models.py` and add a fixture under `tests/fixtures/`. See [SECURITY.md](SECURITY.md)
for how credentials are handled.
