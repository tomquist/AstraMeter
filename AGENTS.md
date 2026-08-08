# Agent notes

Keep this file current: whenever a change makes anything documented here wrong or incomplete — the dev/test commands, the parity rules, the powermeter checklist, or any other guidance below — update `AGENTS.md` in the same change so the next agent inherits accurate notes.

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

## Home Assistant add-on image

`tests/test_addon_container.py` runs the built add-on image against a stand-in
Supervisor (`tests/_fake_supervisor.py`) — the only test that covers
`ha_addon/run.sh`, the venv path and `SUPERVISOR_TOKEN` reaching the app. It
skips unless the image exists, so build it first when touching the add-on's
container or launch path:

```bash
docker build -f ha_addon/Dockerfile -t astrameter-addon:test .
uv run pytest tests/test_addon_container.py
```

CI does the same in the `addon-container` job — the durable path, since a
sandboxed agent session may not be able to run these locally:

- **The daemon is usually not running.** Docker is installed in Claude Code's
  remote environment, but nothing starts it; `dockerd &` (as root) works, and
  it does not survive into the next session.
- **The build fails behind a TLS-intercepting proxy.** `apk add` in the
  builder stage cannot verify the proxy's certificate. Build with
  `--network host` and the proxy CA added to the image (the environment's
  proxy notes — `/root/.ccr/README.md` on Claude Code — describe how). Do this
  in a copy of the Dockerfile: the real one must stay proxy-free for CI.

When neither is possible, leave the container tests to CI and say so rather
than reporting the image as untested.

## Python ↔ ESPHome parity (REQUIRED)

`esphome/components/ct002/` is a C++ mirror of the Python CT002 stack. Any change to shared behavior must land on **both** sides in the same change. See `CONTRIBUTING.md` for the file mapping and what has no C++ counterpart. Verify with `uv run pytest tests/components/ct002/`.

Both halves of that suite need something the sandbox does not have by default,
and both are obtainable — don't skip them:

- **`test_shared_e2e.py` skips without the ESPHome CLI.** `uv tool install
  esphome` (~2 min) turns ~40 skips into real `[esphome]` runs, which is the
  half that actually proves parity.
- **`test_host_protocol.py` fails to build behind a TLS-intercepting proxy.**
  CMake fetches googletest as a GitHub tarball and the proxy answers 403. `git
  clone` is allowed, so clone it once and point FetchContent at it — no repo
  change needed:

  ```bash
  git clone --depth 1 --branch v1.14.0 https://github.com/google/googletest.git /tmp/googletest
  cmake -S tests/components/ct002 -B /tmp/ct002_build -DCMAKE_BUILD_TYPE=Release \
        -DFETCHCONTENT_SOURCE_DIR_GOOGLETEST=/tmp/googletest
  cmake --build /tmp/ct002_build -j && (cd /tmp/ct002_build && for t in host_*_test; do ./$t; done)
  ```

### Dashboard / web UI (one page, two backends)

**The same page** — `web/ts/dashboard/`, built into one self-contained HTML
file — is served by the Python stack and by the ESPHome component, so a UI
change lands on both at once and there is no second frontend to keep in sync.
What differs is the document behind it:

- The **status half has parity**: `src/astrameter/status/serialize.py` ↔
  `esphome/components/ct002/status_json.{h,cpp}` (the wire layer), and
  `CT002.status_snapshot` / `LoadBalancer.status_snapshot` ↔ their C++
  namesakes. A field added to one side belongs on the other, under the same
  name and unit. The firmware serves a genuinely **reduced** document, which
  the schema is built for: every field is optional at every level, the frontend
  renders only what it receives, and it must never substitute 0 or "—" for
  something absent. What it leaves out is what the page does not render —
  `balancer.config`, and the `integrations` entries with no card (cloud
  reporting, Marstek registration) — since those would be bytes on every poll
  that nothing reads. A field the page *does* render belongs on both sides.
