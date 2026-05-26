# Agent notes

Resolved versions live in **`uv.lock`**. Install dev dependencies the same way CI does:

```bash
uv sync --extra dev
```

Before finishing Python changes, run (from repo root, with dev deps):

```bash
uv run ruff format .
uv run ruff check .
uv run mypy src/
uv run pytest
```

CI runs the same steps (see `.github/workflows/ci.yml`).

## Python ↔ ESPHome parity (REQUIRED)

The `esphome/components/ct002/` directory is a C++ port of the CT002 stack
that runs the emulator natively on an ESP32. **Python is canonical; the C++
is a mechanical mirror** (same file / function / field names, same ordering).

**Any change that affects shared behavior MUST be made on BOTH sides in the
same change** — a bug fix, protocol tweak, balancer adjustment, MQTT/discovery
field, or Marstek-cloud call done only in Python (or only in C++) is
incomplete. Before finishing, ask: "does an equivalent file exist on the
other side?" If yes, port the change.

File mapping (see `CONTRIBUTING.md` for the full table):

| Python (canonical) | C++ mirror (`esphome/components/ct002/`) |
| --- | --- |
| `src/astrameter/ct002/protocol.py` | `protocol.{h,cpp}` |
| `src/astrameter/ct002/balancer.py` | `balancer.{h,cpp}` |
| `src/astrameter/ct002/ct002.py` | `ct002.{h,cpp}` |
| `src/astrameter/powermeter/wrappers/{hampel,smoothing,pid}.py` | `{hampel,smoothing,pid}.{h,cpp}` |
| `src/astrameter/mqtt_insights/service.py` | `mqtt_insights.{h,cpp}` |
| `src/astrameter/mqtt_insights/discovery.py` | `ha_discovery.{h,cpp}` |
| `src/astrameter/mqtt_insights/marstek_mqtt.py` | `marstek_responder.{h,cpp}` |
| `src/astrameter/marstek_api.py` | `marstek_registration.{h,cpp}` |

**No C++ counterpart** (do NOT port): the `transform`/`throttling` wrappers
(delegated to ESPHome's per-sensor `filters:` — `offset`/`multiply`/`throttle`),
the Shelly emulator/discovery path, the ARP lookup, and Python's asyncio
queue / aiomqtt reconnect loop (ESPHome owns reconnect).

**Verify the C++ side** (needs `cmake` + a C++17 compiler; install esphome
with `uv tool install esphome` for the compile/e2e checks):

```bash
uv run pytest tests/components/ct002/test_host_protocol.py   # host-gcc gtests (parity guard)
uv run pytest tests/components/ct002/test_host_e2e.py        # BatterySimulator → host binary
esphome compile tests/components/ct002/test.host.yaml        # host-platform build
```

The host-gcc gtest suites (protocol / wrappers / balancer / marstek_responder)
are the C++-side regression guard against drift — add a case there for any
new C++ behavior. Note that the response-builder and the HA discovery/state
JSON have **no** host test yet, so log/JSON-shape parity for those still
relies on manual review against the Python source.

## Changelog

For user-facing work on a branch, keep **one bullet under `## Next`** that summarizes the **overall** outcome of that branch. **Add** it when you first document the change; on **later iterations** on the same branch, **edit that same bullet** if the scope or wording shifts—do **not** append extra bullets for each follow-up. Skip `CHANGELOG.md` entirely when nothing users would notice changes (refactors, tests-only, etc.).

Do **not** expand `CHANGELOG.md` with every internal or tooling-only follow-up. If the branch bullet already states the high-level theme, leave it unless the **user-visible** story changes.

## Adding a powermeter

Powermeters are Python-only and have **no** ESPHome counterpart (the ESPHome
component reads grid power from any native ESPHome sensor instead), so the
parity rule above does not apply here.

1. **Implementation** — Add `src/astrameter/powermeter/<module>.py` with a class subclassing `Powermeter`; implement `get_powermeter_watts()` (and `wait_for_message()` only if the base default is wrong for your source).
2. **Exports** — Import and re-export the class from `src/astrameter/powermeter/__init__.py`.
3. **Config** — In `src/astrameter/config/config_loader.py`: import the class, define a `*_SECTION` string, add a `section.startswith(...)` branch in `create_powermeter()`, and a `create_*_powermeter()` factory that reads options from the section. `POWER_OFFSET` / `POWER_MULTIPLIER`, `THROTTLE_INTERVAL`, and `NETMASK` are handled globally for any section that returns a powermeter — no extra wiring unless you need something custom.
4. **Examples, docs & changelog** — Add a commented example to `config.ini.example` and a subsection under **Configuration** in `README.md`, plus one **`## Next`** bullet for the powermeter (add once, then update that bullet on follow-up iterations if needed—see **Changelog** above).
5. **Tests** — Add `src/astrameter/powermeter/<module>_test.py` (or extend existing tests) and run the commands above before finishing.
