import { test, expect } from "@playwright/test";
import { BASE_URL, readIni, startStack, type Stack } from "./stack.js";

/** What the backend substitutes for a stored secret (status/secrets.py). */
const SENTINEL = "\u2022".repeat(8);

/** The structured `config.ini` editor, asserted against the file on disk. */

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
  await page.goto(`${BASE_URL}#/config`);
  await expect(page.locator("details.section").first()).toBeVisible();
});

test("renders a card per section with a control typed per setting", async ({
  page,
}) => {
  await expect(page.locator(".sec-name")).toContainText([
    "[GENERAL]",
    "[CT002]",
    "[JSON_HTTP]",
  ]);
  // Types come from the backend's own key metadata, not guesswork.
  await expect(
    page.locator('select[aria-label="DEVICE_TYPE value"]'),
  ).toBeVisible();
  await expect(
    page.locator('select[aria-label="ACTIVE_CONTROL value"]'),
  ).toBeVisible();
  await expect(
    page.locator('input[type="number"][aria-label="UDP_PORT value"]'),
  ).toBeVisible();
  await expect(
    page.locator('input[type="text"][aria-label="URL value"]'),
  ).toBeVisible();
});

test("editing a setting writes it to the file", async ({ page }) => {
  expect(readIni(stack)).toContain("MIN_EFFICIENT_POWER = 100");

  await page.locator('[aria-label="MIN_EFFICIENT_POWER value"]').fill("250");
  await expect(page.locator('button:text("Save only")')).toBeEnabled();
  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner")).toContainText("Saved");

  expect(readIni(stack)).toContain("MIN_EFFICIENT_POWER = 250");
});

test("a setting can be added, typed from its name, and saved", async ({ page }) => {
  const ct = page.locator("details.section", {
    has: page.locator('.sec-name:text-is("[CT002]")'),
  });
  await ct.locator('button:text("+ Add setting")').click();

  const name = page.locator('[aria-label="Setting name: NEW_SETTING"]');
  await name.fill("WIFI_RSSI");
  await name.blur();

  // Naming it a known key gives it that key's type.
  const value = page.locator('[aria-label="WIFI_RSSI value"]');
  await expect(value).toHaveAttribute("type", "number");
  await value.fill("-62");
  await value.dispatchEvent("change");

  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner")).toContainText("Saved");
  expect(readIni(stack)).toContain("WIFI_RSSI = -62");
});

test("a setting can be removed", async ({ page }) => {
  // Create it here rather than leaning on the previous test: Playwright
  // retries a single test, not the file, so on a retry the row would be
  // missing and a skip would report this as green without asserting removal.
  const row = page.locator(".keyrow", {
    has: page.locator('[aria-label="Setting name: WIFI_RSSI"]'),
  });
  if ((await row.count()) === 0) {
    const ct = page.locator("details.section", {
      has: page.locator('summary:text("CT002")'),
    });
    await ct.locator('button:text("+ Add setting")').click();
    const name = page.locator('[aria-label="Setting name: NEW_SETTING"]');
    await name.fill("WIFI_RSSI");
    await name.blur();
    const value = page.locator('[aria-label="WIFI_RSSI value"]');
    await value.fill("-62");
    await value.dispatchEvent("change");
    await page.locator('button:text("Save only")').click();
    await expect(page.locator(".banner")).toContainText("Saved");
  }
  await expect(row).toHaveCount(1);
  await row.locator("button.iconbtn").click();
  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner")).toContainText("Saved");
  expect(readIni(stack)).not.toContain("WIFI_RSSI");
});

test("known settings are suggested for the section", async ({ page }) => {
  const options = await page
    .locator("#keys-CT002 option")
    .evaluateAll((els) => els.map((e) => e.getAttribute("value")));
  expect(options).toContain("ACTIVE_CONTROL");
  expect(options).toContain("WIFI_RSSI");
  // Suggestions are section-scoped, not one global list.
  expect(options).not.toContain("DEVICE_TYPE");
});

test("a section can be collapsed and stays collapsed", async ({ page }) => {
  // Regression: `open` was re-asserted on every render, so a section could
  // be expanded but never closed.
  const section = page.locator("details.section").first();
  await expect(section).toHaveAttribute("open", "");
  await section.locator("summary").click();
  await expect(section).not.toHaveAttribute("open", "");
  await page.waitForTimeout(5000);
  await expect(section).not.toHaveAttribute("open", "");
});

test("a corrupting payload is refused and the running file left alone", async ({
  page,
}) => {
  const before = readIni(stack);
  // A newline in a value would split the line and silently corrupt the file,
  // so the write guard rejects it outright.
  const response = await page.request.post(`${BASE_URL}api/config`, {
    data: {
      sections: { GENERAL: { DEVICE_TYPE: "ct002\nEVIL = 1" } },
      order: ["GENERAL"],
    },
  });
  expect(response.status()).toBe(400);
  expect(readIni(stack)).toBe(before);
});

test("secrets are masked and survive an unrelated edit", async ({ page }) => {
  // Put a secret in the file, then confirm the browser never sees it and
  // editing something else does not blank it.
  await page.locator('button:text("+ Add section")').click();
  const newSection = page.locator("details.section").last();
  await newSection.locator('button:text("+ Add setting")').click();
  const name = newSection.locator('[aria-label="Setting name: NEW_SETTING"]');
  await name.fill("PASSWORD");
  await name.blur();
  const value = newSection.locator('[aria-label="PASSWORD value"]');
  await expect(value).toHaveAttribute("type", "password");
  await value.fill("hunter2");
  await value.dispatchEvent("change");
  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner")).toContainText("Saved");
  expect(readIni(stack)).toContain("PASSWORD = hunter2");

  await page.reload();
  await expect(page.locator("details.section").first()).toBeVisible();
  const served = await (await fetch(`${BASE_URL}api/config`)).text();
  expect(served).not.toContain("hunter2");
  // Assert the decoded value: whether the encoder escapes the bullet or
  // emits it as UTF-8 is not what this test is about.
  const sections = JSON.parse(served).sections as Record<
    string,
    Record<string, string>
  >;
  const stored = Object.values(sections)
    .map((pairs) => pairs.PASSWORD)
    .filter((v) => v !== undefined);
  expect(stored).toEqual([SENTINEL]);

  await page.locator('[aria-label="MIN_EFFICIENT_POWER value"]').fill("300");
  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner")).toContainText("Saved");
  // The password is still the real one, not eight bullets.
  expect(readIni(stack)).toContain("PASSWORD = hunter2");
  expect(readIni(stack)).toContain("MIN_EFFICIENT_POWER = 300");
});
