// generate.js — pure config generators. No DOM access so it can be unit-tested
// in Node (see generate.test.mjs). Takes the app state object and returns a
// string for either config.ini (Python add-on) or an ESPHome YAML file.

import {
  getPowermeter,
  PER_METER_TUNING,
  CT_BASIC,
  CT_ACTIVE,
  CT_BALANCER,
  CT_DC_KEEPALIVE,
  CT_EFFICIENCY,
  CT_SATURATION,
  MARSTEK_FIELDS,
  MQTT_INSIGHTS_FIELDS,
  type Field,
  type Fields,
  type FieldValue,
} from "./schema.js";
import type { State, Meter } from "./state.js";
import { esphomeSource } from "./links.js";

const APP_VERSION_NOTE =
  "# Generated with the AstraMeter config generator.\n# Re-import this file there any time to keep editing.";

// ── small helpers ──────────────────────────────────────────────────────────

function isBlank(v: unknown): boolean {
  return v === undefined || v === null || String(v).trim() === "";
}

function boolToIni(v: unknown): string {
  return v ? "True" : "False";
}

// Emit a single `KEY = value` line for a schema field given the raw form value,
// honouring checkbox defaults (only write when it differs from the default).
function iniLine(field: Field, value: unknown): string | null {
  if (field.type === "checkbox") {
    // Honour the schema default when the user never touched the box. The Python
    // loaders default every boolean to False, so we only ever need to *emit*
    // True (a missing key already means False); emitting True is harmless and
    // explicit, and covers meters whose schema default is true (e.g. EmLog).
    const cur = value === undefined ? !!field.default : !!value;
    return cur ? `${field.key} = True` : null;
  }
  if (isBlank(value)) return null;
  return `${field.key} = ${String(value).trim()}`;
}

// ── config.ini ───────────────────────────────────────────────────────────────

function generalSection(state: State): string {
  const g = state.general || {};
  const lines = ["[GENERAL]"];
  const types = (g.deviceTypes && g.deviceTypes.length ? g.deviceTypes : ["shellypro3em"]).join(",");
  lines.push(`DEVICE_TYPE = ${types}`);
  if (!isBlank(g.deviceIds)) lines.push(`DEVICE_IDS = ${g.deviceIds.trim()}`);
  lines.push(`SKIP_POWERMETER_TEST = ${boolToIni(!!g.skipPowermeterTest)}`);
  if (g.webConfigEnabled) {
    lines.push("WEB_CONFIG_ENABLED = True");
    if (!isBlank(g.webServerPort)) lines.push(`WEB_SERVER_PORT = ${g.webServerPort}`);
  }
  if (!isBlank(g.throttleInterval)) lines.push(`THROTTLE_INTERVAL = ${g.throttleInterval}`);
  if (!isBlank(g.waitForNextMessage)) lines.push(`WAIT_FOR_NEXT_MESSAGE = ${g.waitForNextMessage}`);
  if (!isBlank(g.dedupeTimeWindow)) lines.push(`DEDUPE_TIME_WINDOW = ${g.dedupeTimeWindow}`);
  return lines.join("\n");
}

function meterSection(meter: Meter, opts: { multi: boolean }): string {
  const pm = getPowermeter(meter.type);
  if (!pm) return "";
  const suffix = (meter.suffix || "").trim();
  const header = `[${pm.section}${suffix ? "_" + suffix : ""}]`;
  const lines = [header];
  const fields = meter.fields || {};

  // MQTT phase handling: TOPIC→TOPICS, JSON_PATH→JSON_PATHS in 3-phase mode.
  const phaseList = pm.phaseListKeys && meter.phases === 3 ? pm.phaseListKeys : null;

  for (const field of pm.fields) {
    let value = fields[field.key];
    if (phaseList && field.key === "TOPIC" && !isBlank(value)) {
      lines.push(`TOPICS = ${String(value).trim()}`);
      continue;
    }
    if (phaseList && field.key === "JSON_PATH" && !isBlank(value)) {
      lines.push(`JSON_PATHS = ${String(value).trim()}`);
      continue;
    }
    const line = iniLine(field, value);
    if (line) lines.push(line);
  }

  // Per-meter tuning (throttle, smoothing, transform, hampel, PID …)
  const tuning = meter.tuning || {};
  for (const field of PER_METER_TUNING) {
    const line = iniLine(field, tuning[field.key]);
    if (line) lines.push(line);
  }

  // NETMASK for multi-meter setups
  if (opts && opts.multi && !isBlank(meter.netmask)) {
    lines.push(`NETMASK = ${meter.netmask.trim()}`);
  }

  return lines.join("\n");
}

