# AstraMeter Config Generator (web)

A static, beginner-friendly website that generates an AstraMeter configuration —
either a Python `config.ini` (Home Assistant add-on / Docker / direct install)
or an ESPHome YAML (run the CT002/CT003 emulator on an ESP32).

It runs entirely in the browser. Nothing is uploaded; generation happens with
plain JavaScript. The form is data-driven from `js/schema.js`, so adding or
changing an option is a one-file edit.

## Files

| File | Purpose |
|------|---------|
| `index.html` | Page shell and intro copy. |
| `css/styles.css` | Styling (dark theme, responsive, sticky live preview). |
| `js/schema.js` | Single source of truth: every powermeter, field, and tuning option with beginner help text. Pure data. |
| `js/generate.js` | Pure functions that turn the app state into `config.ini` or ESPHome YAML. No DOM. |
| `js/app.js` | Renders the form from the schema, holds state, live preview, save/load. |
| `js/generate.test.mjs` | Node assertions for the generators. |

## Develop locally

It's static — open `index.html` through any local server (ES modules need
`http://`, not `file://`):

```bash
cd web
python3 -m http.server 8000
# open http://localhost:8000
```

## Test

```bash
node web/js/generate.test.mjs
```

CI runs the same test before deploying (see `.github/workflows/pages.yml`).

## Save / load

User progress is saved automatically to `localStorage`. Users can also:

- **Save project file** — download the current answers as `astrameter-project.json`.
- **Load project file** — restore from that JSON to keep iterating later.
- **Copy share link** — encode the whole state into a URL hash to share or bookmark.

## Deploying

The site is published by the **Deploy config generator to GitHub Pages**
workflow. One-time: repository **Settings → Pages → Source → GitHub Actions**.
After that, any push touching `web/` redeploys it.

## Keeping it in sync

The schema mirrors the options documented in the repo. When a powermeter or
option is added or changed, update `js/schema.js` (and `js/generate.js` if the
output format changes) and extend `js/generate.test.mjs`. The sources of truth
are `config.ini.example`, `esphome.example.yaml`, `docs/powermeters.md`,
`docs/esphome-powermeters.md`, and the **Configuration** section of the main
`README.md`.
