import { test, expect } from "@playwright/test";
import { BASE_URL, startStack, statusSnapshot, type Stack } from "./stack.js";

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
});

test("shows live grid and battery state from real UDP reports", async ({ page }) => {
  await page.goto(BASE_URL);

  // The headline is the grid total, signed.
  await expect(page.locator(".rail-value")).toHaveText(/[−+]?\d/);
  await expect(page.locator(".rail-label")).toHaveText(
    /(import|export)ing from the grid|exporting to the grid/,
  );

  // One contribution row per reporting battery, on the grid's own axis.
  await expect(page.locator(".contrib-row")).toHaveCount(2);
  await expect(page.locator(".contrib")).toContainText(
    "Each battery's effect on the grid",
  );

  await expect(page.locator(".card", { hasText: "EMULATOR" })).toContainText(
    "Active control",
  );
  // Named without the class suffix every one of them carries: a filter chain
  // reading "Hampel → Smoothed → Deadband" says the same thing as one
  // repeating "Powermeter" after each.
  const source = page.locator(".card", { hasText: "POWER SOURCE" });
  await expect(source).toContainText("JsonHttp");
  await expect(source).not.toContainText("Powermeter");
});

test("values keep updating as the batteries steer", async ({ page }) => {
  await page.goto(BASE_URL);
  const seq = async () => (await statusSnapshot()).seq;
  const first = await seq();
  await expect.poll(seq, { timeout: 20_000 }).toBeGreaterThan(first);
  // A live page must reflect that without a reload.
  await expect(page.locator(".rail-value")).toBeVisible();
});

test("a trend line fills in from the polls the page makes", async ({ page }) => {
  // No backend series: the samples are the ones the page has already received,
  // so the line appears only after a few polls rather than on first paint.
  await page.goto(`${BASE_URL}#/sources`);
  const spark = page.locator(".spark-line").first();
  await expect(spark).toBeVisible({ timeout: 30_000 });
  // Points, not an empty element: a broken geometry renders as an SVG that is
  // there but draws nothing.
  const points = await spark.getAttribute("points");
  expect((points || "").split(" ").length).toBeGreaterThanOrEqual(3);
  // In the SVG namespace, which is not what createElement gives you:
  // document.createElement("svg") makes an HTMLUnknownElement, so every
  // attribute lands and the aria-label still reads while nothing is drawn.
  expect(await spark.evaluate((el) => el.namespaceURI)).toBe(
    "http://www.w3.org/2000/svg",
  );
  await expect(page.locator(".spark-range").first()).toContainText("W");
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

test("a deep link into a tab loads that tab's data", async ({ page }) => {
  // Regression: only the tab *click* triggered the config load, so opening
  // the URL directly sat on "Loading…" forever.
  await page.goto(`${BASE_URL}#/config`);
  await expect(page.locator("details.section").first()).toBeVisible();
  await expect(page.locator("main")).not.toContainText("Loading config.ini");
});

test("diagnostics exposes the balancer internals", async ({ page }) => {
  await page.goto(`${BASE_URL}#/diagnostics`);
  const balancer = page.locator(".card", { hasText: "BALANCER" });
  await expect(balancer).toContainText("Predicted grid");
  await expect(balancer).toContainText("Prediction trust");
  await expect(balancer).toContainText("Efficiency rotation");
});

test("the page survives losing the backend and recovers", async ({ page }) => {
  await page.goto(BASE_URL);
  await expect(page.locator(".rail-value")).toBeVisible();

  // Cut the API and confirm the page says so rather than freezing on a
  // stale-looking reading.
  await page.route("**/api/status", (route) => route.abort());
  await expect(page.locator(".banner.err")).toContainText(
    "Lost contact with AstraMeter",
    { timeout: 30_000 },
  );
  await expect(page.locator("#app")).toHaveAttribute("data-conn", "offline");
  // Relative ages would be frozen and misleading, so they are withdrawn.
  await expect(page.locator("main")).not.toContainText(/\d+(\.\d+)? s ago/);

  await page.unroute("**/api/status");
  await expect(page.locator("#app")).toHaveAttribute("data-conn", "live", {
    timeout: 30_000,
  });
  await expect(page.locator(".banner.err")).toHaveCount(0);
});

test("serves no absolute URLs, so it works under an ingress prefix", async ({
  page,
}) => {
  const requested: string[] = [];
  page.on("request", (r) => requested.push(r.url()));
  await page.goto(BASE_URL);
  await page.waitForTimeout(3000);
  for (const url of requested) {
    expect(url.startsWith(BASE_URL), `${url} escaped the base path`).toBe(true);
  }
  const html = await (await fetch(BASE_URL)).text();
  // One self-contained document: no subresource can resolve to the wrong place.
  expect(html).not.toMatch(/<(script|link)[^>]+(src|href)="\/(?!\/)/);
});

test("the control-quality verdict survives live updates", async ({ page }) => {
  // The verdict chip and its blurb are conditional nodes: they appear only
  // once the backend has a verdict, so each poll toggles them between absent
  // and present. The unit tests render to a string and cannot see the
  // reconciler renumbering siblings around that, which would corrupt the rest
  // of the card.
  await page.goto(`${BASE_URL}#/diagnostics`);
  const card = page.locator(".card", { hasText: "BALANCER" });
  await expect(card).toBeVisible({ timeout: 30_000 });

  const chip = card.locator(".chip");
  await expect(chip).toHaveText(/Idle|Warming up|Stable|Off target|Limited/, {
    timeout: 30_000,
  });
  // Whatever the loop is doing, the rows below the chip keep their pairing —
  // a mis-patched card shows a label against the wrong value.
  await expect(card).toContainText("Settling band");
  await expect(card).toContainText("Predicted grid");

  // Survives several polls without the card being torn down and rebuilt.
  // Marked on the live node: a reconciler that replaces the element instead of
  // patching it loses the mark, which is exactly the failure a string-rendered
  // unit test cannot see.
  await card.evaluate((el) => ((el as any).__mark = "kept"));
  // Wait on observed progress rather than the clock: a fixed sleep is tied to
  // whatever the poll interval happens to be, so it stops covering several
  // polls the moment that interval grows. `seq` advances once per snapshot the
  // backend produces, which is what the page is polling for.
  const seq = async () => (await statusSnapshot()).seq;
  const before = await seq();
  await expect.poll(seq, { timeout: 30_000 }).toBeGreaterThan(before + 2);
  await expect(chip).toBeVisible();
  expect(await card.evaluate((el) => (el as any).__mark)).toBe("kept");
});
