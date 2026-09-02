---
name: config-option
description: The seven surfaces a user-facing config option must reach — settings, config.ini.example, web editor, web generator, Home Assistant add-on, dashboard guided form, docs — and how to test the add-on container image. Use when adding or renaming a [SECTION] key, or when changing anything under ha_addon/.
---

# Config options reach every surface

An option only one entry point understands is a bug. Adding or renaming a
`[SECTION]` key means all seven:

1. **Settings** — field + default on the matching dataclass in
   `config/settings.py`. `ini_config.py` then reads it by itself: the INI key is
   the field name upper-cased unless `_GENERAL_KEY_OVERRIDES` says otherwise,
   and the getter follows the field's declared type. Powermeter keys are read
   explicitly in `config_loader.py`. Use the field where it belongs (e.g.
   `run_device` in `main.py`). Only `ini_config.py` and `config_loader.py` may
   know section or key names — every other backend answers the `AppConfig`
   interface.
2. **`config.ini.example`** — a commented example with a short rationale.
3. **Web config editor** — the typed key in `SECTION_KEY_TYPES`
   (`src/astrameter/web_config.py`).
4. **Web config generator** — the field in its `web/ts/schema.ts` group (a
   `CT_*` group or a `POWERMETERS` entry), emitted from `generate.ts` for every
   target it applies to: `config.ini`, the add-on options, and ESPHome **only
   if** it has an ESPHome counterpart — a Python-only option carries no `ey` key
   and must stay out of the `ct002:` block. Surface it in `app.ts` and assert it
   in `generate.test.ts`. Run `cd web && npm run check`. This step is **not
   optional**: an option the generator can't produce is incomplete.
5. **Home Assistant add-on** — the option + schema in `ha_addon/config.yaml`,
   mapped onto the settings field in `config/addon.py` (usually just the field
   name appended to `_CT_FIELDS` / `_SOURCE_SIGNAL_FIELDS` / `_GENERAL_FIELDS`;
   an `_OPTION_NAMES` entry only when the option isn't named after the field), a
   description in `ha_addon/translations/en.yaml`, and a test in
   `addon_test.py`. `ha_addon/run.sh` only launches the app — nothing to change
   there.
6. **Dashboard guided form** — a label, a `help` sentence and a group in
   `OPTION_META` (`web/ts/dashboard/option-meta.ts`), plus a `placeholder` when
   leaving it empty means something worth stating. Groups are declared in
   `GROUPS` in the same file; anything but the two open ones is folded shut.
7. **Docs** — the relevant `docs/*.md`, and `README.md` if it belongs in the
   quick reference.

`addon_schema_test.py`, `tests/test_addon_golden_settings.py` and
`dashboard.test.ts` fail until steps 5 and 6 are done — `dashboard.test.ts` also
fails on an entry for an option that no longer exists — so an option that does
nothing cannot ship.

## Verifying the add-on container

Only `tests/test_addon_container.py` covers `ha_addon/run.sh`, the venv path and
`SUPERVISOR_TOKEN` reaching the app. It skips unless the image exists, so build
it first when touching the add-on's container or launch path:

```bash
docker build -f ha_addon/Dockerfile -t astrameter-addon:test .
uv run pytest tests/test_addon_container.py
```

CI does the same in the `addon-container` job — the durable path, since a
sandbox may not manage it: Docker is installed but nothing starts it (`dockerd
&` as root works, and doesn't survive the session), and behind a
TLS-intercepting proxy `apk add` can't verify the certificate, so the build
needs `--network host` and the proxy CA added in a *copy* of the Dockerfile —
the real one stays proxy-free for CI. When neither is possible, leave the
container tests to CI and say so rather than reporting the image as untested.
