<!-- Thanks for contributing! Keep PRs small (< ~1000 lines) and focused. -->

## What & why

<!-- What does this change and why? Link any related issue. -->

## Test plan

```
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

<!-- Note any manual verification (e.g. `avios login`, `avios tui`). -->

## Checklist

- [ ] Tests added/updated (network/browser mocked)
- [ ] `ruff`, `mypy`, `pytest` pass locally
- [ ] Files stay under 1000 lines; diff under ~1000 lines
- [ ] CHANGELOG updated if user-facing
