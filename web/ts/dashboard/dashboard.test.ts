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
  hasReported,
  initialState,
  meterHealth,
  overallHealth,
  pollInterval,
  railScale,
  reportingConsumers,
  type AppState,
} from "./model.js";
import { normalizeAddonSchema, parseAddonSchema } from "./option-meta.js";
import { view } from "./view.js";
import {
  entityList,
  initialConfigState,
  knownKeys,
  matchEntities,
  specFor,
} from "./config-view.js";
import {
  GRID_SERIES,
  HISTORY_LIMIT,
  batterySeries,
  meterSeries,
  recordSnapshot,
  seriesOf,
  sparkGeometry,
  type SeriesHistory,
} from "./history.js";
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

// "Answered every" is redundant while it equals the poll interval, so it only
// earns a row when a dedupe window is actually holding replies back.
lacks(writable, "Answered every", "no answer-rate row when nothing is deduped");
const dedupedState: AppState = {
  ...live,
  tab: "batteries",
  snapshot: {
    ...snapshot,
    devices: snapshot.devices.map((d) =>
      d.kind === "ct002"
        ? {
            ...d,
            consumers: (d.consumers ?? []).map((c) => ({
              ...c,
              poll_interval_s: 0.5,
              answer_interval_s: 4,
            })),
          }
        : d,
    ),
  },
};
const dedupedHtml = renderToString(
  h("div", null, ...view(dedupedState, actions, initialConfigState())),
);
has(dedupedHtml, "Answered every", "the answer rate shows once it diverges from the poll rate");
has(dedupedHtml, ">4 s<", "and reports the answered cadence, not the poll cadence");

// ── the Shelly emulator, which is the default DEVICE_TYPE ──
//
// Regression: the views filtered on kind === "ct002", so a Shelly install
// rendered no device card, an empty hero and "no batteries have reported"
// while batteries were actively polling it.
const shellySnapshot: StatusSnapshot = {
  schema_version: 1,
  generated_at: "2026-08-01T12:00:00+00:00",
  capabilities: { controls: true },
  powermeters: [
    { name: "JSON_HTTP", kind: "JsonHttpPowermeter", last_read_ok: true, last_total_w: 848.6 },
  ],
  devices: [
    {
      kind: "shelly",
      device_id: "shellypro3em-ec4609c439c1",
      device_type: "shellypro3em_new",
      udp_port: 2220,
      running: true,
      inactive_timeout_s: 120,
      batteries: [
        { ip: "10.0.0.31", last_seen_age_s: 2.1, poll_interval_s: 1.0, active: true },
        { ip: "10.0.0.32", last_seen_age_s: 3.4, poll_interval_s: 1.1, active: true },
      ],
    },
  ],
};
const shellyState: AppState = {
  ...initialState(),
  snapshot: shellySnapshot,
  connection: "live",
};

ok(
  gridTotal(shellySnapshot) === 848.6,
  "with no CT device the hero falls back to the power source total",
);
ok(
  overallHealth(shellyState).label === "Serving readings",
  "a polled Shelly emulator is healthy, and does not claim to be steering",
);

const shellyHtml = renderToString(
  h("div", null, ...view(shellyState, actions, initialConfigState())),
);
has(shellyHtml, "shellypro3em_new emulator", "the Shelly emulator gets its own card");
has(shellyHtml, "Batteries polling", "the card says how many batteries poll it");
has(shellyHtml, "+849 W", "the hero shows the grid reading rather than a dash");
lacks(shellyHtml, "Waiting for a grid reading", "the hero is not left empty");
lacks(shellyHtml, "No batteries have reported yet", "polling batteries are not called absent");
lacks(shellyHtml, "undefined", "no undefined leaks into the Shelly page");

