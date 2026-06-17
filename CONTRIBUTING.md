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
- `src/astrameter/mqtt_insights/discovery.py` → `esphome/components/ct002/ha_discovery.{h,cpp}`. Keep `node_id`/`unique_id`/`value_template` strings identical so HA dedupe across the Python and ESPHome paths works correctly when both happen to share a broker. **Exception:** the top-level **AstraMeter hub device** (`build_addon_device_discovery`, the retained `{base}/bridge` state, and the `via_device` links on the meter devices) is **Python-only**. The hub is published whenever HA discovery is on — identified by `ADDON_SLUG` on the Supervisor add-on, or a base-topic fallback (`MqttInsightsService._hub_identifier`) in standalone/Docker — and groups the per-meter devices under it. It has no ESPHome equivalent, so the ESPHome CT002 device stands alone with no `via_device`. **Exception:** the per-powermeter **Online** diagnostic device (`build_powermeter_device_discovery` and the retained `{base}/powermeter/<section>` state) is **Python-only** — powermeters have no ESPHome counterpart (the ESPHome component reads grid power from a native sensor), so there is nothing to mirror.
- `src/astrameter/mqtt_insights/service.py` → `esphome/components/ct002/mqtt_insights.{h,cpp}`. The ESPHome port intentionally omits the asyncio queue and the reconnect loop — see the header for the documented architectural diff. The whole file is gated by `#ifdef USE_MQTT` so it's a no-op on builds without `mqtt:` configured. **Exception:** the powermeter health loop (`_powermeter_health_loop` and the `stream_online()` hooks it reads) is **Python-only**, since it tracks Python powermeter backends that the firmware doesn't have.
- `src/astrameter/marstek_api.py` → `esphome/components/ct002/marstek_registration.{h,cpp}`. Keep the URL paths (`/app/Solar/v2_get_device.php`, `/ems/api/v1/getDeviceList`, `/app/Solar/v2_add_device.php`), the User-Agent (`Dart/2.19 (dart:io)`), the password MD5 hashing, and the `02b250` managed-MAC prefix in lockstep — the cloud API responses depend on a specific payload shape. The ESPHome port's only architectural change is running the Python helper's linear flow as a state machine in `loop()` so the watchdog stays fed between HTTPS calls. Gated by `#ifdef USE_CT002_MARSTEK_REGISTRATION` (defined from `_to_code_marstek_registration` in ct002/__init__.py).

Fixes to `src/astrameter/powermeter/wrappers/{transform,throttling}.py` have **no** C++ counterpart — those wrappers are delegated to ESPHome's standard `sensor: filters:` (`offset:`, `multiply:`, `throttle:`) on the upstream sensor. `src/astrameter/powermeter/wrappers/health.py` (the outermost `HealthTrackingPowermeter` feeding the MQTT Insights Online sensor) likewise has **no** C++ counterpart — it tracks Python powermeter reads, which the firmware doesn't have.

The host-gcc gtest suite (`uv run pytest tests/components/ct002/test_host_protocol.py`) is the C++-side guard against translation drift. It builds via CMake with FetchContent-fetched googletest, so all you need locally is `cmake` and a C++17 compiler. Add a gtest case for any new C++ behavior that doesn't map 1:1 to a Python file.

Two host e2e modules drive the compiled binary over real UDP:

- `test_host_e2e.py` — the `BatterySimulator` round-trip against `test.host.yaml`, validating the real client path.
- `test_shared_e2e.py` — **differential** scenarios written once and parametrized over two backends with a common `poll / set_grid / set_clock / advance_clock` interface: `python` (the canonical `CT002` driven in-process via `_handle_request` + a fake transport) and `esphome` (the host binary). Asserting the same wire facts on both is the cross-stack parity guard. The `python` parametrizations need no ESPHome toolchain; the `esphome` ones skip without it.

The `esphome` backend uses a "test-hooks" binary (`test.e2e.host.yaml`) that compiles in a UDP control channel — enabled only by the test-only `test_control_port:` option, which adds the `USE_CT002_TEST_HOOKS` define (see `test_hooks.cpp`). The channel injects grid power and drives a mock clock so time-gated behaviour (dedup, saturation, eviction) is deterministic against the black-box binary. `test_control_port:` is **test-only** — never set it in a real config. When you add a shared scenario, write it against the `backend` fixture so it runs on both stacks.

## Branches and pull requests

- Base feature work on **`develop`** and open PRs against **`develop`**.
- Releases are merged to **`main`** as appropriate for the project maintainer.

## Changelog

For user-visible changes, add or update **the bullet for your change** under **`## Next`** in [CHANGELOG.md](CHANGELOG.md). The unit is the **change, not the branch or PR** — a change that spans several branches or PRs edits the *same* bullet rather than adding one each. `## Next` accumulates **one bullet per change** (so it normally holds several at once); add yours once, edit it on later iterations, and never consolidate or remove a bullet belonging to a *different* change (see [AGENTS.md](AGENTS.md) — Changelog).
