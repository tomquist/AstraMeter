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

## Python ↔ ESPHome parity (REQUIRED)

`esphome/components/ct002/` is a C++ mirror of the Python CT002 stack. Any change to shared behavior must land on **both** sides in the same change. See `CONTRIBUTING.md` for the file mapping and what has no C++ counterpart. Verify with `uv run pytest tests/components/ct002/`.

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
do-no-harm overshoot/hunting guardrails, cycle-life battery travel) plus a hard
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

## Changelog

For user-facing work, contribute **exactly one bullet under `## Next`** that summarizes the **overall** outcome of *that change*. The unit is the **change (feature/fix), not the branch or PR**: a single change may span several branches or PRs, and they all **edit the same bullet** rather than each adding their own. `## Next` accumulates **one bullet per change**, so it normally holds **several** bullets at once (one for each change heading into the next release) — multiple bullets under `## Next` are correct and expected, never a violation. What you must not do is author **more than one** bullet for **your own** change, or consolidate/remove a bullet belonging to a *different* change. **Add** your bullet when you first document the change; on **later iterations** (more commits, or a follow-up PR for the same change), **edit that same bullet** if the scope or wording shifts—do **not** append extra bullets for each follow-up. Skip `CHANGELOG.md` entirely when nothing users would notice changes (refactors, tests-only, etc.).

Do **not** expand `CHANGELOG.md` with every internal or tooling-only follow-up. If the change's bullet already states the high-level theme, leave it unless the **user-visible** story changes.

Write each bullet for the **user**, not the implementer: describe what changed for them and why it matters, and keep it **compact and clear**. Leave out implementation details — internal symbol/function/class/file names, config knob mechanics, data structures, parity-mirror notes, and the like — unless a user genuinely needs them (e.g. a config option or env var they set). Prefer one tight sentence over an exhaustive list of everything touched.

**Link the bullet to its PR once the number is known** — append a `([#<pr>](https://github.com/tomquist/astrameter/pull/<pr>))` reference (alongside any issue links already cited) so the changelog points back to the change. The PR number usually isn't known when you first write the bullet, so add the link on the follow-up iteration after the PR exists.

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