const shellyBatteriesHtml = renderToString(
  h("div", null, ...view({ ...shellyState, tab: "batteries" }, actions, initialConfigState())),
);
has(shellyBatteriesHtml, "10.0.0.31", "each polling battery is listed by address");
has(shellyBatteriesHtml, "Polls every", "with the cadence it polls at");
// A Shelly emulator steers nothing, so offering per-battery controls would
// promise writes the device cannot make.
lacks(shellyBatteriesHtml, ">Controls<", "no steering controls for a Shelly battery");

// The empty state must name the meter the user is actually emulating.
const shellyEmpty = renderToString(
  h("div", null, ...view(
    { ...shellyState, snapshot: { ...shellySnapshot, devices: [{ kind: "shelly", batteries: [] }] } },
    actions,
    initialConfigState(),
  )),
);
has(shellyEmpty, "select this Shelly meter", "the empty state names the Shelly meter");
lacks(shellyEmpty, "AstraMeter CT as its meter", "it does not send them looking for a CT");

// ── add-on schema parsing ──
const f = parseAddonSchema("float(0,1)?");
ok(f.type === "float" && f.min === 0 && f.max === 1 && f.optional, "float(0,1)? parses");
const l = parseAddonSchema("list(critical|error|info)");
ok(l.type === "list" && l.options?.length === 3 && !l.optional, "list(...) parses");
ok(parseAddonSchema("password?").type === "password", "password? parses");
ok(parseAddonSchema("int(0,)?").min === 0, "int(0,) parses an open upper bound");
ok(parseAddonSchema("").type === "str", "an empty spec falls back to text");
ok(parseAddonSchema("weird_type").type === "weird_type", "unknown types survive");
// The schema is Supervisor's, not ours: a repeated option arrives as a list
// and a nested one as an object. Assuming a string here threw inside render,
// which froze the whole page — not just the field — on "Loading add-on
// options…" forever.
for (const spec of [["str"], { inner: "str" }, 5, true] as unknown[]) {
  const parsed = parseAddonSchema(spec);
  ok(parsed.type === "unsupported", `a ${typeof spec} spec does not throw`);
  ok(Boolean(parsed.unsupported), "and it carries the raw shape for the UI");
}
ok(parseAddonSchema(null).type === "str", "an absent spec still falls back to text");
ok(parseAddonSchema(undefined).type === "str", "so does an undefined one");

// What Supervisor actually puts on the wire. The `name: validator` mapping an
// add-on declares in config.yaml is rendered into a *list* of field
// descriptors before any dashboard sees it; reading that list as a mapping
// keyed the whole form by array index, so every control came out labelled
// 0, 1, 2 with the descriptor printed underneath it.
const SUPERVISOR_SCHEMA: unknown[] = [
  { name: "power_input_alias", required: true, type: "string" },
  {
    name: "grid_predict_trust",
    lengthMin: 0,
    lengthMax: 1,
    optional: true,
    type: "float",
  },
  { name: "efficiency_rotation_interval", lengthMin: 1, optional: true, type: "integer" },
  { name: "active_control", optional: true, type: "boolean" },
  { name: "marstek_password", optional: true, type: "string", format: "password" },
  {
    name: "log_level",
    required: true,
    type: "select",
    options: ["critical", "error", "info"],
  },
  { name: "extra_hosts", multiple: true, required: true, type: "string" },
  { name: "ports", type: "schema", optional: true, multiple: false, schema: [] },
];
const rendered = normalizeAddonSchema(SUPERVISOR_SCHEMA);
ok(rendered.power_input_alias.type === "str", "a descriptor list keys by option name");
ok(!("0" in rendered), "and never by array index");
const trust = rendered.grid_predict_trust;
ok(
  trust.type === "float" && trust.min === 0 && trust.max === 1 && trust.optional,
  "lengthMin/lengthMax become the bounds of a number",
);
ok(rendered.efficiency_rotation_interval.type === "int", "integer is a number control");
ok(rendered.active_control.type === "bool", "boolean is a checkbox");
ok(rendered.marstek_password.type === "password", "a password keeps its masking");
ok(
  rendered.log_level.type === "list" && rendered.log_level.options?.length === 3,
  "a select carries its own options",
);
ok(!rendered.power_input_alias.optional, "a required option is marked required");
ok(rendered.extra_hosts.type === "unsupported", "a repeated option is not edited here");
ok(rendered.ports.type === "unsupported", "nor is a nested block");
// Whatever else arrives, the form must render — a throw here froze the page.
for (const junk of [null, undefined, 42, "str", [1, "two", { noName: 1 }]] as unknown[]) {
  ok(Object.keys(normalizeAddonSchema(junk)).length === 0, "a fieldless shape is empty");
}
// The add-on's own declared form still parses: it is what config.yaml holds.
ok(normalizeAddonSchema({ ct_mac: "str?" }).ct_mac.optional, "the mapping form parses");

