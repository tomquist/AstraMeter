// Build the dashboard into ONE self-contained HTML file and write it to
// src/astrameter/static/dashboard.html, plus the gzipped copy the ESPHome
// firmware serves from flash (esphome/components/ct002/dashboard_asset.h).
//
// Why committed artifacts rather than a build step:
//   * neither the Docker build nor `esphome compile` has Node available —
//     the Dockerfile copies pyproject/uv.lock/src and nothing else;
//   * an ESP32 can only serve something already embedded in its firmware.
// A CI job runs this with --check and fails if a committed file differs,
// so neither artifact can silently drift from its source.
//
// Why one file with everything inlined: Home Assistant serves the panel
// through ingress at an arbitrary path prefix, and a single document with no
// subresources simply cannot get a URL wrong. The ESP32 gets the same
// property for free — and one flash blob to serve instead of a file table.
//
// The size budget is the ESPHome forcing function. An ESP32 has to hold this
// in flash alongside the real firmware, so the bundle stays small — which is
// also what keeps it framework-free.

import { build } from "esbuild";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { gzipSync } from "node:zlib";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "../..");
const OUT = resolve(repo, "src/astrameter/static/dashboard.html");
const OUT_ESPHOME = resolve(repo, "esphome/components/ct002/dashboard_asset.h");

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

const gz = gzipSync(Buffer.from(html), { level: 9 });
// zlib stamps the building platform into the header's OS byte, which would
// make the committed artifact differ between a Linux and a Windows
// contributor for no difference in content. Pin it to "unknown".
gz[9] = 0xff;

const summary = `${(html.length / 1024).toFixed(1)} KiB raw, ${(gz.length / 1024).toFixed(1)} KiB gzipped`;

if (gz.length > BUDGET_GZIP) {
  console.error(
    `✗ dashboard bundle is ${summary} — over the ${BUDGET_GZIP / 1024} KiB gzipped budget.\n` +
      `  The budget exists so an ESP32 can still serve this. Trim the bundle ` +
      `rather than raising it.`,
  );
  process.exit(1);
}

/** The gzipped page as a C array the ct002 component compiles into flash. */
function esphomeHeader(bytes) {
  const lines = [];
  for (let i = 0; i < bytes.length; i += 16) {
    const row = [...bytes.subarray(i, i + 16)]
      .map((b) => `0x${b.toString(16).padStart(2, "0")}`)
      .join(", ");
    lines.push(`    ${row},`);
  }
  return `#pragma once
// GENERATED FILE — do not edit.
//
// The dashboard page (web/ts/dashboard/), gzipped, for the ESP32 to serve
// straight out of flash. Regenerate with: cd web && npm run build:dashboard
// CI fails on a stale copy, so this cannot drift from its source.
//
// ${(bytes.length / 1024).toFixed(1)} KiB gzipped, from ${(html.length / 1024).toFixed(1)} KiB of HTML.

#include "esphome/core/hal.h"

namespace esphome {
namespace ct002 {
namespace dashboard {

constexpr uint8_t DASHBOARD_HTML_GZ[] PROGMEM = {
${lines.join("\n")}
};

}  // namespace dashboard
}  // namespace ct002
}  // namespace esphome
`;
}

const header = esphomeHeader(gz);

async function unchanged(path, expected) {
  let existing = null;
  try {
    existing = await readFile(path, "utf8");
  } catch {
    /* not generated yet */
  }
  return existing === expected;
}

if (checkOnly) {
  for (const [path, expected] of [
    [OUT, html],
    [OUT_ESPHOME, header],
  ]) {
    if (!(await unchanged(path, expected))) {
      console.error(
        `✗ ${relative(repo, path)} is out of date.\n  Run: cd web && npm run build:dashboard`,
      );
      process.exit(1);
    }
  }
  console.log(`✓ dashboard assets up to date (${summary})`);
} else {
  await mkdir(dirname(OUT), { recursive: true });
  await writeFile(OUT, html);
  await writeFile(OUT_ESPHOME, header);
  console.log(`✓ wrote ${relative(repo, OUT)} and ${relative(repo, OUT_ESPHOME)} (${summary})`);
}
