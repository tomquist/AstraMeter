import { defineConfig, devices } from "@playwright/test";

/**
 * Browser-level end-to-end tests for the dashboard.
 *
 * These exist because a class of defect is invisible to the unit tests: the
 * views are pure functions rendered to a string there, so anything involving
 * the *live* DOM — a disclosure snapping shut on the next poll, an element
 * being recreated under the user's cursor, a control that writes but never
 * confirms — renders perfectly and is still broken. Both of those shipped
 * and were caught here.
 *
 * Each spec boots the real stack: the battery simulator speaking CT002 UDP,
 * a real AstraMeter reading it, and the committed dashboard bundle. The
 * Shelly spec is the exception only in who polls — the simulator has no
 * Shelly client, so the spec sends the datagrams itself.
 */

// This container ships Chromium at a fixed path and blocks downloads, while
// CI installs its own. Honour an override, else let Playwright resolve it.
const executablePath = process.env.ASTRAMETER_E2E_CHROMIUM || undefined;

export default defineConfig({
  testDir: ".",
  testMatch: /.*\.spec\.ts/,
  // The stack is booted per worker and the specs mutate shared device state.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  use: {
    ...devices["Desktop Chrome"],
    launchOptions: { executablePath },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
