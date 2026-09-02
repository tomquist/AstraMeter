---
name: dashboard
description: The dashboard page served by both stacks — its committed build outputs, browser tests, and the docs screenshots. Use when changing anything under web/ or refreshing docs/images/dashboard-*.png.
---

# Dashboard and screenshots

`web/ts/dashboard/` is **one page served by both stacks**, so a UI change lands
on the Python service and the ESPHome component at once. The document behind it
differs; the `ct002-parity` skill covers that split and the firmware's
constraints on the page.

Verify with `cd web && npm run check`.

## Committed build outputs

`src/astrameter/static/dashboard.html` and
`esphome/components/ct002/dashboard_asset.h` (the same page gzipped, for the
ESP32's flash) are **committed generated artifacts** — neither the Docker build
nor `esphome compile` has Node. After touching anything under `web/`, run `cd
web && npm run build:dashboard` and commit **both**; CI fails on a stale one.

`.gitattributes` marks them `-diff -merge linguist-generated`, so a one-line
source edit doesn't bury itself under a rewritten byte array. Two consequences:
`git diff` won't show you their contents — ask `npm run check:dashboard` whether
they're stale — and a branch that conflicts on one is resolved by
**regenerating**, not by editing the conflict markers.

## Browser tests

`web/e2e/` (`npm run e2e`) boots the real stack. Anything touching the live DOM
— the reconciler, a control's write path, a disclosure — needs a test there: the
unit tests render views to a string and cannot see those failures.

## Screenshots

`docs/images/dashboard-<tab>-<light|dark>.png` are committed generated artifacts
too, embedded by `docs/dashboard.md`, `README.md` and the landing page
(`web/build.mjs` copies them into `dist/assets/screenshots/`).

```bash
cd web && npm run screenshots     # ~8 minutes; takes all 8
```

`web/tools/screenshots.ts` boots the same stack the browser tests use — via a
`sim`/`configDir` override on `startStack` — against a bigger house (three
batteries, five appliances, solar) and drives a real browser. It is deliberately
patient: the trend lines are built in the browser from polls, so it waits for
real samples and for a moment when the grid is actually at zero with every
battery working. `--tabs`, `--themes`, `--warmup` and `--out` narrow a re-run.

There is **no CI check** for staleness — the values are live, so every run
differs and a diff would always be dirty. Refresh them when a UI change makes
them wrong, and hold onto two things while you do:

- **The captions claim the grid sits at zero, and the images have to earn it.**
  A shot tens of watts off means the scenario is wrong, not the caption — don't
  reword to match a bad run. Two settings decide it: `base_noise` is re-rolled
  every read, so it is a hard floor under how close to zero the grid can be
  held, and `auto_interval` must stay longer than the loop takes to settle
  (~35 s mean, ~62 s p95) or the house is never settled at all. `--settle`
  (default 30 W, a little wider than the balancer's own ±25 W band) catches both.
- **`web/index.html` states each image's intrinsic `width`/`height`** to reserve
  layout space. The crop height follows the tab's content, so re-check those
  attributes after a refresh that changes a tab's height.
