import { test, expect } from "@playwright/test";
import { BASE_URL, pollShelly, startStack, type Stack } from "./stack.js";

/**
 * The Shelly half of the device matrix — the default device type, and the
 * one whose page has no consumers, no balancer and no controls.
 *
 * The unit tests render these views to a string, which cannot see the live
 * DOM: this file is here to prove the page goes from "nothing has polled me"
 * to a battery card in place, on a real poll, without a reload.
 */

let stack: Stack;

test.beforeAll(async () => {
  stack = await startStack({ shelly: true });
});
test.afterAll(async () => {
  await stack?.stop();
});

test.beforeEach(async ({ page }) => {
  page.on("pageerror", (e) => {
    throw new Error(`uncaught page error: ${e}`);
  });
});

function emulator(page: import("@playwright/test").Page) {
  return page.locator(".card", { hasText: "shellyemg3 emulator" });
}

test("a battery that starts polling appears in place", async ({ page }) => {
  await page.goto(BASE_URL);

  // Nothing has polled yet, and the empty state names the meter this install
  // actually emulates rather than the CT.
  await expect(page.locator(".empty")).toContainText(
    "No batteries have reported yet",
  );
  await expect(page.locator(".empty")).toContainText("select this Shelly meter");
  await expect(emulator(page)).toContainText("No batteries polling");
  await expect(emulator(page)).toContainText("UDP 2222");

  // A real datagram over the real socket, answered with a real meter reading.
  const reply = await pollShelly();
  expect(typeof reply.result.total_act_power).toBe("number");

  // The same page instance has to reflect it — this is the patch the unit
  // tests cannot see.
  await expect(emulator(page)).toContainText("Serving readings");
  await expect(emulator(page)).toContainText("Batteries polling");
  await expect(page.locator(".empty")).toHaveCount(0);
});

test("the hero shows the house total the emulator is serving", async ({ page }) => {
  await page.goto(BASE_URL);
  await pollShelly();

  // No CT device reports a grid triple here, so the total comes from the
  // power source — a dash would be wrong when a reading exists.
  await expect(page.locator(".rail-value")).toHaveText(/[−+]?\d/);
  // Named without the class suffix every one of them carries.
  const source = page.locator(".card", { hasText: "POWER SOURCE" });
  await expect(source).toContainText("JsonHttp");
  await expect(source).not.toContainText("Powermeter");
});

test("nothing this device cannot do is offered", async ({ page }) => {
  await page.goto(BASE_URL);
  await expect(emulator(page)).toBeVisible();

  // A Shelly emulator steers nothing: no balancer, and no switches that would
  // write to a consumer that does not exist.
  await expect(emulator(page)).not.toContainText("Active control");
  await expect(page.locator(".contrib")).toHaveCount(0);
  await expect(page.locator("main .switch")).toHaveCount(0);
});

test("the batteries tab lists who is polling, and how often", async ({ page }) => {
  // Twice, so the emulator has an interval to smooth rather than a first
  // sighting.
  await pollShelly();
  await new Promise((r) => setTimeout(r, 1200));
  await pollShelly();

  await page.goto(`${BASE_URL}#/batteries`);
  const battery = page.locator(".card", { hasText: "127.0.0.1" });
  await expect(battery).toContainText("Polling");
  await expect(battery).toContainText("Last seen");
  await expect(battery).toContainText("Polls every");
});

test("every tab renders without a page error", async ({ page }) => {
  for (const tab of ["overview", "batteries", "sources", "diagnostics", "config"]) {
    await page.goto(`${BASE_URL}#/${tab}`);
    await expect(page.locator(`.tab[aria-current="page"]`)).toBeVisible();
    await expect(page.locator("main")).not.toContainText("undefined");
    await expect(page.locator("main")).not.toContainText("NaN");
    await expect(page.locator("main")).not.toContainText("[object Object]");
  }
});