function ctSection(state: State, sectionName: string): string {
  const ct = state.ct || {};
  const f = ct.fields || {};
  const lines = [`[${sectionName}]`];
  const groups = [
    CT_BASIC,
    CT_ACTIVE,
    CT_BALANCER,
    CT_DC_KEEPALIVE,
    CT_EFFICIENCY,
    CT_SATURATION,
  ];
  for (const group of groups) {
    for (const field of group) {
      const line = iniLine(field, f[field.key]);
      if (line) lines.push(line);
    }
  }
  // Only emit if something beyond the header was added.
  return lines.length > 1 ? lines.join("\n") : "";
}

function marstekSection(state: State): string {
  const m = state.marstek || {};
  if (!m.enabled) return "";
  const f = m.fields || {};
  // Registration needs credentials; skip the section while it's enabled but
  // unconfigured so the default-on toggle never emits a broken [MARSTEK].
  if (isBlank(f.MAILBOX) || isBlank(f.PASSWORD)) return "";
  const lines = ["[MARSTEK]", "ENABLE = True"];
  for (const field of MARSTEK_FIELDS) {
    const line = iniLine(field, f[field.key]);
    if (line) lines.push(line);
  }
  return lines.join("\n");
}

function mqttInsightsSection(state: State): string {
  const mi = state.mqttInsights || {};
  if (!mi.enabled) return "";
  const f = mi.fields || {};
  // Insights needs a broker to connect to; skip while enabled but unconfigured
  // so the default-on toggle never emits an empty [MQTT_INSIGHTS] section.
  if (isBlank(f.BROKER)) return "";
  const lines = ["[MQTT_INSIGHTS]"];
  for (const field of MQTT_INSIGHTS_FIELDS) {
    const line = iniLine(field, f[field.key]);
    if (line) lines.push(line);
  }
  return lines.join("\n");
}

export function generateConfigIni(state: State): string {
  const meters = state.meters && state.meters.length ? state.meters : [];
  const multi = meters.length > 1;
  const blocks = [APP_VERSION_NOTE, generalSection(state)];

  for (const meter of meters) {
    const block = meterSection(meter, { multi });
    if (block) blocks.push(block);
  }

  const types = (state.general && state.general.deviceTypes) || [];
  if (types.includes("ct002")) {
    const s = ctSection(state, "CT002");
    if (s) blocks.push(s);
  }
  if (types.includes("ct003")) {
    const s = ctSection(state, "CT003");
    if (s) blocks.push(s);
  }

  const marstek = marstekSection(state);
  if (marstek) blocks.push(marstek);
  const insights = mqttInsightsSection(state);
  if (insights) blocks.push(insights);

  return blocks.join("\n\n") + "\n";
}

// ── ESPHome YAML ─────────────────────────────────────────────────────────────

const IND = "  ";