// The field renders, read-only: a text box over a list would write a string
// back and flatten the real value on save.
{
  const state: AppState = {
    ...initialState(),
    snapshot: {
      schema_version: 1,
      generated_at: "2026-08-01T12:00:00+00:00",
      capabilities: { config_mode: "ha_simple", ha_options: true, controls: true },
    },
    connection: "live",
    tab: "config",
  };
  const cfg = initialConfigState();
  cfg.loadedMode = "ha_simple";
  cfg.options = {
    power_input_alias: "sensor.grid",
    grid_predict_trust: 0.5,
    extra_hosts: ["a", "b"],
  };
  cfg.schema = rendered;
  const html = renderToString(h("div", null, ...view(state, actions, cfg)));
  has(html, "Grid power sensor", "a descriptor-built field gets its own label");
  has(html, "Grid measurement", "and lands in its group, not a numbered pile");
  has(html, 'max="1"', "the float's bounds reach the control");
  has(html, 'type="password"', "the secret is masked");
  has(html, "Extra Hosts", "the unsupported option is still shown");
  has(html, "disabled", "but not editable here");
  has(html, "Configuration page", "and it says where to edit it");
  lacks(html, "undefined", "no undefined leaks into the guided form");
}

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
const HA_SCHEMA = normalizeAddonSchema([
  { name: "power_input_alias", required: true, type: "string" },
  { name: "device_types", required: true, type: "string" },
]);
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
has(pickerHtml, 'role="combobox"', "the sensor field is a combobox");
has(pickerHtml, "Grid power — currently 412.8 W", "the chosen sensor resolves to a readable line");
has(pickerHtml, "Switch to a config file", "the mode switch is offered in the add-on");
// Closed until the field is focused, so a form of them is not a wall of lists.
lacks(pickerHtml, "combo-list", "the suggestions stay closed until asked for");
has(pickerHtml, 'aria-expanded="false"', "and the field says so");

// The suggestions are ours, not a native <datalist>: Safari on iOS does not
// implement one, so on a phone the field was a plain text box with no way to
// discover an entity id at all.
lacks(pickerHtml, "<datalist", "no datalist is relied on");
const openPicker = { ...withEntities, openPicker: "power_input_alias:0", pickerIndex: -1 };
const openHtml = renderToString(h("div", null, ...view(haState, actions, openPicker)));
has(openHtml, 'role="listbox"', "the open field draws its own list");
has(openHtml, 'aria-expanded="true"', "and says it is open");
has(openHtml, "sensor.p1_meter", "every applicable sensor is offered");
has(openHtml, "P1 meter", "by the name a human recognises");
has(openHtml, "1.24 kW", "with what it currently reads");

