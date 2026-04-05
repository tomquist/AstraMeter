# Contributing

Thanks for helping improve astrameter. This document covers local development; for end-user install options see [README.md](README.md).

## Prerequisites

- **Python** 3.10 or newer (3.10–3.13 are tested in CI)
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** for dependencies and virtualenvs

## Dev setup

From the repository root:

```bash
uv sync --extra dev
```

This creates `.venv`, installs runtime and dev dependencies, and installs the project in editable mode so `astrameter` imports resolve without `PYTHONPATH`.

## Project layout

Application code lives under **`src/astrameter/`** (src layout). Notable pieces:

| Path | Role |
|------|------|
| `src/astrameter/main.py` | CLI entry and device orchestration |
| `src/astrameter/config/` | INI loading, powermeter factories |
| `src/astrameter/powermeter/` | Powermeter backends |
| `src/astrameter/ct002/` | CT002/CT003 UDP emulator |
| `src/astrameter/shelly/` | Shelly protocol emulation |
| `tests/` | Integration-style tests |

Co-located tests use `*_test.py` next to modules under `src/astrameter/`.

## Checks to run before pushing

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src/
uv run pytest
```

CI runs the same (ruff format check, ruff check, mypy on `src/`, pytest with coverage on supported Python versions).

## Adding a powermeter

Follow the checklist in [AGENTS.md](AGENTS.md) (**Adding a powermeter**), using paths under `src/astrameter/` (e.g. `src/astrameter/powermeter/<module>.py`, `src/astrameter/config/config_loader.py`).

## Branches and pull requests

- Base feature work on **`develop`** and open PRs against **`develop`**.
- Releases are merged to **`main`** as appropriate for the project maintainer.

## Changelog

For user-visible changes, add or update the single bullet under **`## Next`** in [CHANGELOG.md](CHANGELOG.md) (see [AGENTS.md](AGENTS.md) — Changelog).
