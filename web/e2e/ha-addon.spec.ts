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

  // Supervisor serves the schema as a list of field descriptors, not as the
  // `name: validator` mapping config.yaml declares. Reading that list as a
  // mapping labelled every field by its array index and printed the raw
  // descriptor underneath it as help text.
  await expect(form(page)).not.toContainText('{"name":');
  await expect(form(page).locator("label.field .name").first()).not.toHaveText("0");

  // The raw file editor must not be offered in this mode.
  await expect(page.locator('button:text("+ Add section")')).toHaveCount(0);
});

test("an option type the form cannot edit does not break it", async ({ page }) => {
  // Supervisor sends a repeated option as a list. Parsing it as a validator
  // string threw inside render, which froze the page on its loading state —
  // and took every other tab's repaint with it.
  await page.goto(`${BASE_URL}#/config`);
  await expect(form(page)).toBeVisible();
  await expect(page.locator("body")).not.toContainText("Loading add-on options");

  const field = page.locator("label.field", { hasText: "Extra Hosts" });
  await expect(field).toContainText("Configuration page");
  await expect(field.locator("input")).toBeDisabled();

  // Saving must round-trip it untouched rather than flattening it to a string.
  await page.getByLabel("Grid prediction trust").fill("0.55");
  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner")).toContainText("Saved");
  expect(readAddonOptions(stack).extra_hosts).toEqual(["alpha", "beta"]);
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

test("the grid sensor is an entity picker listing only power entities", async ({
  page,
}) => {
  await page.goto(`${BASE_URL}#/config`);
  // Exact: the option takes one sensor per phase, so the field holds several
  // comboboxes and "Grid power sensor phase 2" would match a loose label.
  const input = page.getByLabel("Grid power sensor", { exact: true });
  await expect(input).toBeVisible();

  // The list is ours, not a native <datalist>: Safari on iOS implements none,
  // so on a phone this field had no discoverable suggestions at all.
  await expect(page.locator(".combo-list")).toHaveCount(0);
  await input.click();
  const list = page.locator(".combo-list").first();
  await expect(list).toBeVisible();
  const suggestions = await list
    .locator(".combo-id")
    .evaluateAll((els) => els.map((e) => e.textContent || ""));

  // Power sensors, including one with no device_class but a W/kW unit —
  // plenty of real installs look like that.
  expect(suggestions.join("|")).toContain("sensor.grid_power");
  expect(suggestions.join("|")).toContain("sensor.p1_meter_active_power");
  // Not a `sensor.`, but a working grid source all the same: readings are
  // fetched from /api/states/<id>, which is domain-agnostic.
  expect(suggestions.join("|")).toContain("number.verbrauch_15");
  // Every unit the meter converts, not just W and kW — the picker used to
  // carry its own shorter list and hid sensors that read perfectly.
  expect(suggestions.join("|")).toContain("sensor.substation_load");
  // Everything that could not be grid power stays out.
  expect(suggestions.join("|")).not.toContain("sensor.house_energy_today");
  expect(suggestions.join("|")).not.toContain("sensor.outside_temperature");
  expect(suggestions.join("|")).not.toContain("light.kitchen");

  // The chosen entity resolves to something a human recognises.
  await expect(
    page.locator(".entity-field", { hasText: "Grid power sensor" }),
  ).toContainText("Grid power — currently");
});

test("a suggestion can be typed for and picked", async ({ page }) => {
  await page.goto(`${BASE_URL}#/config`);
  const input = page.getByLabel("Grid power sensor", { exact: true });
  await input.click();
  await input.fill("verbrauch");

  const list = page.locator(".combo-list").first();
  await expect(list.locator(".combo-opt")).toHaveCount(1);
  await expect(list).toContainText("number.verbrauch_15");

  await list.locator(".combo-opt").first().click();
  await expect(input).toHaveValue("number.verbrauch_15");
  // Taken, so the list closes rather than sitting over the rest of the form.
  await expect(page.locator(".combo-list")).toHaveCount(0);

  // Nothing is saved, so leave the form as it was found.
  await page.reload();
});

test("the suggestion list is drivable from the keyboard", async ({ page }) => {
  await page.goto(`${BASE_URL}#/config`);
  const input = page.getByLabel("Grid power sensor", { exact: true });
  await input.click();
  const list = page.locator(".combo-list").first();
  await expect(list).toBeVisible();
  const selected = list.locator('.combo-opt[aria-selected="true"]');

  // Nothing is highlighted until asked for, so Enter on half-typed text keeps
  // the text rather than taking whatever happened to be first.
  await expect(selected).toHaveCount(0);
  await input.press("ArrowDown");
  await expect(selected).toHaveCount(1);

  // Past the end and back. The highlight has to move on the very next press:
  // when the index was a counter of its own rather than the row on screen, it
  // ran off the end and sat still for as many presses as it had overshot.
  const count = await list.locator(".combo-opt").count();
  for (let i = 0; i < count + 3; i++) await input.press("ArrowDown");
  const atEnd = (await selected.textContent()) || "";
  await input.press("ArrowUp");
  await expect(selected).not.toHaveText(atEnd);

  // Escape gives up on the list and leaves the field alone.
  const before = await input.inputValue();
  await input.press("Escape");
  await expect(page.locator(".combo-list")).toHaveCount(0);
  await expect(input).toHaveValue(before);

  // Enter takes the highlighted one.
  await input.click();
  await input.press("ArrowDown");
  await input.press("Enter");
  await expect(input).not.toHaveValue(before);
  await expect(page.locator(".combo-list")).toHaveCount(0);

  // Nothing saved; leave the form as it was found.
  await page.reload();
});

test("an entity Home Assistant does not know is flagged in place", async ({
  page,
}) => {
  await page.goto(`${BASE_URL}#/config`);
  const input = page.getByLabel("Grid power sensor", { exact: true });
  await expect(input).toBeVisible();
  await input.fill("sensor.definitely_not_real");
  await expect(
    page.locator(".entity-field", { hasText: "Grid power sensor" }),
  ).toContainText("Not found in Home Assistant right now.");
  await expect(input).toHaveClass(/warn-input/);
});

test("a sensor labelled power that reads energy is offered but marked", async ({
  page,
}) => {
  await page.goto(`${BASE_URL}#/config`);
  const input = page.getByLabel("Grid power sensor", { exact: true });
  await input.click();
  await input.fill("pv yield");

  // Still offered: hiding it makes the entity someone is hunting for simply
  // vanish, with nothing to say why.
  const list = page.locator(".combo-list").first();
  await expect(list).toContainText("sensor.pv_yield_total");
  await expect(list.locator(".combo-warn")).toHaveText("not a power unit");

  // Choosing it says what is wrong with it rather than looking resolved.
  await list.locator(".combo-opt").first().click();
  await expect(input).toHaveValue("sensor.pv_yield_total");
  await expect(input).toHaveClass(/warn-input/);
  await expect(
    page.locator(".entity-field", { hasText: "Grid power sensor" }),
  ).toContainText("AstraMeter cannot read it");

  // Nothing saved; leave the form as it was found.
  await page.reload();
});

test("a three-phase meter can be entered one sensor per phase", async ({ page }) => {
  // The option is a comma-separated list, one entity per phase. As a single
  // box it could not be filled in here at all: the picker overwrote the whole
  // string on the next selection, and the joined value read as one unknown
  // entity.
  await page.goto(`${BASE_URL}#/config`);
  await expect(form(page)).toBeVisible();

  // By role, not by label: each row's remove button is named after the row it
  // removes, so a label lookup would match both.
  const phase = (n: number) =>
    page.getByRole("combobox", { name: `Grid power sensor phase ${n}` });
  await expect(phase(2)).toBeVisible();
  await phase(2).fill("sensor.p1_meter_active_power");

  // Naming follows the count: with more than one, they are phases.
  await expect(phase(1)).toHaveValue("sensor.grid_power");
  await expect(phase(3)).toHaveValue("");
  await expect(phase(4)).toHaveCount(0);

  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner")).toContainText("Saved");
  expect(readAddonOptions(stack).power_input_alias).toBe(
    "sensor.grid_power, sensor.p1_meter_active_power",
  );

  // And back out again, leaving the whole-house sensor the rest of the run
  // expects.
  await page.reload();
  await page
    .getByRole("button", { name: "Remove Grid power sensor phase 2" })
    .click();
  await expect(page.getByLabel("Grid power sensor", { exact: true })).toHaveValue(
    "sensor.grid_power",
  );
  await page.locator('button:text("Save only")').click();
  await expect(page.locator(".banner")).toContainText("Saved");
  expect(readAddonOptions(stack).power_input_alias).toBe("sensor.grid_power");
});

test("switching to a config file materializes what is running", async ({ page }) => {
  await page.goto(`${BASE_URL}#/config`);
  const button = page.locator('button:text("Switch to a config file")');
  await expect(button).toBeVisible();
  await button.click();

  // Asked about first: it changes where every setting comes from and takes
  // the add-on off the air for a minute.
  const confirm = page.locator(".confirm");
  await expect(confirm).toContainText("/config/astrameter.ini");
  await expect(page.locator('button:text("Yes, switch and restart")')).toBeVisible();
  // Backing out leaves everything alone.
  await page.locator('button:text("Cancel")').click();
  await expect(page.locator(".confirm")).toHaveCount(0);
  expect(readAddonOptions(stack).custom_config).toBeFalsy();

  // Nor does it survive leaving the tab: coming back to change something else
  // must not land on a primed restart button.
  await button.click();
  await expect(page.locator(".confirm")).toBeVisible();
  await page.goto(`${BASE_URL}#/overview`);
  await page.goto(`${BASE_URL}#/config`);
  await expect(page.locator(".confirm")).toHaveCount(0);

  await button.click();
  await page.locator('button:text("Yes, switch and restart")').click();
  await expect(page.locator(".banner")).toContainText("Configuration mode changed");
  // The restart is deferred until after this response, so the call the page
  // made must have succeeded rather than dying with the container.
  await expect(page.locator(".banner.err")).toHaveCount(0);

  // The add-on is pointed at the new file...
  await expect
    .poll(() => readAddonOptions(stack).custom_config, { timeout: 15_000 })
    .toBe("astrameter.ini");
});
