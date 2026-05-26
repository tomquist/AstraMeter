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
- `src/astrameter/mqtt_insights/marstek_mqtt.py` → `esphome/components/ct002/marstek_responder.{h,cpp}` (sub-block under `ct002:`). Wire-format changes (topic templates, `cd=1`/`cd=4` payload tokens, k=v ordering) must keep host-gcc `host_marstek_responder_test` green so the Marstek app and hm2mqtt-style parsers see identical bytes from both stacks.
- `src/astrameter/mqtt_insights/discovery.py` → `esphome/components/ct002/ha_discovery.{h,cpp}`. Keep `node_id`/`unique_id`/`value_template` strings identical so HA dedupe across the Python and ESPHome paths works correctly when both happen to share a broker.
- `src/astrameter/mqtt_insights/service.py` → `esphome/components/ct002/mqtt_insights.{h,cpp}`. The ESPHome port intentionally omits the asyncio queue, the reconnect loop, and the ARP lookup — see the header for the documented architectural diff. The whole file is gated by `#ifdef USE_MQTT` so it's a no-op on builds without `mqtt:` configured.
- `src/astrameter/marstek_api.py` → `esphome/components/ct002/marstek_registration.{h,cpp}`. Keep the URL paths (`/app/Solar/v2_get_device.php`, `/ems/api/v1/getDeviceList`, `/app/Solar/v2_add_device.php`), the User-Agent (`Dart/2.19 (dart:io)`), the password MD5 hashing, and the `02b250` managed-MAC prefix in lockstep — the cloud API responses depend on a specific payload shape. The ESPHome port's only architectural change is running the Python helper's linear flow as a state machine in `loop()` so the watchdog stays fed between HTTPS calls. Gated by `#ifdef USE_CT002_MARSTEK_REGISTRATION` (defined from `_to_code_marstek_registration` in ct002/__init__.py).

Fixes to `src/astrameter/powermeter/wrappers/{transform,throttling}.py` have **no** C++ counterpart — those wrappers are delegated to ESPHome's standard `sensor: filters:` (`offset:`, `multiply:`, `throttle:`) on the upstream sensor.

The host-gcc gtest suite (`uv run pytest tests/components/ct002/test_host_protocol.py`) is the C++-side guard against translation drift. It builds via CMake with FetchContent-fetched googletest, so all you need locally is `cmake` and a C++17 compiler. Add a gtest case for any new C++ behavior that doesn't map 1:1 to a Python file.

`tests/components/ct002/test_host_e2e.py` drives the compiled host binary over real UDP. Besides the BatterySimulator round-trip, it builds a second "test-hooks" binary (`test.e2e.host.yaml`) that compiles in a UDP control channel — enabled only by the test-only `test_control_port:` option, which adds the `USE_CT002_TEST_HOOKS` define (see `test_hooks.cpp`). The channel lets the harness inject grid power and drive a mock clock so time-gated behaviour (dedup, saturation, eviction) is deterministic against the black-box binary. `test_control_port:` is **test-only** — never set it in a real config. This is the foundation for an eventual shared Python↔ESPHome e2e suite.

## Branches and pull requests

- Base feature work on **`develop`** and open PRs against **`develop`**.
- Releases are merged to **`main`** as appropriate for the project maintainer.

## Changelog

For user-visible changes, add or update the single bullet under **`## Next`** in [CHANGELOG.md](CHANGELOG.md) (see [AGENTS.md](AGENTS.md) — Changelog).
