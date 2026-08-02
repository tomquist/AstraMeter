import { test, expect } from "@playwright/test";
import {
  BASE_URL,
  battery,
  startStack,
  statusSnapshot,
  type Stack,
} from "./stack.js";

/**
 * The live control surface — the same set the MQTT integration exposes.
 *
 * Every assertion here checks the *device*, not the DOM: a control that
 * lights up but changes nothing is the failure worth catching.
 */

const BATTERY = "02b250000001";

let stack: Stack;

test.beforeAll(async () => {
  stack = await startStack();
});
test.afterAll(async () => {
  await stack?.stop();
});

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (e) => {
    throw new Error(`uncaught page error: ${e}`);
  });
  await page.goto(`${BASE_URL}#/batteries`);
  await expect(page.locator("details.controls-fold").first()).toBeVisible();
});

function firstFold(page: import("@playwright/test").Page) {
  return page.locator("details.controls-fold").first();
}

test("the controls panel stays open across live updates", async ({ page }) => {
  // Regression: the 1 Hz re-render stripped the `open` attribute the browser
  // sets on <details>, so the panel snapped shut and nothing inside it could
  // be used at all.
  const fold = firstFold(page);
  await fold.locator("summary").click();
  await expect(fold).toHaveAttribute("open", "");
  await page.waitForTimeout(6000);
  await expect(fold).toHaveAttribute("open", "");
  await expect(fold.locator('[aria-label="Distribution weight"]')).toBeVisible();
});

test("controls are not recreated under the user on each poll", async ({ page }) => {
  // Regression: absent children were filtered out instead of holding their
  // slot, so every conditional control renumbered its siblings and the
  // reconciler replaced live elements — destroying focus mid-interaction.
  const fold = firstFold(page);
  await fold.locator("summary").click();
  const slider = fold.locator('[aria-label="Distribution weight"]');
  await slider.evaluate((el) => ((window as any).__probe = el));
  await slider.focus();
  await page.waitForTimeout(6000);
  expect(
    await slider.evaluate((el) => el === (window as any).__probe),
    "the slider element was replaced by a re-render",
  ).toBe(true);
  expect(
    await page.evaluate(
      () => document.activeElement?.getAttribute("aria-label") ?? null,
    ),
    "focus was lost to a re-render",
  ).toBe("Distribution weight");
});

test("enabling and disabling a battery reaches the device", async ({ page }) => {
  const fold = firstFold(page);
  await fold.locator("summary").click();
  const active = fold.locator('input[aria-label="Active"]');

  await active.uncheck();
  await expect.poll(async () => (await battery(BATTERY)).active).toBe(false);
  await active.check();
  await expect.poll(async () => (await battery(BATTERY)).active).toBe(true);
});

test("a manual target can be set and handed back to automatic", async ({ page }) => {
  const fold = firstFold(page);
  await fold.locator("summary").click();
  const auto = fold.locator('input[aria-label="Auto target"]');

  await auto.uncheck();
  await expect.poll(async () => (await battery(BATTERY)).manual_enabled).toBe(true);

  const target = fold.locator('[aria-label="Manual target"]');
  await expect(target).toBeVisible();
  await target.fill("-450");
  await target.dispatchEvent("change");
  await expect
    .poll(async () => (await battery(BATTERY)).manual_target_w)
    .toBe(-450);

  await auto.check();
  await expect.poll(async () => (await battery(BATTERY)).manual_enabled).toBe(false);
  // Back on automatic, the setpoint box is not offered.
  await expect(fold.locator('[aria-label="Manual target"]')).toHaveCount(0);
});

test("distribution weight writes through", async ({ page }) => {
  const fold = firstFold(page);
  await fold.locator("summary").click();
  const weight = fold.locator('[aria-label="Distribution weight"]');
  await weight.fill("2.5");
  await weight.dispatchEvent("change");
  await expect
    .poll(async () => (await battery(BATTERY)).distribution_weight)
    .toBe(2.5);
});

test("efficiency window is a percentage end to end", async ({ page }) => {
  // The MQTT entity is 0-100 % while the setter takes a 0-1 fraction; the
  // two must not disagree, or 50 in the UI would mean 50x internally.
  const fold = firstFold(page);
  await fold.locator("summary").click();
  const window = fold.locator('[aria-label="Efficiency window"]');
  await expect(window).toBeVisible();
  await window.fill("40");
  await window.dispatchEvent("change");
  await expect
    .poll(async () => (await battery(BATTERY)).efficiency_window_weight_pct)
    .toBe(40);
});

test("device-wide active control and force rotation are reachable", async ({
  page,
}) => {
  await page.goto(`${BASE_URL}#/overview`);
  const controls = page.locator(".controls");
  await expect(controls).toBeVisible();

  await expect(page.locator('button:text("Force rotation")')).toBeEnabled();
  await page.locator('button:text("Force rotation")').click();

  const toggle = controls.locator('input[aria-label="Active control"]');
  await toggle.uncheck();
  await expect
    .poll(async () => (await statusSnapshot()).devices[0].control.active_control)
    .toBe(false);
  await toggle.check();
  await expect
    .poll(async () => (await statusSnapshot()).devices[0].control.active_control)
    .toBe(true);
});

test("an in-flight write is not snapped back by the next poll", async ({ page }) => {
  // Regression: the poll re-rendered the switch from the server's *old*
  // value before the write landed, flipping it back under the user.
  const fold = firstFold(page);
  await fold.locator("summary").click();
  const active = fold.locator('input[aria-label="Active"]');

  // Hold the write open so a poll is guaranteed to land mid-flight.
  await page.route("**/api/control/consumer", async (route) => {
    await new Promise((r) => setTimeout(r, 4000));
    await route.continue();
  });
  await active.uncheck();
  await page.waitForTimeout(2500);
  expect(await active.isChecked(), "the switch reverted mid-write").toBe(false);
  await page.unroute("**/api/control/consumer");
  await expect.poll(async () => (await battery(BATTERY)).active).toBe(false);
  // Wait for the restore to land, not just to be sent: these tests share one
  // stack, and a write still in flight re-renders the panel under the next
  // test while it is reaching for a control.
  await active.check();
  await expect.poll(async () => (await battery(BATTERY)).active).toBe(true);
});

test("a rejected write surfaces the reason", async ({ page }) => {
  const fold = firstFold(page);
  await fold.locator("summary").click();
  await fold.locator('input[aria-label="Auto target"]').uncheck();
  const target = fold.locator('[aria-label="Manual target"]');
  await expect(target).toBeVisible();

  // Past the range MQTT enforces; the server must refuse and say why.
  await target.evaluate((el: HTMLInputElement) => {
    el.value = "99999";
    el.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect(page.locator(".banner.err")).toContainText(
    "manual_target must be between",
  );
});

test("a read-only dashboard offers no controls", async ({ page }) => {
  await page.route("**/api/status", async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    body.capabilities.controls = false;
    await route.fulfill({ response, json: body });
  });
  await page.goto(`${BASE_URL}#/batteries`);
  await expect(page.locator(".card").first()).toBeVisible();
  await expect(page.locator("details.controls-fold")).toHaveCount(0);
  await page.goto(`${BASE_URL}#/overview`);
  await expect(page.locator('button:text("Force rotation")')).toHaveCount(0);
});
