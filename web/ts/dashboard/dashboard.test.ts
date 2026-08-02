// Tests for the dashboard's pure layers: formatting, the derived model, the
// vdom's escaping, and the views rendered to a string.
//
// Runs in plain Node — no DOM, no jsdom — because view() is a pure function
// of state. Same tiny ok() harness the rest of web/ts uses.

import { h, renderToString, escapeHtml } from "./vdom.js";
import {
  ago,
  batteryName,
  clockTime,
  macLabel,
  percent,
  phaseLabel,
  signedWatts,
  watts,
} from "./format.js";
import {
  allConsumers,
  batteryHealth,
  contribution,
  gridTotal,
  initialState,
  meterHealth,
  overallHealth,
  pollInterval,
  railScale,
  type AppState,
} from "./model.js";
import { parseAddonSchema } from "./option-meta.js";
import { view } from "./view.js";
import { initialConfigState, knownKeys, specFor } from "./config-view.js";
import type { StatusSnapshot } from "./types.js";
import { readFileSync } from "node:fs";

let failures = 0;
function ok(cond: boolean, msg: string) {
  if (!cond) {
    failures++;
    console.error("✗ " + msg);
  }
}
function has(haystack: string, needle: string, msg?: string) {
  ok(haystack.includes(needle), msg || `expected to find: ${JSON.stringify(needle)}`);
}
function lacks(haystack: string, needle: string, msg?: string) {
  ok(
    !haystack.includes(needle),
    msg || `expected NOT to find: ${JSON.stringify(needle)}`,
  );
}

// ── formatting: absence is never a number ──
ok(watts(undefined) === null, "watts(undefined) is null, not 0");
ok(watts(null) === null, "watts(null) is null");
ok(watts(0) === "0 W", "watts(0) is a real zero");
ok(watts(1500) === "1.50 kW", "watts scales to kW");
ok(signedWatts(-412) === "−412 W", "signedWatts uses a real minus sign");
ok(signedWatts(412) === "+412 W", "signedWatts marks import");
ok(signedWatts(0) === "0 W", "signed zero carries no sign");
ok(percent(0.42) === "42%", "percent scales");
ok(percent(undefined) === null, "percent(undefined) is null");
ok(ago(4) === "4 s ago", "ago formats seconds");
ok(ago(undefined) === null, "ago(undefined) is null");
ok(phaseLabel("A") === "Phase A", "phase label matches HA vocabulary");
ok(phaseLabel("D") === "All phases", "combined phase is spelled out");
ok(phaseLabel(undefined) === null, "absent phase is null");
ok(macLabel("02b250000001") === "02:B2:50:00:00:01", "MAC is grouped");
ok(macLabel("nope") === "nope", "a non-MAC id passes through");
ok(
  batteryName("02b250000001", "HMK-2") === "HMK-2 ·0001",
  "battery name keeps the model and the last four",
);
ok(clockTime(undefined) === null, "clockTime(undefined) is null");
ok(clockTime("not a date") === null, "clockTime rejects garbage");

// ── vdom escaping: backend data can never become markup ──
ok(
  escapeHtml('<img src=x onerror="a">') ===
    "&lt;img src=x onerror=&quot;a&quot;&gt;",
  "escapeHtml neutralises tags and quotes",
);
const hostile = renderToString(
  h("div", { title: '"><script>alert(1)</script>' }, "<b>not bold</b>"),
);
lacks(hostile, "<script>", "a hostile attribute cannot open a tag");
lacks(hostile, "<b>", "hostile text is escaped, not rendered");
ok(
  renderToString(h("input", { type: "text", disabled: true })) ===
    '<input type="text" disabled>',
  "void elements self-close and boolean attrs collapse",
);
ok(
  renderToString(h("div", { onclick: () => {} }, "x")) === "<div>x</div>",
  "event handlers never reach the HTML",
);
ok(renderToString(null) === "", "null renders as nothing");
ok(renderToString(h("p", null, false, "kept")) === "<p>kept</p>", "false is dropped");

// ── model ──
const snapshot: StatusSnapshot = {
  schema_version: 1,
  generated_at: "2026-08-01T12:00:00+00:00",
  capabilities: { config_mode: "standalone", controls: true, poll_interval_ms: 2000 },
  service: { version: "2.2.4", runtime: "docker" },
  powermeters: [
    { name: "JSON_HTTP", kind: "JsonHttpPowermeter", last_read_ok: true, last_total_w: 240 },
  ],
  devices: [
    {
      kind: "ct002",
      device_id: "ct-1",
      ct_type: "HME-4",
      udp_port: 12345,
      running: true,
      control: { active_control: true },
      grid: { l1_w: 12, l2_w: -30, l3_w: 5, grid_total_w: -13 },
      consumers: [
        {
          consumer_id: "02b250000001",
          device_type: "HMK-2",
          phase: "A",
          reported_power_w: -320,
          last_seen_age_s: 0.4,
          active: true,
          balancer: { saturation: 0.12, last_target_w: -330 },
        },
        {
          consumer_id: "02b250000002",
          phase: "B",
          reported_power_w: 210,
          expired: true,
        },
      ],
    },
  ],
};

