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
import { initialConfigState } from "./config-view.js";
import type { StatusSnapshot } from "./types.js";

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
lacks(readOnlyHtml, ">Disable<", "no write controls without the capability");
const writable = renderToString(
  h("div", null, ...view({ ...live, tab: "batteries" }, actions, initialConfigState())),
);
has(writable, ">Disable<", "write controls appear with the capability");

// ── add-on schema parsing ──
const f = parseAddonSchema("float(0,1)?");
ok(f.type === "float" && f.min === 0 && f.max === 1 && f.optional, "float(0,1)? parses");
const l = parseAddonSchema("list(critical|error|info)");
ok(l.type === "list" && l.options?.length === 3 && !l.optional, "list(...) parses");
ok(parseAddonSchema("password?").type === "password", "password? parses");
ok(parseAddonSchema("int(0,)?").min === 0, "int(0,) parses an open upper bound");
ok(parseAddonSchema("").type === "str", "an empty spec falls back to text");
ok(parseAddonSchema("weird_type").type === "weird_type", "unknown types survive");

if (failures) {
  console.error(`\n${failures} assertion(s) failed`);
  process.exit(1);
}
console.log("dashboard.test.ts: ALL PASSED");