- The **configuration half is permanently waived.** An ESPHome device's config
  is compiled into its firmware, so there is nothing for a dashboard to write.
  The page hides its Configuration tab when the backend reports no
  `config_mode`.
- The **write path has parity too**: `esphome/components/ct002/controls.{h,cpp}`
  mirrors `_CONTROL_RANGES` / `_CONTROL_SCALE` / `_coerce_control_value` in
  `web_server.py`, and `apply_consumer_control` / `apply_device_control` mirror
  its `_CONSUMER_SETTERS` table. The bounds MUST match: a value one stack
  accepts and the other refuses would be settable from one dashboard and then
  silently reverted by the next retained MQTT replay. It is opt-in on the
  firmware (`controls:`, default off) because that page has no login, and it
  requires `Content-Type: application/json` — a header a browser cannot set
  cross-origin without a preflight, which is what stops any page the owner
  happens to visit from POSTing to a device on their LAN.

  Three divergences there are deliberate, so don't "restore" them: the
  firmware **rejects a device write with no `value`** (except `force_rotation`,
  which is a button) where Python defaults it to `True` — defaulting would
  switch on `active_control` nobody asked for; it wants a **JSON number** where
  Python accepts anything `float()` swallows; and it **ignores `device_id`**,
  having exactly one device to write to.

Three things the ESPHome half constrains, so keep them true:

- The bundle stays inside the gzipped budget enforced by `npm run
  check:dashboard` — it lives in the ESP32's flash.
- The HTTP handler runs on the httpd task, **not** the main loop, so it must
  never walk live state. `dashboard.cpp` builds the document from `loop()` and
  hands writes to it, both across a mutex.
- The ESP-IDF HTTP shim only parses **form-encoded** POST bodies into request
  params. The page sends JSON, so `handle_control_` reads the body off the raw
  `httpd_req_t` itself (the shim leaves it unread). Keep it that way rather
  than inventing a second wire format for the firmware.

Browser-level tests live in `web/e2e/` (`cd web && npm run e2e`) and boot the
real stack. Anything touching the live DOM — the reconciler, a control's write
path, a disclosure — needs a test there, because the unit tests render views
to a string and cannot see those failures.

The firmware side is covered in four places, because **ESPHome has no web
server for the `host` platform** (`web_server` is declared ESP-only and
`web_server_base.h` falls back to `<ESPAsyncWebServer.h>` elsewhere), so
`dashboard.cpp` cannot be built or run there:

- `host_status_json_test.cpp` / `host_controls_test.cpp` — the wire format and
  the write-path bounds, as pure host gtests.
- `host_write_slot_test.cpp` — the httpd-task ↔ main-loop write handover, with
  real threads. This is why it lives in `write_slot.h` and not in
  `dashboard.cpp`: keep it free of ESPHome deps so both sides stay drivable
  from a host test, since a race here is otherwise untestable anywhere.
- `test_dashboard_e2e.py` — the document built from **live** state and the
  writes applied back into it, driven against the compiled host binary over
  the test-control channel (`status` / `control` commands in `test_hooks.cpp`,
  which is why `dashboard_state.cpp` compiles for test-hook builds too).
- The ESP32 compile matrix — everything HTTP.