ok(gridTotal(snapshot) === -13, "grid total comes from the device");
ok(gridTotal(null) === undefined, "grid total is absent without a snapshot");
ok(allConsumers(snapshot).length === 2, "consumers are flattened across devices");
ok(
  contribution({ reported_power_w: 320 }) === -320,
  "a discharging battery contributes negatively to the grid",
);
ok(contribution({}) === undefined, "no report means no contribution");
ok(railScale(-13, allConsumers(snapshot)) === 500, "rail scale rounds up past the peak");
ok(railScale(undefined, []) === 100, "rail scale has a floor");
ok(pollInterval(snapshot) === 2000, "poll interval follows the backend");
ok(
  pollInterval({ ...snapshot, capabilities: { poll_interval_ms: 1 } }) === 500,
  "an absurd poll interval is clamped",
);

ok(batteryHealth({ expired: true }).severity === "err", "an expired battery is an error");
ok(batteryHealth({ active: false }).severity === "idle", "a disabled battery is idle");
ok(batteryHealth({ manual_enabled: true }).severity === "warn", "manual is a warning");
ok(
  batteryHealth({ balancer: { saturation: 0.9 } }).severity === "warn",
  "a saturated battery warns",
);
ok(batteryHealth({}).severity === "ok", "a plain reporting battery is ok");
ok(
  batteryHealth({}).glyph !== batteryHealth({ expired: true }).glyph,
  "severity is distinguishable without colour",
);

ok(meterHealth({ online: false }).severity === "err", "an offline meter is an error");
ok(
  meterHealth({ last_read_age_s: 120 }).severity === "warn",
  "a quiet meter is stale before it is offline",
);
ok(
  meterHealth({ online: null as any }).severity === "idle",
  "a pull meter's unknown liveness is not a failure",
);

// ── overall health drives the one-line answer ──
const live: AppState = { ...initialState(), snapshot, connection: "live" };
// The fixture deliberately holds one expired battery, so the honest summary
// is a warning rather than "all good".
ok(
  overallHealth(live).label === "Battery missing",
  "an expired battery is surfaced in the headline health",
);
const allWell: AppState = {
  ...live,
  snapshot: {
    ...snapshot,
    devices: [
      { ...snapshot.devices![0], consumers: [snapshot.devices![0].consumers![0]] },
    ],
  },
};
ok(overallHealth(allWell).label === "Steering", "a healthy system reports Steering");
ok(
  overallHealth({ ...live, connection: "offline" }).severity === "err",
  "a dropped connection outranks everything",
);
ok(
  overallHealth({ ...live, snapshot: { ...snapshot, devices: [] } }).label ===
    "Starting up",
  "no devices yet means starting up",
);

// ── views render, and omit what they were not given ──
const actions: any = new Proxy({}, { get: () => () => {} });
const html = renderToString(
  h("div", null, ...view(live, actions, initialConfigState())),
);
has(html, "−13 W", "the hero shows the grid total");
has(html, "effect on the grid", "the rail states which frame the battery bars use");
has(html, "exporting to the grid", "the hero states the direction in words");
has(html, "HMK-2 ·0001", "a battery appears by name");
has(html, "Battery missing", "the health chip is rendered");
lacks(html, "undefined", "no undefined leaks into the page");
lacks(html, "NaN", "no NaN leaks into the page");
lacks(html, "[object Object]", "no object stringification leaks into the page");

// A near-empty snapshot must still render a complete page: this is the
// property that lets a future ESPHome backend serve a fraction of the
// document without the UI turning into a grid of dashes.
const minimal: StatusSnapshot = {
  schema_version: 1,
  generated_at: "2026-08-01T12:00:00+00:00",
  capabilities: {},
};
const minimalHtml = renderToString(
  h("div", null, ...view({ ...initialState(), snapshot: minimal, connection: "live" }, actions, initialConfigState())),
);
has(minimalHtml, "AstraMeter", "the shell renders with almost no data");
has(minimalHtml, "No batteries have reported yet", "an empty state explains itself");
lacks(minimalHtml, "undefined", "absent fields do not print undefined");

