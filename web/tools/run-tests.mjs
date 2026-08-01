// Run every ts/**/*.test.ts through tsx.
//
// Discovered by glob rather than listed, so adding a test file never
// requires editing package.json — the failure mode that silently leaves new
// tests unrun.

import { readdir } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { dirname, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const web = resolve(here, "..");
const root = join(web, "ts");

async function findTests(dir) {
  const out = [];
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) out.push(...(await findTests(full)));
    else if (entry.name.endsWith(".test.ts")) out.push(full);
  }
  return out.sort();
}

const tests = await findTests(root);
if (!tests.length) {
  console.error("✗ no test files found under ts/");
  process.exit(1);
}

let failed = 0;
for (const test of tests) {
  const name = relative(web, test);
  const result = spawnSync("npx", ["tsx", test], {
    cwd: web,
    stdio: "inherit",
    shell: process.platform === "win32",
  });
  if (result.status !== 0) {
    failed++;
    console.error(`✗ ${name} exited ${result.status}`);
  }
}

if (failed) {
  console.error(`\n${failed} of ${tests.length} test file(s) failed`);
  process.exit(1);
}
console.log(`\n${tests.length} test file(s) passed`);
