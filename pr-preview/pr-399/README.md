# AstraMeter project website

The public website for AstraMeter, built as a static site for GitHub Pages. It
has two parts:

1. **Landing page** (`index.html`) — what AstraMeter is, features, supported
   devices and power meters, installation options, and an FAQ.
2. **Config generator** (`generator.html`) — a beginner-friendly tool that
   generates an AstraMeter configuration: a Python `config.ini` (Home Assistant
   add-on / Docker / direct install) or an ESPHome YAML (run on an ESP32).

Everything runs in the browser. Nothing is uploaded; config generation happens
with plain JavaScript. The generator form is data-driven from `js/schema.js`,
and the landing page renders its supported-meter grid and feature list from the
same schema so the marketing copy can't drift from the real capabilities.

## Files

| File | Purpose |
|------|---------|
| `index.html` | Landing / marketing page. |
| `generator.html` | The config generator page (mounts the app). |
| `css/styles.css` | Styling for both pages (nav, hero, landing sections, footer, generator UI). |
| `js/site.js` | Shared site behaviour: mobile nav, scroll state, and landing-page feature/power-meter grids (from the schema). |
| `js/schema.js` | Single source of truth: every powermeter, field, and tuning option with beginner help text. Pure data. |
| `js/state.js` | State model + persistence helpers (defaults, `migrate`, sanitisation of untrusted restored input). Pure, no DOM. |
| `js/generate.js` | Pure functions that turn the app state into `config.ini` or ESPHome YAML. No DOM. |
| `js/app.js` | Renders the generator form from the schema, holds state, live preview, save/load. |
| `js/schema.test.mjs` | Structural validation of the schema (typo guard). |
| `js/state.test.mjs` | Tests for the state model + untrusted-input sanitisation. |
| `js/generate.test.mjs` | Node assertions for the generators. |

## Develop locally

It's static — serve it through any local server (ES modules need `http://`, not
`file://`):

```bash
cd web
python3 -m http.server 8000
# landing page:    http://localhost:8000/
# config generator: http://localhost:8000/generator.html
```

## Test

```bash
node web/js/schema.test.mjs     # validates the schema structure (typo guard)
node web/js/state.test.mjs      # state model + untrusted-input sanitisation
node web/js/generate.test.mjs   # asserts the generated config.ini / YAML
```

CI runs all three before deploying (see `.github/workflows/pages.yml` and
`pr-preview.yml`).

## Save / load

User progress is saved automatically to `localStorage`. Users can also:

- **Save project file** — download the current answers as `astrameter-project.json`.
- **Load project file** — restore from that JSON to keep iterating later.
- **Copy share link** — encode the whole state into a URL hash to share or bookmark.

## Deploying

The site is published to the **`gh-pages`** branch and served by GitHub Pages.

One-time setup: repository **Settings → Pages → Build and deployment → Source →
"Deploy from a branch" → `gh-pages` / `/ (root)`**, and **Settings → Actions →
General → Workflow permissions → "Read and write permissions"** (so the workflow
can push to `gh-pages`).

The *Deploy config generator to GitHub Pages* workflow (`.github/workflows/pages.yml`)
publishes `web/` on every push that touches it:

- **Production** — pushes to **`main`** publish to the site **root**:
  `https://<user>.github.io/<repo>/`
- **Staging** — pushes to **`develop`** publish under **`/develop/`**:
  `https://<user>.github.io/<repo>/develop/`
- **Per-PR previews** — the *Deploy PR preview* workflow
  (`.github/workflows/pr-preview.yml`) deploys each pull request to
  `pr-preview/pr-<number>/` and posts the live URL as a comment on the PR, so
  reviewers can test the real site before merging. The preview is removed when
  the PR closes. (Previews only run for same-repo branches; forks can't write
  to `gh-pages`.)

Root, `/develop/`, and `/pr-preview/` all coexist on the `gh-pages` branch
(`keep_files: true`), so a deploy to one never wipes the others. The site uses
only relative URLs, so it works correctly under any of these subpaths.

GitHub links in the site (footer docs, the per-meter reference link, and the
ESPHome `external_components` source in generated configs) are committed pointing
at `@develop`. At deploy time `.github/scripts/stamp-ref.sh` rewrites them to the
ref being deployed — so a `main` build links to `@main`, a PR preview links to
the PR's branch, and develop stays on `@develop`.

## Adding or editing a powermeter

Almost everything lives in **`js/schema.js`** — it's designed so common changes
are a one-file edit, and `js/schema.test.mjs` guards the structure.

**Edit an existing field** (label, help text, default, placeholder, dropdown
options): find the meter in `POWERMETERS` and change the field object. Done.

**Add a field to a meter**: add a `{ key, label, help, type, … }` object to that
meter's `fields` array. `key` is the `config.ini` key. Use `phase: true` if it
takes per-phase values; `advanced: true` to tuck it behind the disclosure.

**Add a whole new powermeter**: append an entry to `POWERMETERS`:

```js
{
  id: "mymeter",                 // unique
  label: "My Meter",
  section: "MYMETER",            // config.ini section (UPPER_SNAKE, unique)
  blurb: "One-line description shown under the dropdown.",
  docPython: "docs/powermeters.md#mymeter",
  fields: [
    { key: "IP", label: "IP address", type: "text", required: true, help: "…" },
  ],
  esphome: {                     // how it's read on an ESP32
    kind: "http",                // homeassistant | mqtt | sml | modbus | http | unsupported
    tier: "generic",             // native | generic | alternate | unsupported (badge)
    note: "What this does on the ESP.",
    url1: (f) => `http://${f.IP}/status`,
    lambda1: 'id(grid_l1).publish_state(root["power"]);',
  },
},
```

Then, if the meter can report three phases, add its `id` to `PHASE_CAPABLE`.

**The `esphome.kind` handlers are generic** — meter-specific behaviour is
declarative, so you rarely touch `js/generate.js`:

- per-meter ESP warning → `esphome.warn` (string, or `(f) => string|null`)
- HTTP request headers → `esphome.headersField: "FIELD_KEY"`
- a `homeassistant`-kind source that names its own entity → `esphome.haEntity: (f) => "sensor.x"`
- MQTT 3-phase key renames → top-level `phaseListKeys: { topic, jsonPath }`

You only edit `js/generate.js` when introducing a brand-new `esphome.kind`
(a new way of producing a sensor block) — then add a handler and extend
`ESP_KINDS` in `schema.test.mjs`.

After any change, run the tests and add an assertion to
`js/generate.test.mjs` for the new output.

## Keeping it in sync

The schema mirrors the options documented in the repo. The sources of truth are
`config.ini.example`, `esphome.example.yaml`, `docs/powermeters.md`,
`docs/esphome-powermeters.md`, and the **Configuration** section of the main
`README.md`.