// Offline swaps every relative age for an absolute clock time, because a
// frozen "0.4 s ago" is indistinguishable from a fresh one.
const offlineHtml = renderToString(
  h("div", null, ...view({ ...live, connection: "offline" }, actions, initialConfigState())),
);
has(offlineHtml, "Lost contact with AstraMeter", "the offline banner appears");
lacks(offlineHtml, "0.4 s ago", "relative ages are withdrawn while offline");

// Controls are gated on the capability, never on backend identity.
const readOnly: AppState = {
  ...live,
  snapshot: { ...snapshot, capabilities: { ...snapshot.capabilities, controls: false } },
  tab: "batteries",
};
const readOnlyHtml = renderToString(
  h("div", null, ...view(readOnly, actions, initialConfigState())),
);
lacks(readOnlyHtml, ">Controls<", "no write controls without the capability");
const writable = renderToString(
  h("div", null, ...view({ ...live, tab: "batteries" }, actions, initialConfigState())),
);
has(writable, ">Controls<", "write controls appear with the capability");

// ── add-on schema parsing ──
const f = parseAddonSchema("float(0,1)?");
ok(f.type === "float" && f.min === 0 && f.max === 1 && f.optional, "float(0,1)? parses");
const l = parseAddonSchema("list(critical|error|info)");
ok(l.type === "list" && l.options?.length === 3 && !l.optional, "list(...) parses");
ok(parseAddonSchema("password?").type === "password", "password? parses");
ok(parseAddonSchema("int(0,)?").min === 0, "int(0,) parses an open upper bound");
ok(parseAddonSchema("").type === "str", "an empty spec falls back to text");
ok(parseAddonSchema("weird_type").type === "weird_type", "unknown types survive");

// ── config.ini editor: typed controls from the backend's key metadata ──
const KEY_TYPES = {
  GENERAL: {
    DEVICE_TYPE: { type: "select", options: ["ct002", "ct003"] },
    DASHBOARD_ENABLED: { type: "boolean" },
    WEB_SERVER_PORT: { type: "integer" },
  },
  SHELLY: { PASS: { type: "password" }, IP: {} },
  CT002: { BALANCE_GAIN: { type: "float", min: 0, max: 1 } },
} as any;

ok(specFor(KEY_TYPES, "GENERAL", "DEVICE_TYPE").type === "select", "exact section match");
ok(specFor(KEY_TYPES, "general", "device_type").type === "select", "section/key match is case-insensitive");
// A second meter of the same kind is configured as [SHELLY_2]; it must inherit
// the SHELLY types rather than fall back to plain text.
ok(specFor(KEY_TYPES, "SHELLY_2", "PASS").type === "password", "suffixed section inherits its base types");
ok(specFor(KEY_TYPES, "UNKNOWN", "FOO").type === undefined, "unknown key falls back to text");
ok(knownKeys(KEY_TYPES, "SHELLY_BACK").includes("IP"), "known keys resolve through a suffix");
ok(knownKeys(KEY_TYPES, "NOPE").length === 0, "no suggestions for an unknown section");

const iniConfig = {
  ...initialConfigState(),
  iniLoaded: true,
  keyTypes: KEY_TYPES,
  order: ["GENERAL", "SHELLY"],
  sections: {
    GENERAL: { DEVICE_TYPE: "ct002", DASHBOARD_ENABLED: "True", WEB_SERVER_PORT: "52500" },
    SHELLY: { IP: "192.168.1.50", PASS: "••••••••" },
  },
};
const iniHtml = renderToString(
  h("div", null, ...view({ ...live, tab: "config" }, actions, iniConfig)),
);
has(iniHtml, "[GENERAL]", "the editor renders a card per section");
has(iniHtml, "[SHELLY]", "every section appears");
has(iniHtml, '<select', "a select-typed key renders a dropdown");
has(iniHtml, 'type="password"', "a password-typed key renders masked");
// The backend redacts by key name in ANY section, so an unknown section's
// secret must not be shown in a visible text box.
{
  const odd = {
    ...iniConfig,
    order: ["MYSTERY"],
    sections: { MYSTERY: { ACCESSTOKEN: "••••••••", NOTE: "hello" } },
  };
  const oddHtml = renderToString(
    h("div", null, ...view({ ...live, tab: "config" }, actions, odd)),
  );
  has(oddHtml, 'type="password"', "a secret key is masked even in an unknown section");
  has(oddHtml, 'aria-label="NOTE value"', "ordinary keys stay text");
}
has(iniHtml, 'type="number"', "an integer-typed key renders a number input");
has(iniHtml, "+ Add setting", "keys can be added");
has(iniHtml, "Remove section", "sections can be removed");
has(iniHtml, "<datalist", "known keys are offered as suggestions");
has(iniHtml, 'aria-label="Setting name: DEVICE_TYPE"', "each key input names its own key");
has(iniHtml, 'aria-label="DEVICE_TYPE value"', "each value control names its key");
lacks(iniHtml, "<textarea", "the raw text box is gone");

