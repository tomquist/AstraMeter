// Build the dashboard into ONE self-contained HTML file and write it to
// src/astrameter/static/dashboard.html.
//
// Why a committed artifact rather than a build step:
//   * neither the Docker build nor `esphome compile` has Node available —
//     the Dockerfile copies pyproject/uv.lock/src and nothing else;
//   * an ESP32 can only serve something already embedded in its firmware.
// A CI job runs this with --check and fails if the committed file differs,
// so the artifact cannot silently drift from its source.
//
// Why one file with everything inlined: Home Assistant serves the panel
// through ingress at an arbitrary path prefix, and a single document with no
// subresources simply cannot get a URL wrong.
//
// The size budget is the ESPHome forcing function. An ESP32 has to hold this
// in flash alongside the real firmware, so the bundle stays small enough to
// remain a candidate — which is also what keeps it framework-free.

import { build } from "esbuild";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { gzipSync } from "node:zlib";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "../..");
const OUT = resolve(repo, "src/astrameter/static/dashboard.html");

// Hard ceiling, enforced in CI. Past this the bundle stops being something
// an ESP32 could serve, which is the property we are protecting.
const BUDGET_GZIP = 60 * 1024;

const checkOnly = process.argv.includes("--check");

const bundle = await build({
  entryPoints: [resolve(repo, "web/ts/dashboard/app.ts")],
  bundle: true,
  write: false,
  // IIFE, not ESM: the script is inlined into the page, so there is no
  // module loader and nothing to import from.
  format: "iife",
  target: "es2019",
  minify: true,
  legalComments: "none",
  sourcemap: false,
  // No branch-dependent define: the output must be byte-identical on every
  // machine or the drift check is worthless.
});

const js = bundle.outputFiles[0].text;
const css = await readFile(resolve(repo, "web/css/dashboard.css"), "utf8");

const html = `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>AstraMeter</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='7' fill='%2303a9f4'/%3E%3Cpath d='M18 4 10 18h5l-1 10 8-14h-5z' fill='%23fff'/%3E%3C/svg%3E">
<style>${css}</style>
</head>
<body>
<div id="app"></div>
<script>${js}</script>
</body>
</html>
`;

const gzipped = gzipSync(Buffer.from(html), { level: 9 }).length;
const summary = `${(html.length / 1024).toFixed(1)} KiB raw, ${(gzipped / 1024).toFixed(1)} KiB gzipped`;

if (gzipped > BUDGET_GZIP) {
  console.error(
    `✗ dashboard bundle is ${summary} — over the ${BUDGET_GZIP / 1024} KiB gzipped budget.\n` +
      `  The budget exists so an ESP32 can still serve this. Trim the bundle ` +
      `rather than raising it.`,
  );
  process.exit(1);
}

if (checkOnly) {
  let existing = null;
  try {
    existing = await readFile(OUT, "utf8");
  } catch {
    /* not generated yet */
  }
  if (existing !== html) {
    console.error(
      `✗ ${OUT} is out of date.\n  Run: cd web && npm run build:dashboard`,
    );
    process.exit(1);
  }
  console.log(`✓ dashboard asset up to date (${summary})`);
} else {
  await mkdir(dirname(OUT), { recursive: true });
  await writeFile(OUT, html);
  console.log(`✓ wrote ${OUT} (${summary})`);
}