// Filtering is on the id and the name, since a user knows one or the other.
ok(
  matchEntities(withEntities.entities, "p1").length === 1,
  "typing filters the suggestions by entity id",
);
ok(
  matchEntities(withEntities.entities, "grid POWER")[0]?.entity_id ===
    "sensor.current_power_in",
  "and by friendly name, case-insensitively",
);
ok(matchEntities(withEntities.entities, "").length === 2, "an empty box offers everything");
// A row already holding a full id keeps the whole list, so the choice can
// still be changed without clearing the box first.
ok(
  matchEntities(withEntities.entities, "sensor.current_power_in").length === 2,
  "an exact match does not filter the list down to itself",
);
ok(matchEntities(withEntities.entities, "nope").length === 0, "a miss offers nothing");
const noMatch = { ...openPicker, options: { ...withEntities.options, power_input_alias: "zzz" } };
has(
  renderToString(h("div", null, ...view(haState, actions, noMatch))),
  "No power sensor matches",
  "and says so rather than showing an empty box",
);

// A configured entity Home Assistant does not know must be called out — a
// typo here otherwise only surfaces as a start-up failure much later.
const typo = { ...withEntities, options: { ...withEntities.options, power_input_alias: "sensor.nope" } };
const typoHtml = renderToString(h("div", null, ...view(haState, actions, typo)));
has(typoHtml, "Not found in Home Assistant right now.", "an unknown entity is flagged");
has(typoHtml, "warn-input", "and the field is visually marked");

// An entity that claims `device_class: power` but reads in something the
// powermeter refuses is offered — hiding it makes the sensor a user is hunting
// for simply vanish — but marked, because every read of it would fail.
const mislabelled = {
  ...withEntities,
  openPicker: "power_input_alias:0",
  options: { ...withEntities.options, power_input_alias: "sensor.house_energy" },
  entities: [
    ...withEntities.entities,
    {
      entity_id: "sensor.house_energy",
      name: "House energy",
      unit: "kWh",
      device_class: "power",
      state: "1284.5",
      readable: false,
    },
  ],
};
const mislabelledHtml = renderToString(
  h("div", null, ...view(haState, actions, mislabelled)),
);
has(mislabelledHtml, "sensor.house_energy", "a mislabelled sensor is still offered");
has(mislabelledHtml, "not a power unit", "but the suggestion is marked");
has(mislabelledHtml, "AstraMeter cannot read it", "and choosing it explains why");
has(mislabelledHtml, "warn-input", "with the field marked as it is for a typo");
// The good one next to it must not be tarred with the same brush.
lacks(
  renderToString(h("div", null, ...view(haState, actions, withEntities))),
  "not a power unit",
  "a readable sensor carries no warning",
);

// Before the lookup returns, nothing is claimed either way.
const pending = { ...withEntities, entitiesLoaded: false, entities: [] };
const pendingHtml = renderToString(h("div", null, ...view(haState, actions, pending)));
lacks(pendingHtml, "Not found in Home Assistant", "no false alarm before the list loads");

// The lookup is best-effort: with no entities the field still accepts typing.
const noEntities = { ...withEntities, entities: [] };
const noneHtml = renderToString(h("div", null, ...view(haState, actions, noEntities)));
has(noneHtml, "No power sensors found", "an empty list explains itself");
has(noneHtml, 'aria-label="Grid power sensor"', "the field is still usable");

// ── one sensor per phase ──
// The option is a comma-separated list: one entity for a whole-house total,
// or one per phase. Rendered as a single box, a three-phase value showed as
// one unreadable string that the picker overwrote on the next selection.
ok(
  entityList("sensor.l1, sensor.l2 ,sensor.l3").join("|") ===
    "sensor.l1|sensor.l2|sensor.l3",
  "a per-phase option parses into one entity per phase",
);
ok(entityList("").length === 0, "an unset option holds nothing");
ok(entityList("sensor.a, ").length === 1, "the empty row this control adds is not a phase");
// A cleared middle row keeps its place until the edit ends — compacting it
// mid-edit would slide the next phase up under the user's cursor.
ok(entityList("sensor.a, , sensor.c").length === 3, "a cleared row holds its position");