// Render the upstream grid sensor(s) for the chosen meter. Returns
// { topBlocks: [...], sensorBlock: string, phases: number, warnings: [...] }
function esphomeSensor(state: State) {
  const meter = (state.meters && state.meters[0]) || { type: "homeassistant", fields: {}, phases: 1, tuning: {} };
  const pm = getPowermeter(meter.type) || getPowermeter("homeassistant")!;
  const phases = meter.phases === 3 ? 3 : 1;
  const f = meter.fields || {};
  const tuning = meter.tuning || {};
  const esp = pm.esphome;
  const warnings: string[] = [];
  const topBlocks: string[] = [];
  const ids = phases === 3 ? ["grid_l1", "grid_l2", "grid_l3"] : ["grid_l1"];

  // Per-sensor filters from value-transform / throttle tuning. Applied to
  // every phase sensor — a single offset/multiplier value applies to all
  // phases (matching the Python add-on), while a comma list maps per phase.
  function phaseValue(raw: FieldValue, idx: number): string {
    if (isBlank(raw)) return "";
    const parts = String(raw).split(",").map((s) => s.trim());
    return parts.length === 1 ? parts[0] : (parts[idx] ?? "");
  }
  function phaseFilterBlock(idx: number): string {
    const lines: string[] = [];
    const off = phaseValue(tuning.POWER_OFFSET, idx);
    const mul = phaseValue(tuning.POWER_MULTIPLIER, idx);
    if (!isBlank(off)) lines.push(`- offset: ${off}`);
    if (!isBlank(mul)) lines.push(`- multiply: ${mul}`);
    if (!isBlank(tuning.THROTTLE_INTERVAL) && Number(tuning.THROTTLE_INTERVAL) > 0)
      lines.push(`- throttle: ${tuning.THROTTLE_INTERVAL}s`);
    return lines.length
      ? `\n${IND}${IND}filters:\n` + lines.map((l) => `${IND}${IND}${IND}${l}`).join("\n")
      : "";
  }

  function templateSensor(id: string): string {
    return `${IND}- platform: template\n${IND}${IND}id: ${id}\n${IND}${IND}unit_of_measurement: W\n${IND}${IND}device_class: power`;
  }

  // Per-meter ESPHome warning, declared on the meter (e.g. Modbus-TCP, TQ-EM).
  if (esp.warn) {
    const w = typeof esp.warn === "function" ? esp.warn(f) : esp.warn;
    if (w) warnings.push(w);
  }

  if (esp.kind === "homeassistant") {
    topBlocks.push("api:        # required: the ESP subscribes to the HA entity over the native API");
    const entities = esp.haEntity
      ? [esp.haEntity(f)]
      : splitPhases(f.CURRENT_POWER_ENTITY || "sensor.grid_power", phases);
    const sensors = ids.map((id, i) => {
      return `${IND}- platform: homeassistant\n${IND}${IND}id: ${id}\n${IND}${IND}entity_id: ${entities[i] || "sensor.grid_power"}${phaseFilterBlock(i)}`;
    });
    return { topBlocks, sensorBlock: "sensor:\n" + sensors.join("\n"), phases, warnings };
  }

  if (esp.kind === "mqtt") {
    const broker = f.BROKER || "192.168.1.10";
    const port = f.PORT || "1883";
    topBlocks.push(`mqtt:\n${IND}broker: ${broker}\n${IND}port: ${port}`);
    const topics = splitPhases(f.TOPIC || "home/powermeter", phases);
    if (!isBlank(f.JSON_PATH)) {
      warnings.push("JSON payloads need an on_json_message handler; see docs/esphome-powermeters.md for the template-sensor pattern.");
    }
    const sensors = ids.map((id, i) => {
      return `${IND}- platform: mqtt_subscribe\n${IND}${IND}id: ${id}\n${IND}${IND}topic: ${topics[i] || "home/powermeter"}\n${IND}${IND}unit_of_measurement: W${phaseFilterBlock(i)}`;
    });
    return { topBlocks, sensorBlock: "sensor:\n" + sensors.join("\n"), phases, warnings };
  }

  if (esp.kind === "sml") {
    topBlocks.push(
      `uart:\n${IND}id: uart_bus\n${IND}rx_pin: GPIO16\n${IND}baud_rate: 9600\n${IND}data_bits: 8\n${IND}parity: NONE\n${IND}stop_bits: 1`,
    );
    topBlocks.push(`sml:\n${IND}id: mysml\n${IND}uart_id: uart_bus`);
    const obis = phases === 3 ? ["1-0:36.7.0", "1-0:56.7.0", "1-0:76.7.0"] : ["1-0:16.7.0"];
    const sensors = ids.map((id, i) => {
      return `${IND}- platform: sml\n${IND}${IND}id: ${id}\n${IND}${IND}sml_id: mysml\n${IND}${IND}obis_code: "${obis[i]}"\n${IND}${IND}unit_of_measurement: W${phaseFilterBlock(i)}`;
    });
    return { topBlocks, sensorBlock: "sensor:\n" + sensors.join("\n"), phases, warnings };
  }

  if (esp.kind === "modbus") {
    topBlocks.push(`uart:\n${IND}id: mod_uart\n${IND}tx_pin: GPIO17\n${IND}rx_pin: GPIO16\n${IND}baud_rate: 9600\n${IND}stop_bits: 1`);
    topBlocks.push(`modbus:\n${IND}id: modbus1\n${IND}uart_id: mod_uart`);
    topBlocks.push(
      `modbus_controller:\n${IND}- id: meter\n${IND}${IND}address: ${f.UNIT_ID || "1"}\n${IND}${IND}modbus_id: modbus1\n${IND}${IND}update_interval: 1s`,
    );
    const valueType = modbusValueType(f.DATA_TYPE);
    const regType = String(f.REGISTER_TYPE || "HOLDING").toLowerCase() === "input" ? "read" : "holding";
    const sensor = `${IND}- platform: modbus_controller\n${IND}${IND}modbus_controller_id: meter\n${IND}${IND}id: grid_l1\n${IND}${IND}register_type: ${regType}\n${IND}${IND}address: ${f.ADDRESS || "0"}\n${IND}${IND}value_type: ${valueType}\n${IND}${IND}unit_of_measurement: W${phaseFilterBlock(0)}`;
    return { topBlocks, sensorBlock: "sensor:\n" + sensor, phases: 1, warnings };
  }

  if (esp.kind === "http") {
    topBlocks.push(`http_request:\n${IND}useragent: esphome/astrameter\n${IND}timeout: 5s`);
    const use3 = phases === 3 && esp.url3;
    const sensors = (use3 ? ids : ["grid_l1"]).map((id, i) => templateSensor(id) + phaseFilterBlock(i));
    const url = use3 ? esp.url3!(f) : (esp.url1 ? esp.url1(f) : "http://example.com/api");
    const lambdaBody = use3
      ? esp.lambda3
      : typeof esp.lambda1 === "function"
        ? esp.lambda1(f)
        : esp.lambda1;
    const jsonRoot = esp.jsonRoot || "JsonObject root";
    const headerLines =
      esp.headersField && !isBlank(f[esp.headersField])
        ? `\n${IND}${IND}${IND}${IND}${IND}headers:\n` +
          String(f[esp.headersField])
            .split(";")
            .map((h) => h.trim())
            .filter(Boolean)
            .map((h) => {
              const idx = h.indexOf(":");
              const k = idx >= 0 ? h.slice(0, idx).trim() : h;
              const v = idx >= 0 ? h.slice(idx + 1).trim() : "";
              return `${IND}${IND}${IND}${IND}${IND}${IND}${k}: ${v}`;
            })
            .join("\n")
        : "";
    const interval =
      `interval:\n${IND}- interval: 1s\n${IND}${IND}then:\n${IND}${IND}${IND}- http_request.get:\n` +
      `${IND}${IND}${IND}${IND}${IND}url: ${url}${headerLines}\n` +
      `${IND}${IND}${IND}${IND}${IND}capture_response: true\n${IND}${IND}${IND}${IND}${IND}on_response:\n${IND}${IND}${IND}${IND}${IND}${IND}then:\n` +
      `${IND}${IND}${IND}${IND}${IND}${IND}${IND}- lambda: |-\n` +
      `${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}json::parse_json(body, [](${jsonRoot}) -> bool {\n` +
      `${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}${lambdaBody}\n` +
      `${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}return true;\n` +
      `${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}${IND}});`;
    topBlocks.push(interval);
    if (use3) warnings.push("Three-phase HTTP support is illustrative — confirm the JSON field names for your meter.");
    return { topBlocks, sensorBlock: "sensor:\n" + sensors.join("\n"), phases: use3 ? 3 : 1, warnings };
  }

  // unsupported
  warnings.push(
    `${pm.label} has no ESPHome component yet — this is a placeholder. Run the Python add-on for this meter, or publish its reading to MQTT/Home Assistant and read that instead.`,
  );
  const sensor = `${IND}- platform: template\n${IND}${IND}id: grid_l1\n${IND}${IND}unit_of_measurement: W\n${IND}${IND}device_class: power\n${IND}${IND}# TODO: publish your grid power (W) into grid_l1.`;
  return { topBlocks, sensorBlock: "sensor:\n" + sensor, phases: 1, warnings };
}

