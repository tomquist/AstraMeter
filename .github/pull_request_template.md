## Why

<!-- The problem this solves, from a user's point of view. The diff already
     shows what changed; explain what made it worth changing. -->

## Checklist

- [ ] Base branch is `develop`, not `main`
- [ ] `uv run ruff format . && uv run ruff check . && uv run mypy src/ && uv run pytest`
- [ ] Python ↔ ESPHome parity held for shared CT002 behaviour, or does not apply ([CONTRIBUTING.md](https://github.com/tomquist/AstraMeter/blob/develop/CONTRIBUTING.md))
- [ ] `web/` changes: rebuilt the dashboard bundle (`cd web && npm run build:dashboard`) and committed it
- [ ] User-visible change: one bullet under `## Next` in [CHANGELOG.md](https://github.com/tomquist/AstraMeter/blob/develop/CHANGELOG.md)
