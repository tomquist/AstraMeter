import { test, expect } from "@playwright/test";
import {
  BASE_URL,
  readAddonOptions,
  startStack,
  statusSnapshot,
  type Stack,
} from "./stack.js";

/** What the backend substitutes for a stored secret (status/secrets.py). */
const SENTINEL = "\u2022".repeat(8);

/**
 * The Home Assistant add-on path: the guided form, the entity picker and the
 * configuration-mode switch, against a stand-in Supervisor serving the
 * repository's own `ha_addon/config.yaml`.
 */

let stack: Stack;

test.beforeAll(async () => {
  stack = await startStack({ homeAssistant: true });
});
test.afterAll(async () => {
  await stack?.stop();
});

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (e) => {
    throw new Error(`uncaught page error: ${e}`);
  });
});

/** The form card specifically — the mode card also mentions "guided setup". */
function form(page: import("@playwright/test").Page) {
  return page.locator(".card", {
    has: page.locator('h2:text-is("Guided setup")'),
  });
}

test("detects add-on options mode and refuses to edit the generated file", async () => {
  const caps = (await statusSnapshot()).capabilities;
  expect(caps.config_mode).toBe("ha_simple");
  expect(caps.ha_options).toBe(true);
  // The add-on regenerates config.ini every boot, so editing it would lose
  // the user's work; the capability says so and the route enforces it.
  expect(caps.config_writable).toBe(false);

  const refused = await fetch(`${BASE_URL}api/config`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ sections: { GENERAL: {} }, order: ["GENERAL"] }),
  });
  expect(refused.status).toBe(403);
});

test("renders the guided form from the add-on's own schema", async ({ page }) => {
  await page.goto(`${BASE_URL}#/config`);
  await expect(form(page)).toBeVisible();

  // Grouped and labelled, not a dump of raw option names.
  await expect(page.locator("h3")).toContainText([
    "Grid measurement",
    "Emulated meter",
  ]);
  // Types come from the schema: a bounded float, an enum, a password.
  await expect(page.getByLabel("Grid prediction trust")).toHaveAttribute(
    "max",
    "1",
  );
  await expect(page.locator("select").first()).toBeVisible();
  const secret = page.locator('input[type="password"]').first();
  await expect(secret).toBeVisible();

  // The raw file editor must not be offered in this mode.
  await expect(page.locator('button:text("+ Add section")')).toHaveCount(0);
});

test("saving writes the options back through the Supervisor", async ({ page }) => {
  await page.goto(`${BASE_URL}#/config`);
  await expect(form(page)).toBeVisible();

  await page.getByLabel("Grid prediction trust").fill("0.75");
  await page.getByLabel("Rotation interval (s)").fill("1200");
  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner")).toContainText("Saved");

  const stored = readAddonOptions(stack);
  expect(stored.grid_predict_trust).toBe(0.75);
  expect(stored.efficiency_rotation_interval).toBe(1200);
});

test("a stored secret never reaches the browser and is not overwritten", async ({
  page,
}) => {
  const served = await (await fetch(`${BASE_URL}api/addon/options`)).text();
  expect(served).not.toContain("super-secret-pw");
  // The decoded value, not its JSON escape: whether the encoder emits the
  // bullet escaped or as UTF-8 is not what this test is about.
  expect(JSON.parse(served).options.marstek_password).toBe(SENTINEL);

  await page.goto(`${BASE_URL}#/config`);
  await expect(form(page)).toBeVisible();
  await page.getByLabel("Grid prediction trust").fill("0.6");
  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner")).toContainText("Saved");

  // Echoing the sentinel back must keep the stored password, not blank it.
  expect(readAddonOptions(stack).marstek_password).toBe("super-secret-pw");
});

test("Supervisor's own rejection message is shown verbatim", async ({ page }) => {
  await page.goto(`${BASE_URL}#/config`);
  await expect(form(page)).toBeVisible();
  const trust = page.getByLabel("Grid prediction trust");
  await trust.evaluate((el: HTMLInputElement) => {
    el.value = "5";
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner.err")).toContainText("expected float in range");
});

test("the grid sensor is an entity picker listing only power sensors", async ({
  page,
}) => {
  await page.goto(`${BASE_URL}#/config`);
  const input = page.getByLabel("Grid power sensor");
  await expect(input).toBeVisible();

  const listId = await input.getAttribute("list");
  const suggestions = await page
    .locator(`#${listId} option`)
    .evaluateAll((els) => els.map((e) => e.getAttribute("value")));

  // Power sensors, including one with no device_class but a W/kW unit —
  // plenty of real installs look like that.
  expect(suggestions).toContain("sensor.grid_power");
  expect(suggestions).toContain("sensor.p1_meter_active_power");
  // Everything that could not be grid power stays out.
  expect(suggestions).not.toContain("sensor.house_energy_today");
  expect(suggestions).not.toContain("sensor.outside_temperature");
  expect(suggestions).not.toContain("light.kitchen");

  // The chosen entity resolves to something a human recognises.
  await expect(
    page.locator("label.field", { hasText: "Grid power sensor" }),
  ).toContainText("Grid power — currently");
});

test("an entity Home Assistant does not know is flagged in place", async ({
  page,
}) => {
  await page.goto(`${BASE_URL}#/config`);
  const input = page.getByLabel("Grid power sensor");
  await expect(input).toBeVisible();
  await input.fill("sensor.definitely_not_real");
  await expect(
    page.locator("label.field", { hasText: "Grid power sensor" }),
  ).toContainText("Not found in Home Assistant right now.");
  await expect(input).toHaveClass(/warn-input/);
});

test("switching to a config file materializes what is running", async ({ page }) => {
  await page.goto(`${BASE_URL}#/config`);
  const button = page.locator('button:text("Switch to a config file")');
  await expect(button).toBeVisible();
  await button.click();
  await expect(page.locator(".banner")).toContainText("Configuration mode changed");

  // The add-on is pointed at the new file...
  await expect
    .poll(() => readAddonOptions(stack).custom_config, { timeout: 15_000 })
    .toBe("astrameter.ini");
});