function splitPhases(value: unknown, phases: number): string[] {
  const parts = String(value).split(",").map((s) => s.trim());
  if (phases === 1) return [parts[0] || String(value)];
  return [parts[0] || "", parts[1] || "", parts[2] || ""];
}

function modbusValueType(dataType: unknown): string {
  switch (String(dataType || "UINT16").toUpperCase()) {
    case "INT16":
      return "S_WORD";
    case "UINT32":
      return "U_DWORD";
    case "INT32":
      return "S_DWORD";
    case "FLOAT32":
      return "FP32";
    default:
      return "U_WORD";
  }
}

// Build the optional ct002: tuning sub-blocks from CT field values.
function ct002OptionalBlocks(ct: { fields: Fields } | undefined): string[] {
  const f = (ct && ct.fields) || {};
  const lines: string[] = [];

  function ctVal(key: string): FieldValue {
    return f[key];
  }
  function pushBool(arr: string[], key: string, eyKey: string): void {
    const v = ctVal(key);
    if (v === "True" || v === "true") arr.push(`${eyKey}: true`);
    else if (v === "False" || v === "false") arr.push(`${eyKey}: false`);
  }

  // active_control + base options live directly on ct002:; handled by caller.

  // filters: (cross-phase) come from the meter tuning, not CT — handled in caller.

  // balancer
  const bal: string[] = [];
  pushBool(bal, "FAIR_DISTRIBUTION", "fair_distribution");
  for (const fld of CT_BALANCER) {
    if (fld.key === "FAIR_DISTRIBUTION") continue;
    if (!isBlank(ctVal(fld.key))) bal.push(`${fld.ey}: ${ctVal(fld.key)}`);
  }
  // MIN_DC_OUTPUT is a per-battery knob in the UI, but its ESPHome key lives
  // under the same `balancer:` block.
  for (const fld of CT_DC_KEEPALIVE) {
    if (!isBlank(ctVal(fld.key))) bal.push(`${fld.ey}: ${ctVal(fld.key)}`);
  }
  for (const fld of CT_EFFICIENCY) {
    if (isBlank(ctVal(fld.key))) continue;
    const v = ctVal(fld.key);
    if (fld.key === "EFFICIENCY_ROTATION_INTERVAL") bal.push(`${fld.ey}: ${v}s`);
    else bal.push(`${fld.ey}: ${v}`);
  }
  if (bal.length) {
    lines.push(`${IND}balancer:\n` + bal.map((l) => `${IND}${IND}${l}`).join("\n"));
  }

  // saturation
  const sat: string[] = [];
  pushBool(sat, "SATURATION_DETECTION", "enabled");
  for (const fld of CT_SATURATION) {
    if (fld.key === "SATURATION_DETECTION") continue;
    if (isBlank(ctVal(fld.key))) continue;
    const v = ctVal(fld.key);
    if (fld.key === "SATURATION_GRACE_SECONDS" || fld.key === "SATURATION_STALL_TIMEOUT_SECONDS")
      sat.push(`${fld.ey}: ${v}s`);
    else sat.push(`${fld.ey}: ${v}`);
  }
  if (sat.length) {
    lines.push(`${IND}saturation:\n` + sat.map((l) => `${IND}${IND}${l}`).join("\n"));
  }

  return lines;
}