const threePhase = {
  ...withEntities,
  schema: normalizeAddonSchema([
    { name: "power_input_alias", required: true, type: "string" },
    { name: "power_output_alias", optional: true, type: "string" },
  ]),
  options: { power_input_alias: "sensor.current_power_in, sensor.p1_meter" },
};
const phaseHtml = renderToString(h("div", null, ...view(haState, actions, threePhase)));
has(phaseHtml, 'value="sensor.current_power_in"', "phase 1 gets its own box");
has(phaseHtml, 'aria-label="Grid power sensor phase 2"', "and phase 2 is named as one");
has(phaseHtml, 'aria-label="Grid power sensor phase 3"', "with an empty box for the third");
has(phaseHtml, "Grid power — currently 412.8 W", "each row resolves on its own");
has(phaseHtml, "P1 meter — currently 1.24 kW", "including the second phase");
has(phaseHtml, "Remove Grid power sensor phase 1", "a phase can be taken back out");

// Three sensors is the cap — a fourth box would only invite a bad value.
const full = {
  ...threePhase,
  options: { power_input_alias: "sensor.a, sensor.b, sensor.c" },
};
const fullHtml = renderToString(h("div", null, ...view(haState, actions, full)));
has(fullHtml, 'aria-label="Grid power sensor phase 3"', "the third phase is editable");
lacks(fullHtml, "phase 4", "and there is no fourth");

// Import and export are zipped per phase, so a mismatch aborts start-up. Say
// so here rather than on the next restart.
const mismatched = {
  ...threePhase,
  options: {
    power_input_alias: "sensor.a, sensor.b, sensor.c",
    power_output_alias: "sensor.out",
  },
};
const mismatchHtml = renderToString(h("div", null, ...view(haState, actions, mismatched)));
has(mismatchHtml, "the counts have to match", "a phase-count mismatch is flagged");
const matched = {
  ...threePhase,
  options: { power_input_alias: "sensor.a", power_output_alias: "sensor.out" },
};
const matchedHtml = renderToString(h("div", null, ...view(haState, actions, matched)));
lacks(matchedHtml, "the counts have to match", "matching counts say nothing");
const unpaired = { ...threePhase, options: { power_input_alias: "sensor.a, sensor.b" } };
const unpairedHtml = renderToString(h("div", null, ...view(haState, actions, unpaired)));
lacks(unpairedHtml, "the counts have to match", "an empty export sensor is not a mismatch");

// Simple mode must never offer the raw file editor.
lacks(pickerHtml, "+ Add section", "the INI editor is hidden in guided mode");