// An unrecognised stored value must stay selectable, or merely opening the
// editor would silently rewrite it to the first option.
const oddValue = {
  ...iniConfig,
  sections: { GENERAL: { DEVICE_TYPE: "ct999" } },
  order: ["GENERAL"],
};
const oddHtml = renderToString(
  h("div", null, ...view({ ...live, tab: "config" }, actions, oddValue)),
);
has(oddHtml, "ct999 (current)", "an unknown select value is preserved");

const emptyIni = { ...iniConfig, sections: {}, order: [] };
const emptyHtml = renderToString(
  h("div", null, ...view({ ...live, tab: "config" }, actions, emptyIni)),
);
has(emptyHtml, "This config file is empty", "an empty file explains itself");

// ── Home Assistant entity picker ──
const HA_SCHEMA = { power_input_alias: "str", device_types: "str" };
const withEntities = {
  ...initialConfigState(),
  loadedMode: "ha_simple",
  schema: HA_SCHEMA,
  options: { power_input_alias: "sensor.current_power_in", device_types: "ct002" },
  entitiesLoaded: true,
  entities: [
    { entity_id: "sensor.current_power_in", name: "Grid power", unit: "W", state: "412.8" },
    { entity_id: "sensor.p1_meter", name: "P1 meter", unit: "kW", state: "1.24" },
  ],
};
const haState: AppState = {
  ...live,
  tab: "config",
  snapshot: { ...snapshot, capabilities: { ...snapshot.capabilities, config_mode: "ha_simple", ha_options: true } },
};
const pickerHtml = renderToString(h("div", null, ...view(haState, actions, withEntities)));
has(pickerHtml, 'list="ha-entities-power_input_alias"', "the sensor field is a combobox");
has(pickerHtml, 'value="sensor.p1_meter"', "every applicable sensor is offered");
has(pickerHtml, "Grid power · 412.8 W", "suggestions show the friendly name and live value");
has(pickerHtml, "Grid power — currently 412.8 W", "the chosen sensor resolves to a readable line");
has(pickerHtml, "Switch to a config file", "the mode switch is offered in the add-on");

// A configured entity Home Assistant does not know must be called out — a
// typo here otherwise only surfaces as a start-up failure much later.
const typo = { ...withEntities, options: { ...withEntities.options, power_input_alias: "sensor.nope" } };
const typoHtml = renderToString(h("div", null, ...view(haState, actions, typo)));
has(typoHtml, "Not found in Home Assistant right now.", "an unknown entity is flagged");
has(typoHtml, "warn-input", "and the field is visually marked");

// Before the lookup returns, nothing is claimed either way.
const pending = { ...withEntities, entitiesLoaded: false, entities: [] };
const pendingHtml = renderToString(h("div", null, ...view(haState, actions, pending)));
lacks(pendingHtml, "Not found in Home Assistant", "no false alarm before the list loads");

// The lookup is best-effort: with no entities the field still accepts typing.
const noEntities = { ...withEntities, entities: [] };
const noneHtml = renderToString(h("div", null, ...view(haState, actions, noEntities)));
has(noneHtml, "No power sensors found", "an empty list explains itself");
has(noneHtml, 'aria-label="Grid power sensor"', "the field is still usable");

// Simple mode must never offer the raw file editor.
lacks(pickerHtml, "+ Add section", "the INI editor is hidden in guided mode");