function ct002FilterBlock(meter: Meter | undefined): string | null {
  const tuning = (meter && meter.tuning) || {};
  const sub: string[] = [];

  const hampel: string[] = [];
  if (!isBlank(tuning.HAMPEL_WINDOW) && Number(tuning.HAMPEL_WINDOW) > 0) {
    hampel.push(`window: ${tuning.HAMPEL_WINDOW}`);
    if (!isBlank(tuning.HAMPEL_N_SIGMA)) hampel.push(`n_sigma: ${tuning.HAMPEL_N_SIGMA}`);
    if (!isBlank(tuning.HAMPEL_MIN_THRESHOLD)) hampel.push(`min_threshold: ${tuning.HAMPEL_MIN_THRESHOLD}`);
  }
  if (hampel.length) sub.push(`${IND}${IND}hampel:\n` + hampel.map((l) => `${IND}${IND}${IND}${l}`).join("\n"));

  const smoothing: string[] = [];
  if (!isBlank(tuning.SMOOTH_TARGET_ALPHA)) smoothing.push(`alpha: ${tuning.SMOOTH_TARGET_ALPHA}`);
  if (!isBlank(tuning.MAX_SMOOTH_STEP) && Number(tuning.MAX_SMOOTH_STEP) > 0)
    smoothing.push(`max_step: ${tuning.MAX_SMOOTH_STEP}`);
  if (smoothing.length) sub.push(`${IND}${IND}smoothing:\n` + smoothing.map((l) => `${IND}${IND}${IND}${l}`).join("\n"));

  if (!isBlank(tuning.DEADBAND) && Number(tuning.DEADBAND) > 0) {
    sub.push(`${IND}${IND}deadband:\n${IND}${IND}${IND}deadband: ${tuning.DEADBAND}`);
  }

  const pid: string[] = [];
  if (!isBlank(tuning.PID_KP) && Number(tuning.PID_KP) > 0) {
    pid.push(`kp: ${tuning.PID_KP}`);
    if (!isBlank(tuning.PID_KI)) pid.push(`ki: ${tuning.PID_KI}`);
    if (!isBlank(tuning.PID_KD)) pid.push(`kd: ${tuning.PID_KD}`);
    if (!isBlank(tuning.PID_OUTPUT_MAX)) pid.push(`output_max: ${tuning.PID_OUTPUT_MAX}`);
    if (!isBlank(tuning.PID_MODE)) pid.push(`mode: ${tuning.PID_MODE}`);
  }
  if (pid.length) sub.push(`${IND}${IND}pid:\n` + pid.map((l) => `${IND}${IND}${IND}${l}`).join("\n"));

  if (!sub.length) return null;
  return `${IND}filters:\n` + sub.join("\n");
}