// ── trend lines, accumulated in the browser ──
// No backend series: the page already polls every couple of seconds, so the
// samples it needs are the ones it has received.
{
  const hist: SeriesHistory = {};
  recordSnapshot(hist, snapshot);
  recordSnapshot(hist, snapshot);
  ok(seriesOf(hist, GRID_SERIES).length === 2, "each new snapshot adds one sample");
  ok(seriesOf(hist, GRID_SERIES)[0] === -13, "and it is the grid total");
  ok(
    seriesOf(hist, batterySeries({ consumer_id: "02b250000001" })).length === 2,
    "batteries are tracked per consumer",
  );
  ok(
    seriesOf(hist, meterSeries("JSON_HTTP"))[0] === 240,
    "so is each power source's total",
  );
  recordSnapshot(hist, null);
  ok(seriesOf(hist, GRID_SERIES).length === 2, "a missing snapshot records nothing");

  // One sample per snapshot, not one per CT device: two of them each carry a
  // share of the house, so recording both would put two points on the trend
  // for one moment and each would be a fraction of the total.
  const twoCt: SeriesHistory = {};
  recordSnapshot(twoCt, {
    ...snapshot,
    devices: [
      { ...snapshot.devices![0], grid: { grid_total_w: -13 } },
      { ...snapshot.devices![0], device_id: "ct-2", grid: { grid_total_w: 7 } },
    ],
  });
  ok(seriesOf(twoCt, GRID_SERIES).length === 1, "two CT devices are one sample");
  ok(seriesOf(twoCt, GRID_SERIES)[0] === -6, "summed the way the headline sums");

  // Through gridTotal, so the trend cannot disagree with the number above it:
  // that brings the per-phase and power-source fallbacks with it.
  const phases: SeriesHistory = {};
  recordSnapshot(phases, {
    ...snapshot,
    devices: [{ ...snapshot.devices![0], grid: { l1_w: 12, l2_w: -30, l3_w: 5 } }],
  });
  ok(seriesOf(phases, GRID_SERIES)[0] === -13, "a per-phase reading still counts");
  ok(seriesOf(hist, "nope").length === 0, "an unseen series is empty, not undefined");

  // A tab left open all day must not grow without bound.
  const long: SeriesHistory = {};
  for (let i = 0; i < HISTORY_LIMIT + 25; i++) {
    recordSnapshot(long, {
      ...snapshot,
      devices: [{ ...snapshot.devices![0], grid: { grid_total_w: i } }],
    });
  }
  const capped = seriesOf(long, GRID_SERIES);
  ok(capped.length === HISTORY_LIMIT, "the window is capped");
  ok(capped[capped.length - 1] === HISTORY_LIMIT + 24, "and it keeps the newest");
  ok(capped[0] === 25, "dropping the oldest");
}

// A reading that is not a number would render as an "NaN,NaN" point and take
// the whole line with it.
{
  const hist: SeriesHistory = {};
  recordSnapshot(hist, {
    ...snapshot,
    devices: [{ ...snapshot.devices![0], grid: { grid_total_w: NaN } }],
    powermeters: [{ name: "M", last_total_w: undefined }],
  });
  ok(seriesOf(hist, GRID_SERIES).length === 0, "NaN is not a sample");
  ok(seriesOf(hist, meterSeries("M")).length === 0, "nor is a missing reading");
}

// A failed read leaves the last value in place; charting it would draw a flat
// line across a moment nothing was measured. gridTotal already skips these, so
// recording them would also put the trend at odds with the headline.
{
  const hist: SeriesHistory = {};
  recordSnapshot(hist, {
    ...snapshot,
    devices: [],
    powermeters: [{ name: "M", last_read_ok: false, last_total_w: 240 }],
  });
  ok(seriesOf(hist, meterSeries("M")).length === 0, "a failed read is not a sample");
  recordSnapshot(hist, {
    ...snapshot,
    devices: [],
    powermeters: [{ name: "M", last_read_ok: true, last_total_w: 240 }],
  });
  ok(seriesOf(hist, meterSeries("M")).length === 1, "a good one still is");
  // Unknown is not failed: a pull meter reports neither until it is read.
  recordSnapshot(hist, {
    ...snapshot,
    devices: [],
    powermeters: [{ name: "M", last_total_w: 250 }],
  });
  ok(seriesOf(hist, meterSeries("M")).length === 2, "an unstated outcome counts");
}

ok(sparkGeometry([1, 2], 100, 26) === null, "two points are not a trend");
ok(sparkGeometry([], 100, 26) === null, "and neither is nothing");
{
  // Signed watts: the range always spans zero, so a window holding only
  // export cannot read as a climb from a baseline that is not zero.
  const g = sparkGeometry([-100, -50, -20], 100, 26)!;
  ok(g.min === -100 && g.max === 0, "the range always includes zero");
  ok(g.zeroY === 0, "so the zero line is drawn at the top here");
  ok(g.points.split(" ").length === 3, "one point per sample");
  ok(g.points.startsWith("0.0,"), "starting at the left edge");
  ok(g.points.split(" ")[2].startsWith("100.0,"), "and ending at the right");
  const flat = sparkGeometry([0, 0, 0], 100, 26)!;
  ok(flat.points.includes("13.0"), "a flat series is centred, not divided by zero");
}