// ── live controls mirror the MQTT control surface ──
const controllable: StatusSnapshot = {
  ...snapshot,
  devices: [
    {
      ...snapshot.devices![0],
      balancer: { efficiency_rotation_enabled: true },
      consumers: [
        {
          ...snapshot.devices![0].consumers![0],
          manual_enabled: true,
          manual_target_w: -250,
          distribution_weight: 1.5,
          efficiency_window_weight_pct: 60,
          min_dc_output_w: 80,
          min_dc_output_applicable: true,
        },
      ],
    },
  ],
};
const ctrlState: AppState = { ...live, tab: "batteries", snapshot: controllable };
const ctrlHtml = renderToString(h("div", null, ...view(ctrlState, actions, initialConfigState())));
has(ctrlHtml, 'aria-label="Manual target"', "manual target is offered");
has(ctrlHtml, 'aria-label="Distribution weight"', "distribution weight is offered");
has(ctrlHtml, 'aria-label="Efficiency window"', "efficiency window is offered");
has(ctrlHtml, 'aria-label="Min DC output"', "min DC output is offered");
has(ctrlHtml, ">Active<", "the active switch is offered");
has(ctrlHtml, "Automatic target", "the auto/manual switch is offered");
// Ranges must match the MQTT entities exactly, or a value valid in one
// surface is rejected by the other.
has(ctrlHtml, 'min="0" max="10" step="0.1"', "distribution weight keeps the MQTT range");
has(ctrlHtml, 'min="0" max="100" step="5"', "efficiency window keeps the MQTT range");
has(ctrlHtml, 'min="0" max="1000" step="1"', "min DC output keeps the MQTT range");
has(ctrlHtml, "60%", "the slider shows its value");

// Conditional visibility must match when the MQTT entity is published.
const noRotation: StatusSnapshot = {
  ...controllable,
  devices: [{ ...controllable.devices![0], balancer: { efficiency_rotation_enabled: false } }],
};
const noRotHtml = renderToString(
  h("div", null, ...view({ ...ctrlState, snapshot: noRotation }, actions, initialConfigState())),
);
lacks(noRotHtml, 'aria-label="Efficiency window"', "no efficiency window without rotation");

const acOnly: StatusSnapshot = {
  ...controllable,
  devices: [
    {
      ...controllable.devices![0],
      consumers: [{ ...controllable.devices![0].consumers![0], min_dc_output_applicable: false }],
    },
  ],
};
const acHtml = renderToString(
  h("div", null, ...view({ ...ctrlState, snapshot: acOnly }, actions, initialConfigState())),
);
lacks(acHtml, 'aria-label="Min DC output"', "no DC floor on an AC-only battery");

// A battery on automatic must not show a manual setpoint box.
const manual: StatusSnapshot = {
  ...controllable,
  devices: [
    {
      ...controllable.devices![0],
      consumers: [{ ...controllable.devices![0].consumers![0], manual_enabled: false }],
    },
  ],
};
const autoHtml = renderToString(
  h("div", null, ...view({ ...ctrlState, snapshot: manual }, actions, initialConfigState())),
);
lacks(autoHtml, 'aria-label="Manual target"', "no manual box while on automatic");

// Device-wide: the Active Control switch and Force Rotation button.
const overviewHtml = renderToString(
  h("div", null, ...view({ ...ctrlState, tab: "overview" }, actions, initialConfigState())),
);
has(overviewHtml, "Force rotation", "force rotation is reachable");
has(overviewHtml, "Active control", "active control is reachable");

// Read-only deployments must offer none of it.
const ro: AppState = {
  ...ctrlState,
  snapshot: { ...controllable, capabilities: { ...controllable.capabilities, controls: false } },
};
const roHtml = renderToString(h("div", null, ...view(ro, actions, initialConfigState())));
lacks(roHtml, 'aria-label="Distribution weight"', "no controls without the capability");
const roOverview = renderToString(
  h("div", null, ...view({ ...ro, tab: "overview" }, actions, initialConfigState())),
);
lacks(roOverview, "Force rotation", "no device controls without the capability");

// ── the vdom must not clobber user-owned UI state ──
// Regression: a <details> the user opened was slammed shut by the next 1 Hz
// re-render, because the attribute pruning removed the `open` the *browser*
// had set. That made every collapsed control panel impossible to use.
ok(
  renderToString(h("details", { class: "x" }, h("summary", null, "s"))) ===
    '<details class="x"><summary>s</summary></details>',
  "a details with no open prop renders closed",
);
ok(
  renderToString(h("details", { open: true }, h("summary", null, "s"))) ===
    "<details open><summary>s</summary></details>",
  "open is a create-time default",
);
{
  const src = readFileSync(new URL("./vdom.ts", import.meta.url), "utf8");
  ok(src.includes("UNCONTROLLED"), "vdom keeps an uncontrolled-attribute set");
  ok(
    /UNCONTROLLED\.has\(attr\.name\)/.test(src),
    "the prune loop skips uncontrolled attributes",
  );
  // `.innerHTML` as a property access — the header comment mentions the
  // word, so a bare substring search would match the prose instead.
  ok(!/\.innerHTML/.test(src), "vdom never writes .innerHTML");
}

if (failures) {
  console.error(`\n${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("dashboard.test.ts: ALL PASSED");