export function generateEsphome(state: State): string {
  const esp = state.esphome || {};
  const meter = (state.meters && state.meters[0]) || {};
  const { topBlocks, sensorBlock, phases, warnings } = esphomeSensor(state);

  // ESPHome allows only one top-level `mqtt:` block, so an MQTT meter and
  // MQTT Insights must share the same broker. Warn if they were set differently
  // — Insights reuses whatever broker this YAML connects to.
  const wantInsights = state.mqttInsights && state.mqttInsights.enabled;
  const wantMarstek = state.marstek && state.marstek.enabled;
  const pm0 = getPowermeter(meter.type);
  if (wantInsights && pm0 && pm0.esphome && pm0.esphome.kind === "mqtt") {
    const mf0 = state.mqttInsights.fields || {};
    const mFields = meter.fields || {};
    const sameBroker = (mf0.BROKER || "") === (mFields.BROKER || "");
    const samePort = (String(mf0.PORT || "1883")) === (String(mFields.PORT || "1883"));
    if (!sameBroker || !samePort) {
      warnings.push(
        "MQTT Insights and the MQTT meter must use the same broker (ESPHome has one mqtt: block). The meter's broker/port below is used for both.",
      );
    }
  }

  const out: string[] = [];
  out.push(
    "# AstraMeter for ESPHome — runs the CT002/CT003 emulator directly on an ESP32.\n" +
      "#\n" +
      "# New to ESPHome? Do this once:\n" +
      "#   1. Install ESPHome (Home Assistant add-on \"ESPHome Device Builder\", or\n" +
      "#      web.esphome.io in Chrome/Edge, or `pip install esphome`).\n" +
      "#   2. Create a new device and replace its file with everything below.\n" +
      "#   3. Add your WiFi to secrets.yaml:\n" +
      "#        wifi_ssid: \"YourWiFiName\"\n" +
      "#        wifi_password: \"YourWiFiPassword\"\n" +
      "#   4. Make sure the board: line below matches the ESP32 you bought.\n" +
      "#   5. Install/flash over USB the first time (then updates go over WiFi).\n" +
      "#   6. In the Marstek app, point the battery at a CT002/CT003 meter.",
  );

  if (warnings.length) {
    out.push(warnings.map((w) => `# ⚠ ${w}`).join("\n"));
  }

  const name = esp.name || "astrameter-ct002";
  const friendly = esp.friendlyName || "AstraMeter CT002";
  out.push(`esphome:\n${IND}name: ${name}\n${IND}friendly_name: ${friendly}`);
  out.push(`esp32:\n${IND}board: ${esp.board || "esp32dev"}\n${IND}framework:\n${IND}${IND}type: ${esp.framework || "esp-idf"}`);
  out.push("logger:");
  out.push(`ota:\n${IND}- platform: esphome`);
  out.push(`wifi:\n${IND}ssid: !secret wifi_ssid\n${IND}password: !secret wifi_password`);
  out.push(`external_components:\n${IND}- source: ${esphomeSource()}\n${IND}${IND}components: [ct002]`);

  // mqtt_insights needs a top-level mqtt: block. If the meter itself is MQTT,
  // its broker wins (the data source is what matters) and Insights reuses it;
  // otherwise fall back to the Insights broker.
  if (wantInsights) {
    const mf = state.mqttInsights.fields || {};
    const useMeter = pm0 && pm0.esphome && pm0.esphome.kind === "mqtt";
    const mFields = (meter.fields || {});
    const broker = (useMeter && mFields.BROKER) || mf.BROKER || "192.168.1.10";
    const port = (useMeter && mFields.PORT) || mf.PORT || "1883";
    out.push(`mqtt:\n${IND}broker: ${broker}\n${IND}port: ${port}`);
  }
  if (wantMarstek) {
    out.push(`http_request:\n${IND}timeout: 20s`);
  }

  // top blocks from the sensor (api/mqtt/uart/etc.) — de-dup api/mqtt if already added
  for (const b of topBlocks) {
    if (wantInsights && b.startsWith("mqtt:")) continue;
    out.push(b);
  }
  out.push(sensorBlock);

  // ct002: block
  const ctLines = [`ct002:`, `${IND}id: ct002_main`, `${IND}power_sensor_l1: grid_l1`];
  if (phases === 3) {
    ctLines.push(`${IND}power_sensor_l2: grid_l2`);
    ctLines.push(`${IND}power_sensor_l3: grid_l3`);
  }
  ctLines.push(`${IND}ct_type: ${esp.ctType || "HME-4"}`);

  const ctf = (state.ct && state.ct.fields) || {};
  // base CT options
  if (!isBlank(ctf.CT_MAC)) ctLines.push(`${IND}ct_mac: "${String(ctf.CT_MAC).trim()}"`);
  if (!isBlank(ctf.UDP_PORT)) ctLines.push(`${IND}udp_port: ${ctf.UDP_PORT}`);
  if (!isBlank(ctf.WIFI_RSSI)) ctLines.push(`${IND}wifi_rssi: ${ctf.WIFI_RSSI}`);
  if (ctf.ACTIVE_CONTROL === "True") ctLines.push(`${IND}active_control: true`);
  else if (ctf.ACTIVE_CONTROL === "False") ctLines.push(`${IND}active_control: false`);
  if (!isBlank(ctf.CONSUMER_TTL)) ctLines.push(`${IND}consumer_ttl: ${ctf.CONSUMER_TTL}s`);
  if (!isBlank(ctf.DEDUPE_TIME_WINDOW)) ctLines.push(`${IND}dedupe_window: ${ctf.DEDUPE_TIME_WINDOW}s`);

  const fb = ct002FilterBlock(meter);
  if (fb) ctLines.push(fb);
  for (const b of ct002OptionalBlocks(state.ct)) ctLines.push(b);

  if (wantInsights) {
    const mf = state.mqttInsights.fields || {};
    const sub = [`${IND}mqtt_insights:`];
    if (!isBlank(mf.BASE_TOPIC)) sub.push(`${IND}${IND}base_topic: ${mf.BASE_TOPIC}`);
    if (mf.HA_DISCOVERY) sub.push(`${IND}${IND}ha_discovery: ${mf.HA_DISCOVERY}`);
    if (!isBlank(mf.HA_DISCOVERY_PREFIX)) sub.push(`${IND}${IND}ha_discovery_prefix: ${mf.HA_DISCOVERY_PREFIX}`);
    if (mf.MARSTEK_MQTT_ENABLED) sub.push(`${IND}${IND}marstek_mqtt_enabled: ${mf.MARSTEK_MQTT_ENABLED}`);
    if (!isBlank(mf.MARSTEK_MQTT_INTERVAL)) sub.push(`${IND}${IND}marstek_mqtt_interval: ${mf.MARSTEK_MQTT_INTERVAL}s`);
    if (sub.length > 1) ctLines.push(sub.join("\n"));
  }

  if (wantMarstek) {
    const rf = state.marstek.fields || {};
    const sub = [`${IND}marstek_registration:`];
    if (!isBlank(rf.BASE_URL)) sub.push(`${IND}${IND}base_url: ${rf.BASE_URL}`);
    if (!isBlank(rf.MAILBOX)) sub.push(`${IND}${IND}mailbox: ${rf.MAILBOX}`);
    sub.push(`${IND}${IND}password: !secret marstek_password`);
    sub.push(`${IND}${IND}device_type: ${(esp.ctType || "HME-4") === "HME-3" ? "ct003" : "ct002"}`);
    if (!isBlank(rf.TIMEZONE)) sub.push(`${IND}${IND}timezone: ${rf.TIMEZONE}`);
    ctLines.push(sub.join("\n"));
  }

  out.push(ctLines.join("\n"));

  return out.join("\n\n") + "\n";
}

