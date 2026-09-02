---
name: powermeter
description: The eight places adding a grid powermeter touches — implementation, exports, config loader, web editor, web generator, ESPHome docs, Python docs, tests. Use when adding or renaming a powermeter backend under src/astrameter/powermeter/.
---

# Adding a powermeter

Powermeters are Python-only and have **no** C++/ESPHome counterpart — the
ESPHome component reads grid power from any native ESPHome sensor instead — so
the parity rule doesn't apply here. A new meter still touches several places
beyond the implementation; grep an existing one (`HomeWizard` / `HOMEWIZARD`) to
find every spot, and work through all eight so the config loader, web editor,
generator and both doc sets stay in sync.

1. **Implementation** — `powermeter/<module>.py` with a class subclassing
   `Powermeter`, implementing `get_powermeter_watts()`; override
   `wait_for_message()` only if the base default is wrong for your source. A
   meter polled over HTTP should subclass `HttpPowermeter`
   (`powermeter/http_client.py`) instead — it owns the session lifecycle and a
   `get_json(url)` that fails fast and raises on an HTTP error, so a backend
   only builds URLs and decodes bodies.
2. **Exports** — the import and `__all__` entry in `powermeter/__init__.py`.
3. **Config loader** — in `config/config_loader.py`: import the class, define a
   `*_SECTION` string, add a `create_*_powermeter()` factory reading the
   section's options, and register the pair in `_FACTORIES` (matched
   longest-prefix-first, so ordering is not a hazard). Pass an optional key
   through `_declared(...)` so the class's own default applies when the key is
   unset, rather than restating it. `POWER_OFFSET`, `POWER_MULTIPLIER`,
   `THROTTLE_INTERVAL` and `NETMASK` are handled globally for any section that
   returns a powermeter.
4. **Web config editor** — the section's typed keys in `SECTION_KEY_TYPES`
   (`src/astrameter/web_config.py`) via the `_pm(...)` helper, listing only the
   non-default types (`password`, `boolean`, `integer`).
5. **Web config generator** — a `POWERMETERS` entry in `web/ts/schema.ts`:
   fields, `docPython`, and an `esphome` spec describing how the same source is
   read on an ESP32 (`kind` / `tier` plus any `haEntity`, `url1`, `lambda1`,
   `warn`). Run `cd web && npm run check`.
6. **ESPHome docs** — even without a C++ port, document how to read the *same
   source* on an ESP32 in `docs/esphome-powermeters.md`: a tier section (🟢
   native / 🔵 generic HTTP / 🟠 alternate via HA, Modbus or MQTT / 🔴 not yet)
   **and** its entry in the Contents legend, consistent with step 5's `esphome`
   spec.
7. **Examples, docs and changelog** — a commented example in
   `config.ini.example`, a subsection **and** Contents entry in
   `docs/powermeters.md`, the meter in `README.md`'s supported-source list, and
   one `## Next` bullet in `CHANGELOG.md`.
8. **Tests** — `powermeter/<module>_test.py` and a `create_*_powermeter` factory
   test in `config/config_loader_test.py`.