// The card only draws a line once it has something to draw.
{
  const withHistory: AppState = {
    ...live,
    tab: "sources",
    history: { [meterSeries("JSON_HTTP")]: [10, 240, 500] },
  };
  const sparkHtml = renderToString(
    h("div", null, ...view(withHistory, actions, initialConfigState())),
  );
  has(sparkHtml, "spark-line", "the power source card plots its total");
  has(sparkHtml, "+500 W", "and says how far it swung");
  const noHistory = renderToString(
    h("div", null, ...view({ ...live, tab: "sources" }, actions, initialConfigState())),
  );
  lacks(noHistory, "spark-line", "with no samples yet there is no line");
}

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

// ── a consumer that has never reported is not a battery ──
//
// A retained MQTT command — or a control changed while a battery was away —
// creates a placeholder consumer to hold that setting until the battery turns
// up, and the emulator deliberately never expires it. Rendered like any other
// consumer it becomes a phantom device: default phase A, 0 W "discharging",
// a healthy Auto chip, and a battery count of two for a one-battery install.
const phantomSnapshot: StatusSnapshot = {
  schema_version: 1,
  generated_at: "2026-08-01T12:00:00+00:00",
  capabilities: { controls: true },
  devices: [
    {
      kind: "ct002",
      device_id: "ct-1",
      ct_type: "HME-4",
      running: true,
      grid: { grid_total_w: -13 },
      consumers: [
        {
          consumer_id: "682499eeef07",
          device_type: "VNSD-0",
          phase: "0",
          reported_power_w: 0,
          last_seen_at: "2026-08-01T11:59:59+00:00",
          last_seen_age_s: 1,
          poll_interval_s: 2,
          active: true,
        },
        // What the backend emits for a placeholder: no liveness at all, and
        // never_reported to say so rather than leaving it to be inferred.
        {
          consumer_id: "7ce712af5ef0",
          phase: "A",
          reported_power_w: 0,
          never_reported: true,
          active: true,
        },
      ],
    },
  ],
};
const phantomState: AppState = {
  ...initialState(),
  snapshot: phantomSnapshot,
  connection: "live",
};

ok(allConsumers(phantomSnapshot).length === 2, "both consumers are in the document");
ok(
  reportingConsumers(phantomSnapshot).length === 1,
  "only the one that has reported counts as a battery",
);
ok(
  hasReported(phantomSnapshot.devices![0].consumers![1]) === false,
  "a missing last_seen_at means it has never reported",
);
ok(
  batteryHealth(phantomSnapshot.devices![0].consumers![1]).label ===
    "Never reported",
  "the placeholder does not claim to be steering happily",
);

const phantomHtml = renderToString(
  h("div", null, ...view(phantomState, actions, initialConfigState())),
);
// The emulator card counts one battery, and the rail plots one bar.
has(phantomHtml, "<dt>Batteries</dt><dd>1</dd>", "the phantom is not counted");
ok(
  (phantomHtml.match(/contrib-row/g) || []).length === 1,
  "the phantom gets no bar on the contribution rail",
);

const phantomBatteries = renderToString(
  h("div", null, ...view({ ...phantomState, tab: "batteries" }, actions, initialConfigState())),
);
// It is still listed, so the stale setting can be found and cleared...
has(phantomBatteries, "7C:E7:12:AF:5E:F0", "the placeholder is still listed");
has(phantomBatteries, "No battery has ever reported", "and it says what it is");
// ...but none of the defaults are dressed up as measurements.
lacks(
  phantomBatteries,
  "<dt>Phase</dt><dd>Phase A</dd>",
  "a default phase is not reported as a measured one",
);
ok(
  (phantomBatteries.match(/batt-figure/g) || []).length === 1,
  "only the real battery gets a power figure",
);

if (failures) {
  console.error(`\n${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("dashboard.test.ts: ALL PASSED");