// ── Home Assistant add-on options (YAML) ──────────────────────────────────────
//
// The add-on can only read grid power from a Home Assistant sensor and runs a
// single meter, so this emits the flat `key: value` options block you paste into
// the add-on's Configuration → "Edit in YAML". Anything the UI can't express
// (other powermeter sources, multiple meters) needs a custom config.ini instead.

// Add-on options that are string-typed in the schema (entity ids, MACs, comma
// lists, credentials, URIs). These must always be quoted so an all-digit value
// like a MAC ("001122334455") keeps its leading zeros and isn't read as a
// number, and so float-ish transform values aren't coerced for a `str?` field.
const QUOTED_OPTION_KEYS = new Set([
  "power_input_alias",
  "power_output_alias",
  "device_types",
  "ct_mac",
  "power_offset",
  "power_multiplier",
  "pid_mode",
  "marstek_mailbox",
  "marstek_password",
  "mqtt_uri",
]);

function quoteYaml(s: string): string {
  return `"${s.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

// Render a YAML scalar. String-typed option keys are always quoted; for other
// keys, bare booleans and numbers pass through and everything else is quoted.
function yamlScalar(value: unknown, key?: string): string {
  const s = String(value).trim();
  if (key && QUOTED_OPTION_KEYS.has(key)) return quoteYaml(s);
  if (s === "true" || s === "false") return s;
  if (/^-?\d+(\.\d+)?$/.test(s)) return s;
  return quoteYaml(s);
}

export function generateHomeAssistant(state: State): string {
  const opts: string[] = [];
  const add = (key: string, value: unknown): void => {
    if (isBlank(value)) return;
    opts.push(`${key}: ${yamlScalar(value, key)}`);
  };

  const g = state.general || {};
  const meter = (state.meters && state.meters[0]) || ({} as Meter);
  const f = meter.fields || {};
  const tuning = meter.tuning || {};

  // Grid-power source: a single signed sensor, or separate import/export entities.
  const calc = f.POWER_CALCULATE === "True" || f.POWER_CALCULATE === true;
  if (calc && !isBlank(f.POWER_INPUT_ALIAS)) {
    add("power_input_alias", f.POWER_INPUT_ALIAS);
    add("power_output_alias", f.POWER_OUTPUT_ALIAS);
  } else {
    add("power_input_alias", f.CURRENT_POWER_ENTITY);
  }

  const types = (g.deviceTypes && g.deviceTypes.length ? g.deviceTypes : ["shellypro3em"]).join(",");
  add("device_types", types);

  // throttle / wait are top-level add-on options; the add-on has no per-meter
  // override, so prefer the global value and fall back to the meter tuning.
  add("throttle_interval", !isBlank(g.throttleInterval) ? g.throttleInterval : tuning.THROTTLE_INTERVAL);
  add("wait_for_next_message", !isBlank(g.waitForNextMessage) ? g.waitForNextMessage : tuning.WAIT_FOR_NEXT_MESSAGE);
  add("dedupe_time_window", g.dedupeTimeWindow);

  // CT identity / efficiency / DC keep-alive options.
  const ctf = (state.ct && state.ct.fields) || {};
  add("ct_mac", ctf.CT_MAC);
  add("min_efficient_power", ctf.MIN_EFFICIENT_POWER);
  add("efficiency_rotation_interval", ctf.EFFICIENCY_ROTATION_INTERVAL);
  add("min_dc_output", ctf.MIN_DC_OUTPUT);

  // Signal-conditioning filters (transform, smoothing, deadband, hampel, pid).
  // Option names are the lower-cased INI keys; throttle/wait handled above.
  for (const field of PER_METER_TUNING) {
    if (field.key === "THROTTLE_INTERVAL" || field.key === "WAIT_FOR_NEXT_MESSAGE") continue;
    add(field.key.toLowerCase(), tuning[field.key]);
  }

  // Marstek managed CT registration.
  if (state.marstek && state.marstek.enabled) {
    const m = state.marstek.fields || {};
    if (!isBlank(m.MAILBOX) && !isBlank(m.PASSWORD)) {
      add("marstek_auto_register_ct_device", true);
      add("marstek_mailbox", m.MAILBOX);
      add("marstek_password", m.PASSWORD);
    }
  }

  // MQTT Insights: only a custom external broker maps to an add-on option; the
  // add-on uses Home Assistant's internal broker automatically otherwise.
  if (state.mqttInsights && state.mqttInsights.enabled) {
    const mi = state.mqttInsights.fields || {};
    if (!isBlank(mi.BROKER)) {
      // TLS is a boolean from the UI, but restored state may carry a string;
      // treat only an explicit true / "true" / "1" as on.
      const tlsOn = mi.TLS === true || mi.TLS === "true" || mi.TLS === "1";
      const scheme = tlsOn ? "mqtts" : "mqtt";
      // Percent-encode credentials so special characters (@, :, /, …) don't
      // break the URI and survive parse_mqtt_uri's unquoting.
      const user = !isBlank(mi.USERNAME) ? encodeURIComponent(String(mi.USERNAME).trim()) : "";
      const pass = !isBlank(mi.PASSWORD) ? encodeURIComponent(String(mi.PASSWORD).trim()) : "";
      const cred = user ? `${user}${pass ? ":" + pass : ""}@` : "";
      const port = !isBlank(mi.PORT) ? `:${String(mi.PORT).trim()}` : "";
      add("mqtt_uri", `${scheme}://${cred}${String(mi.BROKER).trim()}${port}`);
    }
  }

  const header = [
    "# AstraMeter — Home Assistant add-on options.",
    "# Paste this into the add-on's Configuration tab via the ⋮ menu → \"Edit in YAML\".",
    "#",
    "# The add-on reads grid power from a Home Assistant sensor and runs a single",
    "# meter. For anything this UI can't express (other power-meter sources,",
    "# multiple meters), generate a config.ini instead (switch the target above),",
    "# drop it in /addon_configs/a0ef98c5_b2500_meter/, and set the add-on's",
    "# \"Custom Config\" option to its filename.",
  ].join("\n");

  return header + "\n" + opts.join("\n") + "\n";
}

export function generate(state: State): string {
  if (state.target === "esphome") return generateEsphome(state);
  if (state.target === "homeassistant") return generateHomeAssistant(state);
  return generateConfigIni(state);
}
