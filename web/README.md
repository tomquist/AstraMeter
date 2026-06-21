# AstraMeter project website

The public website for AstraMeter, a static site (TypeScript + esbuild) for
GitHub Pages. It has two parts:

1. **Landing page** (`index.html`) — what AstraMeter is, features, supported
   devices and power meters, installation options, and an FAQ.
2. **Config generator** (`generator.html`) — a beginner-friendly tool that
   generates an AstraMeter configuration: a Python `config.ini` (Home Assistant
   add-on / Docker / direct install) or an ESPHome YAML (run on an ESP32).

Everything runs in the browser — nothing is uploaded. The generator form is
data-driven from `ts/schema.ts`, and the landing page renders its supported-meter
grid and feature list from the same schema, so the marketing copy can't drift
from the real capabilities.

## Stack

TypeScript sources in `ts/` are bundled with **esbuild** into `dist/` (the
publishable site), type-checked with **tsc**, and tested with **tsx**. The
GitHub ref the site links to is injected at build time (see *Deploying*).

## Files

| File | Purpose |
|------|---------|
| `index.html`, `generator.html` | Landing page and generator page (static shells). |
| `css/styles.css` | Styling for both pages. |
| `assets/` | Logo (SVG + PNG), favicon, og:image. |
| `CNAME` | Custom domain (`astrameter.com`) published to the `gh-pages` root by the build. |
| `robots.txt` | Keeps `/develop/` and `/pr-preview/` out of search indexes. |
| `ts/schema.ts` | Single source of truth: every powermeter, field, and tuning option, fully typed. Pure data. |
| `ts/links.ts` | Builds every GitHub URL from the build-injected ref (`__GH_REF__`). |
| `ts/state.ts` | State model + persistence helpers (defaults, `migrate`, sanitisation of untrusted restored input). Pure, no DOM. |
| `ts/generate.ts` | Pure functions that turn the app state into `config.ini` or ESPHome YAML. No DOM. |
| `ts/app.ts` | Renders the generator form from the schema; state, live preview, save/load. Entry point → `dist/js/app.js`. |
| `ts/site.ts` | Shared site behaviour: mobile nav, scroll state, `data-gh` link resolution, landing-page grids. Entry point → `dist/js/site.js`. |
| `ts/schema.test.ts` | Structural validation of the schema (typo guard). |
| `ts/state.test.ts` | Tests for the state model + untrusted-input sanitisation. |
| `ts/generate.test.ts` | Assertions for the generators. |
| `build.mjs` | esbuild build: copies static files + bundles the entry points into `dist/`. |

## Develop locally

```bash
cd web
npm install          # once
npm run check        # tsc --noEmit + the test suites
npm run build        # -> dist/  (GH_REF defaults to "develop")
python3 -m http.server 8000 --directory dist
# landing page:     http://localhost:8000/
# config generator: http://localhost:8000/generator.html
```

To preview the links for another ref: `GH_REF=main npm run build`.

## Test & type-check

```bash
npm run typecheck    # tsc --noEmit
npm test             # schema, state, and generate suites via tsx
```

CI runs `typecheck` + `test` + `build` before every deploy (see
`.github/workflows/pages.yml` and `.github/workflows/pr-preview.yml`).

## Save / load

User progress is saved automatically to `localStorage`. Users can also:

- **Save project file** — download the current answers as `astrameter-project.json`.
- **Load project file** — restore from that JSON to keep iterating later.
- **Copy share link** — encode the whole state into a URL hash to share or bookmark.

## Deploying

The build (`dist/`) is published to the **`gh-pages`** branch and served by
GitHub Pages.

One-time setup: repository **Settings → Pages → Build and deployment → Source →
"Deploy from a branch" → `gh-pages` / `/ (root)`**, and **Settings → Actions →
General → Workflow permissions → "Read and write permissions"** (so the workflow
can push to `gh-pages`).

The *Deploy config generator to GitHub Pages* workflow builds and publishes on
every push that touches `web/`:

- **Production** — pushes to **`main`** publish to the site **root**, served at
  the custom domain [`https://astrameter.com/`](https://astrameter.com/) (the
  `CNAME` file in `web/` is published to the `gh-pages` root by the build).
- **Staging** — pushes to **`develop`** publish under **`/develop/`**:
  `https://astrameter.com/develop/`
- **Per-PR previews** — the *Deploy PR preview* workflow deploys each pull
  request to `pr-preview/pr-<number>/` and comments the live URL on the PR; it's
  removed when the PR closes. (Same-repo branches only; forks can't write
  `gh-pages`.)

Root, `/develop/`, and `/pr-preview/` all coexist on `gh-pages` (`keep_files:
true`). The site uses only relative URLs, so it works under any subpath. They all
serve under the custom domain; `robots.txt` keeps `/develop/` and `/pr-preview/`
out of search indexes so only production gets indexed.

### GitHub links track the deployed ref

Every GitHub URL — bare repo links, README section anchors (install cards), the
doc-file links, the per-meter reference link, and the ESPHome
`external_components` source in generated configs — is produced by `ts/links.ts`
from a single ref. The deploy workflow passes that ref to the build via the
`GH_REF` env var (`main` / `develop` / the PR's `head_ref`), which esbuild bakes
in as `__GH_REF__`. So a `main` build links to `@main`, a PR preview to the PR's
branch, and develop to `@develop` — no post-build rewriting. (Only `/issues` is
ref-agnostic.) Static HTML links carry a `data-gh` attribute that `site.ts`
resolves at load; their hardcoded `href` is a no-JS fallback.

## Adding or editing a powermeter

Almost everything lives in **`ts/schema.ts`** — common changes are a one-file
edit, `tsc` catches type slips, and `schema.test.ts` guards the structure.

**Edit an existing field** (label, help, default, placeholder, options): find the
meter in `POWERMETERS` and change the field object. Done.

**Add a field to a meter**: add a `{ key, label, help, type, … }` object to that
meter's `fields` array. `key` is the `config.ini` key. Use `phase: true` for
per-phase values; `advanced: true` to tuck it behind the disclosure.

**Add a whole new powermeter**: append a typed entry to `POWERMETERS`:

```ts
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
declarative, so you rarely touch `ts/generate.ts`:

- per-meter ESP warning → `esphome.warn` (string, or `(f) => string|null`)
- HTTP request headers → `esphome.headersField: "FIELD_KEY"`
- a `homeassistant`-kind source that names its own entity → `esphome.haEntity: (f) => "sensor.x"`
- MQTT 3-phase key renames → top-level `phaseListKeys: { topic, jsonPath }`

You only edit `ts/generate.ts` to introduce a brand-new `esphome.kind` — then add
a handler and extend `ESP_KINDS` in `schema.test.ts`.

After any change, run `npm run check` and add an assertion to
`ts/generate.test.ts` for the new output.

## Keeping it in sync

The schema mirrors the options documented in the repo. The sources of truth are
`config.ini.example`, `esphome.example.yaml`, `docs/powermeters.md`,
`docs/esphome-powermeters.md`, and the **Configuration** section of the main
`README.md`.