`src/astrameter/static/dashboard.html` and
`esphome/components/ct002/dashboard_asset.h` (the same page gzipped, for the
ESP32's flash) are **committed generated artifacts** — neither the Docker build
nor `esphome compile` has Node. After touching anything under `web/`, run
`cd web && npm run build:dashboard` and commit **both**; CI fails on a stale one.

### Screenshots (docs + website)

`docs/images/dashboard-<tab>-<light|dark>.png` are **committed generated
artifacts** too — embedded by `docs/dashboard.md`, `README.md` and the landing
page (`web/build.mjs` copies them to `dist/assets/screenshots/`). Refresh them
with:

```bash
cd web && npm run screenshots     # ~8 minutes; takes all 8
```

`web/tools/screenshots.ts` boots the same stack the browser tests use — via a
`sim`/`configDir` override on `startStack` — against a bigger house (three
batteries, five appliances, solar) and drives a real browser. It is
deliberately patient: the trend lines are built in the browser from polls, so
it waits for real samples, and it waits for a moment when the grid is actually
at zero with every battery working before each shot. `--tabs`, `--themes`,
`--warmup` and `--out` narrow a re-run. There is **no CI check** for staleness —
the values are live, so every run differs and a diff would always be dirty;
refresh them when a UI change makes them wrong.

Two things to hold onto when refreshing them:

- **The captions claim the grid sits at zero, and the images have to earn
  it.** If a shot comes out tens of watts off, the scenario is wrong, not the
  caption — do not reword the caption to match a bad run. Two settings decide
  this: `base_noise` is re-rolled every read, so it is a hard floor under how
  close to zero the grid can be held, and `auto_interval` must stay longer
  than the loop takes to settle (~35 s mean, ~62 s p95 per the steering
  evaluation) or the house is never settled at all. `--settle` (default 30 W,
  a little wider than the balancer's own ±25 W settling band) is the guard
  that catches both.
- **`web/index.html` states each image's intrinsic `width`/`height`** to
  reserve layout space. The crop height follows the tab's content, so re-check
  those attributes after a refresh that changes a tab's height.

## Steering-quality evaluation (run when touching balancer behavior)

`uv run python -m astrameter.simulator.evaluation` simulates hours of
realistic household activity against the firmware-accurate battery plant and
reports reaction/oscillation/energy metrics per scenario. Each scenario is run
over several seeds (`--seeds`, default 5) **in parallel across CPU cores**, and
every metric is the mean over those seeds — so the figures are the
seed-averaged signal, not one noisy draw (use `--seeds 1` for a quick
single-seed run, and `--seed N` to set the starting seed — seeds run are
`N..N+seeds-1`). When changing `src/astrameter/ct002/balancer.py` (or anything
else in the active-control loop), capture a baseline first (`--json base.json`
on the unchanged code), re-run after the change, and compare with `--input
head.json --compare base.json`. CI runs the same suite on PR base + head (job
`steering-eval`) and posts the comparison as a sticky PR comment. The
comparison leads with an **aggregate roll-up** (per-metric mean across all
scenarios plus a one-line overall verdict — how many metrics
improved/regressed and the mean relative change), so an across-the-board
improvement or regression is visible without reading every scenario table. A
second **priority verdict** sits below it: a value-weighted score (`_METRIC_WEIGHTS`
— `cost_regret_ct` money north-star, import-heavy self-consumption energy,
do-no-harm overshoot/hunting guardrails, cycle-life battery travel and
`share_imbalance_w` inter-battery fairness) plus a hard
flag when any do-no-harm guardrail (`_GUARDRAIL_METRICS`: overshoot,
band-crossings, grid p2p, avoidable grid import, and cost regret) regresses past
5% (or appears from a zero base). Read the flat mean for "did most numbers move
down?" and the priority verdict for "did it improve *where it matters*, and did
it break a guardrail?".

The headline metric is **`cost_regret_ct`**: the controller's electricity bill
(eurocents, asymmetric tariff — import @ `RETAIL_CT_PER_KWH`, export @
`FEEDIN_CT_PER_KWH`) minus what a **perfect-foresight optimal battery** would
have paid on the same load (`_oracle_cost_ct`, a lossless greedy aggregate
battery — provably optimal under a flat tariff). It is the single ungameable
"money the controller left on the table" number — 0 means it matched the
optimum; the irreducible cost when the pack saturates is subtracted out, so
regret is purely controllable loss. **`grid_rms_w`** is the whole-run L2 tracking
error (control quality, transients included), pairing with
`battery_travel_w_per_h` as the effort term (the two LQR terms are kept separate,
not fused with an arbitrary weight).

## Branches and pull requests

Open pull requests against **`develop`**, never `main` (see `CONTRIBUTING.md`).
`main` takes release merges and maintainer hotfixes only — never agent work,
and the guard below exempts maintainers, so nothing will catch it for you.
Tooling offers `main` as the base, so set it yourself;
`.github/workflows/pr-base-guard.yml` retargets what slips through, at the cost
of a round-trip.

GitHub reads that workflow and `.github/pull_request_template.md` from the
**default branch**, so edits to either do nothing until they reach `main` at the
next release.

## Changelog

For user-facing work, contribute **exactly one bullet under `## Next`** that summarizes the **overall** outcome of *that change*. The unit is the **change (feature/fix), not the branch or PR**: a single change may span several branches or PRs, and they all **edit the same bullet** rather than each adding their own. `## Next` accumulates **one bullet per change**, so it normally holds **several** bullets at once (one for each change heading into the next release) — multiple bullets under `## Next` are correct and expected, never a violation. What you must not do is author **more than one** bullet for **your own** change, or consolidate/remove a bullet belonging to a *different* change. **Add** your bullet when you first document the change; on **later iterations** (more commits, or a follow-up PR for the same change), **edit that same bullet** if the scope or wording shifts—do **not** append extra bullets for each follow-up. Skip `CHANGELOG.md` entirely when nothing users would notice changes (refactors, tests-only, etc.).

Do **not** expand `CHANGELOG.md` with every internal or tooling-only follow-up. If the change's bullet already states the high-level theme, leave it unless the **user-visible** story changes.

Write each bullet for the **user**, not the implementer: the user-visible problem and outcome, not *how* it was fixed. **Keep it to one sentence of roughly 30 words.** Add a second sentence only when the user has to *do* something — set a new option, undo a workaround, adapt to a breaking change. Anything else gets cut: log excerpts, retellings of the symptom, why it happened, everything the change touched. Err on the side of too short — a bullet that reads as terse is right; a paragraph never is.

**No implementation details** — internal symbol/function/class/file names, config-knob mechanics, data structures, parity-mirror notes — unless the user genuinely needs them (a config option or env var *they* set).

**Link the bullet to its PR once the number is known** — append a `([#<pr>](https://github.com/tomquist/astrameter/pull/<pr>))` reference (alongside any issue links already cited) so the changelog points back to the change. The PR number usually isn't known when you first write the bullet, so add the link on the follow-up iteration after the PR exists. **Always do this as soon as you learn the PR number** (e.g. the moment a PR is opened for the branch, or a number is shared with you) — don't wait to be asked: add the reference and push it in your next commit.

## Config options (surface everywhere)

Any **user-facing config option** must be wired into **every** config surface, not just the loader — a setting that only one entry point understands is a bug. When you add or rename a `[SECTION]` key, update **all** of:

1. **Settings + loader** — add the field (with its default) to the matching dataclass in `src/astrameter/config/settings.py`, read the `[SECTION] KEY` for it in `src/astrameter/config/ini_config.py` (powermeter keys: `src/astrameter/config/config_loader.py`), and use it where it belongs (e.g. `run_device` in `main.py`). Config **backends** answer the `AppConfig` interface — nothing outside `ini_config.py` / `config_loader.py` should know section or key names.
2. **`config.ini.example`** — a commented example with a short rationale.
3. **Web config editor** — register typed keys in `SECTION_KEY_TYPES` in `src/astrameter/web_config.py`.
4. **Web config generator (ALWAYS)** — add the field to the matching group in `web/ts/schema.ts` (e.g. a `CT_*` group or a `POWERMETERS` entry), emit it from `web/ts/generate.ts` for **every** target it applies to (`config.ini`, the Home Assistant add-on options, and ESPHome **only if** it has an ESPHome counterpart — Python-only options carry no `ey` key and must be excluded from the `ct002:` block), surface it in `web/ts/app.ts`, and add `web/ts/generate.test.ts` assertions. Run `cd web && npm run check`.
5. **Home Assistant add-on** — add the option + schema to `ha_addon/config.yaml`, map it onto the settings field in `src/astrameter/config/addon.py` (the `--addon` backend reads the add-on options directly — usually one entry in `_CT_FIELDS` / `_SOURCE_SIGNAL_FIELDS` / `_GENERAL_FIELDS`) with a test in `addon_test.py`, and describe it in `ha_addon/translations/en.yaml`. `ha_addon/run.sh` only launches the app — nothing to change there. `addon_schema_test.py` fails until the option is wired up (and `tests/test_addon_golden_settings.py` until it appears in the golden fixture), so an option that does nothing cannot ship.
6. **Dashboard guided form** — an add-on option also needs a label, a `help` sentence and a group in `OPTION_META` (`web/ts/dashboard/option-meta.ts`), plus a `placeholder` when leaving it empty means something worth stating. Groups are declared in `GROUPS` in the same file; anything but the two open ones is folded shut. `dashboard.test.ts` reads `ha_addon/config.yaml` and fails on an option with no entry or no help — and on an entry for an option that no longer exists.
7. **Docs** — the relevant `docs/*.md` (and `README.md` if it belongs in the quick reference).

The web config generator is **not optional** — a new option that the generator can't produce is incomplete.

## Adding a powermeter

Powermeters are Python-only and have **no** C++/ESPHome counterpart (the ESPHome
component reads grid power from any native ESPHome sensor instead), so the
parity rule above does not apply here. A new powermeter still touches several
places beyond the implementation — work through **every** step below so the
config loader, web editor, config generator, and both doc sets stay in sync
(grep an existing meter, e.g. `HomeWizard`/`HOMEWIZARD`, to find all the spots):

1. **Implementation** — Add `src/astrameter/powermeter/<module>.py` with a class subclassing `Powermeter`; implement `get_powermeter_watts()` (and `wait_for_message()` only if the base default is wrong for your source).
2. **Exports** — Import and re-export the class from `src/astrameter/powermeter/__init__.py` (both the import and `__all__`).
3. **Config loader** — In `src/astrameter/config/config_loader.py`: import the class, define a `*_SECTION` string, add a `section.startswith(...)` branch in `create_powermeter()`, and a `create_*_powermeter()` factory that reads options from the section. `POWER_OFFSET` / `POWER_MULTIPLIER`, `THROTTLE_INTERVAL`, and `NETMASK` are handled globally for any section that returns a powermeter — no extra wiring unless you need something custom.
4. **Web config editor** — Register the section's typed keys in `SECTION_KEY_TYPES` in `src/astrameter/web_config.py` (use the `_pm(...)` helper, adding only the non-default field types, e.g. `password`/`boolean`/`integer`).
5. **Web config generator** — Add a `POWERMETERS` entry in `web/ts/schema.ts` (fields, `docPython`, and an `esphome` spec describing how the same source is read on an ESP32 — `kind`/`tier` plus any `haEntity`/`url1`/`lambda1`/`warn`). Run `cd web && npm run check`.
6. **ESPHome docs** — Even though there's no C++ port, document how to read the *same source* on an ESP32 in `docs/esphome-powermeters.md`: a tier section (🟢 native / 🔵 generic HTTP / 🟠 alternate via HA/Modbus/MQTT / 🔴 not yet) **and** its entry in the Contents legend. Keep it consistent with the generator's `esphome` spec from step 5.
7. **Examples, Python docs & changelog** — Add a commented example to `config.ini.example`, a subsection **and** Contents entry in `docs/powermeters.md`, the meter to the supported-source list in `README.md`, plus one **`## Next`** `CHANGELOG.md` bullet (add once, then update that bullet on follow-up iterations if needed—see **Changelog** above).
8. **Tests** — Add `src/astrameter/powermeter/<module>_test.py` and a `create_*_powermeter` factory test in `src/astrameter/config/config_loader_test.py`; run the commands above (and `cd web && npm run check`) before finishing.
