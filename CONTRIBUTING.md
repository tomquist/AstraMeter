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

## ESPHome external component (parity rule)

The `esphome/components/ct002/` directory is a C++ port of `src/astrameter/ct002/` and related modules. **Python is canonical, C++ is a mechanical mirror.** Filenames, class names, function names, and filter ordering all match Python so that a bug fix on one side maps to one file on the other.

When you fix a bug in:

- `src/astrameter/ct002/balancer.py` → also port the fix to `esphome/components/ct002/balancer.{h,cpp}` in the same PR.
- `src/astrameter/ct002/ct002.py` → `ct002.{h,cpp}` (including the response-builder math, MAC validation, and `_compute_smooth_target` dispatch).
- `src/astrameter/ct002/protocol.py` → `protocol.{h,cpp}`. Add a vector to `tests/components/ct002/fixtures/protocol_golden_vectors.json` if the behaviour change affects wire bytes; both the Python pytest and the host-gcc gtest will pick it up automatically once `_populate_wire_hex.py` is re-run.
- `src/astrameter/powermeter/wrappers/{hampel,smoothing,pid}.py` → the matching `{hampel,smoothing,pid}.{h,cpp}` in the component directory.

Fixes to `src/astrameter/powermeter/wrappers/{transform,throttling}.py` have **no** C++ counterpart — those wrappers are delegated to ESPHome's standard `sensor: filters:` (`offset:`, `multiply:`, `throttle:`) on the upstream sensor.

The host-gcc gtest suite (`uv run pytest tests/components/ct002/test_host_protocol.py`) is the C++-side guard against translation drift. It builds via CMake with FetchContent-fetched googletest, so all you need locally is `cmake` and a C++17 compiler. Add a gtest case for any new C++ behavior that doesn't map 1:1 to a Python file.

## Branches and pull requests

- Base feature work on **`develop`** and open PRs against **`develop`**.
- Releases are merged to **`main`** as appropriate for the project maintainer.

## Changelog

For user-visible changes, add or update the single bullet under **`## Next`** in [CHANGELOG.md](CHANGELOG.md) (see [AGENTS.md](AGENTS.md) — Changelog).
