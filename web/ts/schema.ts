// schema.js — the single source of truth for every option the generator knows
// about. Pure data (no DOM, no side effects) so it can be unit-tested in Node
// and reused by both the form renderer (app.js) and the config generators
// (generate.js).
//
// Editing guide: to tweak a meter, change its entry below — nothing else.
// `schema.test.mjs` validates the structure (run it, or let CI run it) and the
// types below give editor autocomplete. The allowed property names are enforced
// by the validator, so a typo fails fast instead of silently doing nothing.
// See web/README.md → "Adding or editing a powermeter" for a worked example.

/** A field value as stored in app state (text/select/number → string, checkbox → boolean). */
export type FieldValue = string | boolean | undefined;
/** The flat key→value map for one meter's fields (or a tuning/option group). */
export type Fields = Record<string, FieldValue>;

/** A choice in a `select` field. */
export interface Option {
  value: string;
  label: string;
}

/** One form control, mapped to a config.ini key. */
export interface Field {
  key: string;
  label: string;
  help?: string;
  type: "text" | "number" | "password" | "select" | "checkbox";
  default?: string | boolean;
  placeholder?: string;
  options?: Option[];
  required?: boolean;
  phase?: boolean;
  advanced?: boolean;
  ey?: string;
}

/** How a source is read on an ESP32 (see docs/esphome-powermeters.md). */
export interface EsphomeSpec {
  kind: "homeassistant" | "mqtt" | "sml" | "modbus" | "http" | "unsupported";
  tier: "native" | "generic" | "alternate" | "unsupported";
  note: string;
  url1?: (f: Fields) => string;
  url3?: (f: Fields) => string;
  lambda1?: string | ((f: Fields) => string);
  lambda3?: string;
  jsonRoot?: string;
  haEntity?: (f: Fields) => string;
  headersField?: string;
  warn?: string | ((f: Fields) => string | null);
}

export interface Powermeter {
  id: string;
  label: string;
  section: string;
  blurb?: string;
  docPython?: string;
  fields: Field[];
  esphome: EsphomeSpec;
  phaseListKeys?: { topic: string; jsonPath: string };
}

export interface DeviceType {
  value: string;
  label: string;
  help: string;
}

export const SHELLY_TYPES: Option[] = [
  { value: "1PM", label: "Shelly 1PM" },
  { value: "PLUS1PM", label: "Shelly Plus 1PM" },
  { value: "EM", label: "Shelly EM" },
  { value: "3EM", label: "Shelly 3EM" },
  { value: "3EMPro", label: "Shelly 3EM Pro" },
];

// Device types AstraMeter can emulate (Python add-on). The shelly* family
// emulates a single Shelly meter; ct002/ct003 emulate Marstek CT clamps and
// are recommended when you run more than one battery.
export const DEVICE_TYPES: DeviceType[] = [
  {
    value: "shellypro3em",
    label: "Shelly Pro 3EM",
    help: "Best all-round choice for a single battery. Works with most Marstek firmware.",
  },
  {
    value: "shellyemg3",
    label: "Shelly EM Gen3",
    help: "Newer Shelly EM emulation. Use if your battery firmware expects it.",
  },
  {
    value: "shellyproem50",
    label: "Shelly Pro EM-50",
    help: "Single-phase Shelly Pro EM-50 emulation.",
  },
  {
    value: "ct002",
    label: "Marstek CT002 (HME-4)",
    help: "Marstek's CT meter. Recommended when you have two or more batteries that should share the load.",
  },
  {
    value: "ct003",
    label: "Marstek CT003 (HME-3)",
    help: "Marstek's other CT meter. Emulated identically to CT002 (same protocol) — from the battery's perspective there's no difference, so either works.",
  },
  {
    value: "shellypro3em_old",
    label: "Shelly Pro 3EM (old port)",
    help: "Forces the legacy port layout. Only pick this if support told you to.",
  },
  {
    value: "shellypro3em_new",
    label: "Shelly Pro 3EM (new port)",
    help: "Forces the new port layout. Only pick this if support told you to.",
  },
];

// Options that apply to *any* powermeter section (and can also be set in
// [GENERAL]). Shown under each meter's "Fine-tuning" disclosure.
export const PER_METER_TUNING: Field[] = [
  {
    key: "THROTTLE_INTERVAL",
    label: "Throttle interval (seconds)",
    help: "Minimum time between meter readings. Leave at 0 for fast local meters (Shelly). Slow/cloud sources like Home Assistant are happier at 2–3.",
    type: "number",
    default: "",
    placeholder: "0",
  },
  {
    key: "WAIT_FOR_NEXT_MESSAGE",
    label: "Wait for a fresh push",
    help: "For push-based meters (MQTT, Home Assistant, HomeWizard …) wait up to 2s for the newest reading. Turn off if your meter updates slower than 2s so replies stay snappy.",
    type: "select",
    default: "",
    options: [
      { value: "", label: "Default (on)" },
      { value: "true", label: "On — wait for fresh data" },
      { value: "false", label: "Off — always use last value" },
    ],
  },
  {
    key: "SMOOTH_TARGET_ALPHA",
    label: "Smoothing (EMA alpha)",
    help: "0 = off. Between 0 and 1: higher reacts faster, lower filters noise. Try 0.9 for fast meters, ~0.3 for slow ones.",
    type: "number",
    default: "",
    placeholder: "0",
  },
  {
    key: "MAX_SMOOTH_STEP",
    label: "Max smoothing step (W)",
    help: "Caps how many watts the smoothed value may change each cycle. 0 = no limit.",
    type: "number",
    default: "",
    placeholder: "0",
  },
  {
    key: "DEADBAND",
    label: "Deadband (W)",
    help: "Report 0 W when the reading is smaller than this, so the battery stops hunting around zero. 10–30 is sensible; 0 = off.",
    type: "number",
    default: "",
    placeholder: "0",
  },
  {
    key: "POWER_OFFSET",
    label: "Power offset (W)",
    help: "Added to every reading. A small negative value (e.g. -20) keeps a tiny import safety buffer so you never accidentally export.",
    type: "text",
    default: "",
    placeholder: "0",
    phase: true,
  },
  {
    key: "POWER_MULTIPLIER",
    label: "Power multiplier",
    help: "Each reading is multiplied by this. Use -1 to flip the sign if import/export are reversed, or a CT ratio to scale.",
    type: "text",
    default: "",
    placeholder: "1",
    phase: true,
  },
  {
    key: "HAMPEL_WINDOW",
    label: "Hampel window",
    help: "Outlier filter for flaky MQTT/HTTP sources. 0 = off; 5–7 works well.",
    type: "number",
    default: "",
    placeholder: "0",
  },
  {
    key: "HAMPEL_N_SIGMA",
    label: "Hampel sigma",
    help: "How far from the median counts as an outlier. Lower rejects more. Default 3.0.",
    type: "number",
    default: "",
    placeholder: "3.0",
  },
  {
    key: "HAMPEL_MIN_THRESHOLD",
    label: "Hampel min threshold (W)",
    help: "Floor for the outlier threshold during long constant readings. ~50 is a good start.",
    type: "number",
    default: "",
    placeholder: "0",
  },
  {
    key: "PID_KP",
    label: "PID proportional gain (Kp)",
    help: "0 = PID off. Set >0 to layer a net-zero controller on top of the meter. 0.5 is a safe start (single battery).",
    type: "number",
    default: "",
    placeholder: "0",
  },
  {
    key: "PID_KI",
    label: "PID integral gain (Ki)",
    help: "Usually leave at 0 — it risks integral wind-up.",
    type: "number",
    default: "",
    placeholder: "0",
    advanced: true,
  },
  {
    key: "PID_KD",
    label: "PID derivative gain (Kd)",
    help: "Leave at 0 — noisy on real meters.",
    type: "number",
    default: "",
    placeholder: "0",
    advanced: true,
  },
  {
    key: "PID_OUTPUT_MAX",
    label: "PID output max (W)",
    help: "Caps the PID output at ± this many watts. Default 800.",
    type: "number",
    default: "",
    placeholder: "800",
    advanced: true,
  },
  {
    key: "PID_MODE",
    label: "PID mode",
    help: "bias adds the PID output to the reading (recommended). replace uses only the PID output.",
    type: "select",
    default: "",
    options: [
      { value: "", label: "Default (bias)" },
      { value: "bias", label: "bias" },
      { value: "replace", label: "replace" },
    ],
    advanced: true,
  },
];

// The powermeter catalogue. `esphome` describes how to read the same source on
// an ESP32 (see docs/esphome-powermeters.md). `esphome.kind` drives the YAML
// generator:
//   'homeassistant' native HA sensor    'mqtt' native mqtt_subscribe
//   'sml' native sml component          'modbus' native modbus_controller
//   'http' generic http_request poll    'unsupported' no ESP path yet
export const POWERMETERS: Powermeter[] = [
  {
    id: "shelly",
    label: "Shelly energy meter",
    section: "SHELLY",
    blurb:
      "A Shelly plug or energy meter (1PM, Plus 1PM, EM, 3EM, 3EM Pro) on your local network.",
    docPython: "docs/powermeters.md#shelly",
    fields: [
      {
        key: "TYPE",
        label: "Shelly model",
        help: "Pick the exact model you own.",
        type: "select",
        options: SHELLY_TYPES,
        default: "1PM",
        required: true,
      },
      { key: "IP", label: "IP address", help: "The Shelly's local IP, e.g. 192.168.1.100.", type: "text", placeholder: "192.168.1.100", required: true },
      { key: "USER", label: "Username", help: "Only if you protected the Shelly web UI with a login.", type: "text", placeholder: "(optional)" },
      { key: "PASS", label: "Password", help: "Only if you set a login on the Shelly.", type: "password", placeholder: "(optional)" },
      { key: "METER_INDEX", label: "Meter index", help: "Which channel to read (e.g. meter1). Leave blank for the default.", type: "text", placeholder: "meter1", advanced: true },
    ],
    esphome: {
      kind: "http",
      tier: "generic",
      note: "Polls the Shelly's HTTP API. Single-phase reads RPC apower; 3-phase reads EM.GetStatus.",
      url1: (f) => `http://${f.IP || "192.168.1.100"}/rpc/Switch.GetStatus?id=0`,
      url3: (f) => `http://${f.IP || "192.168.1.100"}/rpc/EM.GetStatus?id=0`,
      lambda1: 'id(grid_l1).publish_state(root["apower"]);',
      lambda3:
        'id(grid_l1).publish_state(root["a_act_power"]);\n                    id(grid_l2).publish_state(root["b_act_power"]);\n                    id(grid_l3).publish_state(root["c_act_power"]);',
    },
  },
  {
    id: "tasmota",
    label: "Tasmota device",
    section: "TASMOTA",
    blurb: "A device flashed with Tasmota that exposes power under StatusSNS.",
    docPython: "docs/powermeters.md#tasmota",
    fields: [
      { key: "IP", label: "IP address", type: "text", placeholder: "192.168.1.101", required: true, help: "The Tasmota device's local IP." },
      { key: "USER", label: "Username", type: "text", placeholder: "(optional)", help: "Only if web auth is enabled." },
      { key: "PASS", label: "Password", type: "password", placeholder: "(optional)", help: "Only if web auth is enabled." },
      { key: "JSON_STATUS", label: "JSON status key", type: "text", default: "StatusSNS", placeholder: "StatusSNS", help: "Top-level key in the status response. Almost always StatusSNS." },
      { key: "JSON_PAYLOAD_MQTT_PREFIX", label: "Sensor prefix", type: "text", placeholder: "SML", help: "The sensor block name, e.g. SML or eBZ — see your Tasmota console." },
      { key: "JSON_POWER_MQTT_LABEL", label: "Power label", type: "text", placeholder: "Power", phase: true, help: "Field holding instantaneous power. For 3-phase use one per phase." },
      { key: "JSON_POWER_CALCULATE", label: "Calculate from in/out", type: "checkbox", default: false, help: "Turn on if your meter reports separate import and export fields instead of one signed value.", advanced: true },
      { key: "JSON_POWER_INPUT_MQTT_LABEL", label: "Import (in) label", type: "text", placeholder: "Power1", phase: true, advanced: true, help: "Import field name (needed when 'Calculate from in/out' is on)." },
      { key: "JSON_POWER_OUTPUT_MQTT_LABEL", label: "Export (out) label", type: "text", placeholder: "Power2", phase: true, advanced: true, help: "Export field name (needed when 'Calculate from in/out' is on)." },
    ],
    esphome: {
      kind: "http",
      tier: "generic",
      note: "Polls /cm?cmnd=status 10. Adjust the prefix/label in the lambda to match your meter.",
      url1: (f) => `http://${f.IP || "192.168.1.101"}/cm?cmnd=status%2010`,
      lambda1: (f) =>
        `id(grid_l1).publish_state(root["${f.JSON_STATUS || "StatusSNS"}"]["${f.JSON_PAYLOAD_MQTT_PREFIX || "SML"}"]["${String(f.JSON_POWER_MQTT_LABEL || "Power").split(",")[0].trim()}"]);`,
    },
  },
  {
    id: "shrdzm",
    label: "SHRDZM module",
    section: "SHRDZM",
    blurb: "A SHRDZM smart-meter module exposing /getLastData.",
    docPython: "docs/powermeters.md#shrdzm",
    fields: [
      { key: "IP", label: "IP address", type: "text", placeholder: "192.168.1.102", required: true, help: "The module's local IP." },
      { key: "USER", label: "Username", type: "text", placeholder: "shrdzm_user", required: true, help: "SHRDZM API user." },
      { key: "PASS", label: "Password", type: "password", placeholder: "shrdzm_pass", required: true, help: "SHRDZM API password." },
    ],
    esphome: {
      kind: "http",
      tier: "generic",
      note: "Reads OBIS 1.7.0 (import) minus 2.7.0 (export). Replace USER/PASS in the URL.",
      url1: (f) => `http://${f.IP || "192.168.1.102"}/getLastData?user=${f.USER || "USER"}&password=${f.PASS || "PASS"}`,
      lambda1:
        'float in_w = root["1.7.0"];\n                    float out_w = root["2.7.0"];\n                    id(grid_l1).publish_state(in_w - out_w);',
    },
  },
  {
    id: "emlog",
    label: "EmLog",
    section: "EMLOG",
    blurb: "An EmLog logger exposing getinformation.php.",
    docPython: "docs/powermeters.md#emlog",
    fields: [
      { key: "IP", label: "IP address", type: "text", placeholder: "192.168.1.103", required: true, help: "The EmLog's local IP." },
      { key: "METER_INDEX", label: "Meter index", type: "number", default: "0", placeholder: "0", help: "Which meter to read (0 for the first)." },
      { key: "JSON_POWER_CALCULATE", label: "Calculate from in/out", type: "checkbox", default: true, help: "EmLog reports import/export separately, so this is normally on.", advanced: true },
    ],
    esphome: {
      kind: "http",
      tier: "generic",
      note: "Reads Leistung170 (import) minus Leistung270 (export).",
      url1: (f) => `http://${f.IP || "192.168.1.103"}/pages/getinformation.php?heute&meterindex=${f.METER_INDEX || "0"}`,
      lambda1:
        'float in_w = root["Leistung170"];\n                    float out_w = root["Leistung270"];\n                    id(grid_l1).publish_state(in_w - out_w);',
    },
  },
  {
    id: "iobroker",
    label: "ioBroker",
    section: "IOBROKER",
    blurb: "An ioBroker instance with the simpleAPI adapter.",
    docPython: "docs/powermeters.md#iobroker",
    fields: [
      { key: "IP", label: "IP address", type: "text", placeholder: "192.168.1.104", required: true, help: "ioBroker host IP." },
      { key: "PORT", label: "Port", type: "number", default: "8087", placeholder: "8087", help: "simpleAPI adapter port (default 8087)." },
      { key: "CURRENT_POWER_ALIAS", label: "Current power object", type: "text", placeholder: "Alias.0.power", help: "ioBroker object id holding signed grid power." },
      { key: "POWER_CALCULATE", label: "Calculate from in/out", type: "checkbox", default: false, advanced: true, help: "Turn on to compute power from separate import/export objects." },
      { key: "POWER_INPUT_ALIAS", label: "Import object", type: "text", placeholder: "Alias.0.power_in", advanced: true, help: "Object id for imported power." },
      { key: "POWER_OUTPUT_ALIAS", label: "Export object", type: "text", placeholder: "Alias.0.power_out", advanced: true, help: "Object id for exported power." },
    ],
    esphome: {
      kind: "http",
      tier: "generic",
      note: "Polls /getBulk/<id>; reads arr[0].val. MQTT adapter + mqtt_subscribe is a simpler alternative.",
      url1: (f) => `http://${f.IP || "192.168.1.104"}:${f.PORT || "8087"}/getBulk/${f.CURRENT_POWER_ALIAS || "Alias.0.power"}`,
      lambda1: 'id(grid_l1).publish_state(arr[0]["val"]);',
      jsonRoot: "JsonArray arr",
    },
  },
  {
    id: "homeassistant",
    label: "Home Assistant entity",
    section: "HOMEASSISTANT",
    blurb:
      "Read a power sensor that already exists in your Home Assistant. The easiest option if HA already shows your grid power.",
    docPython: "docs/powermeters.md#homeassistant",
    fields: [
      { key: "IP", label: "Home Assistant IP", type: "text", placeholder: "192.168.1.105", required: true, help: "Your HA host's IP or hostname." },
      { key: "PORT", label: "Port", type: "number", default: "8123", placeholder: "8123", help: "HA port (default 8123)." },
      { key: "HTTPS", label: "Use HTTPS", type: "checkbox", default: false, help: "Turn on if you reach HA over https://." },
      { key: "ACCESSTOKEN", label: "Long-lived access token", type: "password", placeholder: "eyJ...", required: true, help: "Create one in HA → your profile → Long-lived access tokens." },
      { key: "CURRENT_POWER_ENTITY", label: "Power sensor entity", type: "text", placeholder: "sensor.grid_power", phase: true, help: "The entity that reports signed grid power. For 3-phase, give one per phase." },
      { key: "POWER_CALCULATE", label: "Calculate from in/out", type: "checkbox", default: false, advanced: true, help: "Use this instead of a single sensor if HA has separate import/export entities." },
      { key: "POWER_INPUT_ALIAS", label: "Import entity", type: "text", placeholder: "sensor.power_in", phase: true, advanced: true, help: "Imported-power entity (when calculating from in/out)." },
      { key: "POWER_OUTPUT_ALIAS", label: "Export entity", type: "text", placeholder: "sensor.power_out", phase: true, advanced: true, help: "Exported-power entity (when calculating from in/out)." },
      { key: "API_PATH_PREFIX", label: "API path prefix", type: "text", placeholder: "/core", advanced: true, help: "Only needed behind some reverse proxies (e.g. /core)." },
    ],
    esphome: {
      kind: "homeassistant",
      tier: "native",
      note: "The ESP subscribes to the HA entity over the native API.",
    },
  },
  {
    id: "vzlogger",
    label: "VZLogger",
    section: "VZLOGGER",
    blurb: "A vzlogger HTTP interface serving values by UUID.",
    docPython: "docs/powermeters.md#vzlogger",
    fields: [
      { key: "IP", label: "IP address", type: "text", placeholder: "192.168.1.106", required: true, help: "vzlogger host IP." },
      { key: "PORT", label: "Port", type: "number", default: "8080", placeholder: "8080", help: "vzlogger HTTP port (default 8080)." },
      { key: "UUID", label: "Channel UUID", type: "text", placeholder: "your-uuid", required: true, phase: true, help: "The channel UUID. For 3-phase give one UUID per phase." },
    ],
    esphome: {
      kind: "http",
      tier: "generic",
      note: "Reads data[0].tuples[0][1]. Or read the meter directly on the ESP with the native sml/dsmr component.",
      url1: (f) => `http://${f.IP || "192.168.1.106"}:${f.PORT || "8080"}/${String(f.UUID || "your-uuid").split(",")[0].trim()}`,
      lambda1: 'id(grid_l1).publish_state(root["data"][0]["tuples"][0][1]);',
    },
  },
  {
    id: "esphome",
    label: "Another ESPHome device",
    section: "ESPHOME",
    blurb: "Poll another ESPHome node's web-server REST API.",
    docPython: "docs/powermeters.md#esphome",
    fields: [
      { key: "IP", label: "IP address", type: "text", placeholder: "192.168.1.107", required: true, help: "The other ESPHome device's IP." },
      { key: "PORT", label: "Port", type: "number", default: "6052", placeholder: "6052", help: "Its web-server port (default 6052)." },
      { key: "DOMAIN", label: "Entity domain", type: "text", placeholder: "sensor", required: true, help: "Usually 'sensor'." },
      { key: "ID", label: "Entity id", type: "text", placeholder: "grid_power", required: true, help: "The object id of the power entity on that device." },
    ],
    esphome: {
      kind: "homeassistant",
      tier: "native",
      note: "On the ESP there is no bridge — import the other node's entity via Home Assistant (shown), or define the sensor in the same YAML.",
      // This source names its entity explicitly rather than via CURRENT_POWER_ENTITY.
      haEntity: (f) => `sensor.${f.ID || "grid_power"}`,
    },
  },
  {
    id: "amis_reader",
    label: "AMIS reader",
    section: "AMIS_READER",
    blurb: "An AMIS reader serving /rest with a signed saldo field.",
    docPython: "docs/powermeters.md#amis-reader",
    fields: [
      { key: "IP", label: "IP address", type: "text", placeholder: "192.168.1.108", required: true, help: "The AMIS reader's IP." },
    ],
    esphome: {
      kind: "http",
      tier: "generic",
      note: "Reads the signed 'saldo' field from /rest.",
      url1: (f) => `http://${f.IP || "192.168.1.108"}/rest`,
      lambda1: 'id(grid_l1).publish_state(root["saldo"]);',
    },
  },
  {
    id: "modbus",
    label: "Modbus meter (TCP/UDP)",
    section: "MODBUS",
    blurb: "A meter reachable over Modbus TCP or UDP.",
    docPython: "docs/powermeters.md#modbus-tcpudp",
    fields: [
      { key: "HOST", label: "Host", type: "text", placeholder: "192.168.1.100", required: true, help: "Meter / gateway IP." },
      { key: "PORT", label: "Port", type: "number", default: "502", placeholder: "502", help: "Modbus port (default 502)." },
      { key: "UNIT_ID", label: "Unit / slave id", type: "number", default: "1", placeholder: "1", help: "The device's Modbus unit id." },
      { key: "ADDRESS", label: "Register address", type: "number", default: "0", placeholder: "0", help: "Address of the power register (from your meter's register map)." },
      { key: "COUNT", label: "Register count", type: "number", default: "1", placeholder: "1", help: "How many registers the value spans (1 for 16-bit, 2 for 32-bit)." },
      {
        key: "DATA_TYPE",
        label: "Data type",
        type: "select",
        default: "UINT16",
        help: "Match your meter's register type.",
        options: [
          { value: "UINT16", label: "UINT16" },
          { value: "INT16", label: "INT16" },
          { value: "UINT32", label: "UINT32" },
          { value: "INT32", label: "INT32" },
          { value: "FLOAT32", label: "FLOAT32" },
        ],
      },
      { key: "BYTE_ORDER", label: "Byte order", type: "select", default: "BIG", options: [{ value: "BIG", label: "BIG" }, { value: "LITTLE", label: "LITTLE" }], advanced: true, help: "Endianness of bytes within a register." },
      { key: "WORD_ORDER", label: "Word order", type: "select", default: "BIG", options: [{ value: "BIG", label: "BIG" }, { value: "LITTLE", label: "LITTLE" }], advanced: true, help: "Order of words for multi-register values." },
      { key: "REGISTER_TYPE", label: "Register type", type: "select", default: "HOLDING", options: [{ value: "HOLDING", label: "HOLDING" }, { value: "INPUT", label: "INPUT" }], help: "Holding or input register." },
      { key: "TRANSPORT", label: "Transport", type: "select", default: "TCP", options: [{ value: "TCP", label: "TCP" }, { value: "UDP", label: "UDP" }], help: "TCP is the default; UDP for meters that need it." },
    ],
    esphome: {
      kind: "modbus",
      tier: "native",
      note: "ESPHome's modbus_controller is RS485 serial only — wire RS485 to the ESP or use a TCP↔RTU gateway. Map your data type onto value_type.",
      warn: (f) =>
        (f.TRANSPORT || "TCP") === "TCP"
          ? "ESPHome's modbus_controller is RS485 serial only — wire RS485 to the ESP or use a Modbus-TCP↔RTU gateway."
          : null,
    },
  },
  {
    id: "mqtt",
    label: "MQTT topic",
    section: "MQTT",
    blurb: "Subscribe to a power value published on an MQTT broker.",
    docPython: "docs/powermeters.md#mqtt",
    fields: [
      { key: "BROKER", label: "Broker host", type: "text", placeholder: "broker.example.com", help: "Broker hostname/IP. Leave blank if you use a full URI below." },
      { key: "PORT", label: "Port", type: "number", default: "1883", placeholder: "1883", help: "Broker port (1883 plain, 8883 TLS)." },
      { key: "TLS", label: "Use TLS (mqtts)", type: "checkbox", default: false, help: "Connect securely over mqtts://.", advanced: true },
      { key: "URI", label: "Full broker URI", type: "text", placeholder: "mqtt://user:pass@broker:1883", advanced: true, help: "Optional: provide everything in one URI instead of the fields above." },
      { key: "TOPIC", label: "Topic", type: "text", placeholder: "home/powermeter", help: "Topic carrying the value. For 3-phase, switch to 3-phase to enter one topic per phase." },
      { key: "JSON_PATH", label: "JSON path", type: "text", placeholder: "$.power", advanced: true, help: "Only if the payload is JSON. JSONPath to the number, e.g. $.power. Omit for a plain number." },
      { key: "USERNAME", label: "Username", type: "text", placeholder: "(optional)", advanced: true, help: "Broker username, if required." },
      { key: "PASSWORD", label: "Password", type: "password", placeholder: "(optional)", advanced: true, help: "Broker password, if required." },
    ],
    // 3-phase handled specially in generate.js (TOPICS / JSON_PATHS)
    phaseListKeys: { topic: "TOPICS", jsonPath: "JSON_PATHS" },
    esphome: {
      kind: "mqtt",
      tier: "native",
      note: "Uses mqtt_subscribe for plain numbers; a JSON payload uses on_json_message into a template sensor.",
    },
  },
  {
    id: "json_http",
    label: "JSON over HTTP",
    section: "JSON_HTTP",
    blurb: "Any HTTP endpoint that returns JSON containing the power value.",
    docPython: "docs/powermeters.md#json-http",
    fields: [
      { key: "URL", label: "URL", type: "text", placeholder: "http://example.com/api", required: true, help: "The endpoint returning JSON." },
      { key: "JSON_PATHS", label: "JSON path(s)", type: "text", placeholder: "$.power", phase: true, help: "JSONPath to the value. For 3-phase, give one path per phase. Supports extensions like `.split(...)` to strip a unit." },
      { key: "USERNAME", label: "Username", type: "text", placeholder: "(optional)", advanced: true, help: "HTTP basic-auth user." },
      { key: "PASSWORD", label: "Password", type: "password", placeholder: "(optional)", advanced: true, help: "HTTP basic-auth password." },
      { key: "HEADERS", label: "Extra headers", type: "text", placeholder: "Authorization: Bearer token", advanced: true, help: "Separate multiple with ';'. Format: 'Key: Value'." },
    ],
    esphome: {
      kind: "http",
      tier: "native",
      note: "Generic http_request poll. Headers and basic auth are supported on the get action.",
      url1: (f) => String(f.URL || "http://example.com/api"),
      lambda1: 'id(grid_l1).publish_state(root["power"]);',
      headersField: "HEADERS",
    },
  },
  {
    id: "tq_em",
    label: "TQ Energy Manager",
    section: "TQ_EM",
    blurb: "A TQ Energy Manager (EM420 and similar).",
    docPython: "docs/powermeters.md#tq-energy-manager",
    fields: [
      { key: "IP", label: "IP address", type: "text", placeholder: "192.168.1.100", required: true, help: "The energy manager's IP." },
      { key: "PASSWORD", label: "Password", type: "password", placeholder: "(optional)", help: "Device password, if set." },
      { key: "TIMEOUT", label: "Timeout (seconds)", type: "number", default: "5.0", placeholder: "5.0", advanced: true, help: "Request timeout." },
    ],
    esphome: {
      kind: "modbus",
      tier: "alternate",
      note: "The proprietary HTTP API has no ESP port — read the EM over its Modbus or MQTT export instead. Set address/value_type from the TQ register map.",
      warn: () => "The TQ proprietary API has no ESP port — this reads the device's Modbus export instead. Set address/value_type from the TQ register map.",
    },
  },
  {
    id: "homewizard",
    label: "HomeWizard P1",
    section: "HOMEWIZARD",
    blurb: "A HomeWizard P1 meter / energy socket via the local v2 WebSocket API.",
    docPython: "docs/powermeters.md#homewizard",
    fields: [
      { key: "IP", label: "IP address", type: "text", placeholder: "192.168.1.110", required: true, help: "The HomeWizard device's IP." },
      { key: "TOKEN", label: "API token", type: "password", placeholder: "32-char hex token", required: true, help: "Obtain once via POST /api/user while pressing the device button." },
      { key: "SERIAL", label: "Device serial", type: "text", placeholder: "your_device_serial", required: true, help: "Printed on the device / shown in the app." },
      { key: "VERIFY_SSL", label: "Verify TLS certificate", type: "select", default: "", options: [{ value: "", label: "Default (on)" }, { value: "True", label: "On" }, { value: "False", label: "Off (insecure)" }], advanced: true, help: "Turn off only if you get certificate errors on a trusted LAN." },
    ],
    esphome: {
      kind: "http",
      tier: "alternate",
      note: "The v2 WebSocket API has no ESP port. Enable Local API and poll the v1 HTTP API instead; grid power is active_power_w.",
      url1: (f) => `http://${f.IP || "192.168.1.110"}/api/v1/data`,
      lambda1: 'id(grid_l1).publish_state(root["active_power_w"]);',
    },
  },
  {
    id: "envoy",
    label: "Enphase Envoy (IQ Gateway)",
    section: "ENVOY",
    blurb: "An Enphase IQ Gateway / Envoy with consumption CTs, via the local HTTPS API.",
    docPython: "docs/powermeters.md#enphase-envoy-iq-gateway",
    fields: [
      { key: "HOST", label: "Host", type: "text", placeholder: "192.168.1.120", required: true, help: "The Envoy's IP." },
      { key: "TOKEN", label: "JWT token", type: "password", placeholder: "eyJ...", help: "Recommended: a long-lived token from entrez.enphaseenergy.com." },
      { key: "USERNAME", label: "Enlighten email", type: "text", placeholder: "you@example.com", advanced: true, help: "Alternative to a token: AstraMeter fetches one via the Enphase cloud (no MFA)." },
      { key: "PASSWORD", label: "Enlighten password", type: "password", placeholder: "(optional)", advanced: true, help: "Used with the email above to auto-fetch a token." },
      { key: "SERIAL", label: "Envoy serial", type: "text", placeholder: "123456789012", advanced: true, help: "Needed for the cloud token flow." },
      { key: "VERIFY_SSL", label: "Verify TLS certificate", type: "select", default: "", options: [{ value: "", label: "Default (off)" }, { value: "True", label: "On" }, { value: "False", label: "Off" }], advanced: true, help: "The Envoy uses a self-signed cert, so this defaults to off for the local connection." },
    ],
    esphome: { kind: "unsupported", tier: "unsupported", note: "There is no ESPHome component for the Enphase Envoy yet. Use the Python add-on for this meter." },
  },
  {
    id: "sma_energy_meter",
    label: "SMA Energy Meter",
    section: "SMA_ENERGY_METER",
    blurb: "An SMA Energy Meter / Sunny Home Manager via Speedwire multicast.",
    docPython: "docs/powermeters.md#sma-energy-meter",
    fields: [
      { key: "MULTICAST_GROUP", label: "Multicast group", type: "text", default: "239.12.255.254", placeholder: "239.12.255.254", help: "SMA's default multicast group — usually leave as-is." },
      { key: "PORT", label: "Port", type: "number", default: "9522", placeholder: "9522", help: "Speedwire port (default 9522)." },
      { key: "SERIAL_NUMBER", label: "Meter serial", type: "number", default: "0", placeholder: "0", help: "0 auto-detects the first meter; set a serial to pin one." },
      { key: "INTERFACE", label: "Bind interface IP", type: "text", placeholder: "(all interfaces)", advanced: true, help: "Optional: bind to a specific network interface IP." },
    ],
    esphome: { kind: "unsupported", tier: "unsupported", note: "There is no ESPHome component for SMA Speedwire yet. Use the Python add-on for this meter." },
  },
  {
    id: "fritz",
    label: "FRITZ!Smart Energy 250",
    section: "FRITZ",
    blurb: "An AVM FRITZ!Smart Energy 250 meter read head, via the FRITZ!Box AHA-HTTP-Interface. Power it over USB — on battery it only updates every ~2 min, too slow for battery control.",
    docPython: "docs/powermeters.md#fritzsmart-energy-250",
    fields: [
      { key: "HOST", label: "FRITZ!Box host", type: "text", default: "fritz.box", placeholder: "fritz.box", required: true, help: "The FRITZ!Box hostname or IP the read head is paired with." },
      { key: "USER", label: "FRITZ!Box user", type: "text", placeholder: "smarthome", required: true, help: "A FRITZ!Box user with the Smart Home permission (Home Network → FRITZ!Box Users)." },
      { key: "PASSWORD", label: "Password", type: "password", placeholder: "(your FRITZ!Box password)", required: true, help: "Password for that FRITZ!Box user." },
      { key: "AIN", label: "AIN", type: "text", placeholder: "12345 0123456", required: true, help: "The read head's AIN from Home Network → SmartHome. The signed grid-import (-1) branch is read by default; append -1 or -2 to pick a branch." },
      { key: "HTTPS", label: "Use HTTPS", type: "checkbox", default: false, advanced: true, help: "Reach the FRITZ!Box over https:// instead of http://." },
      { key: "VERIFY_SSL", label: "Verify TLS certificate", type: "select", default: "", options: [{ value: "", label: "Default (on)" }, { value: "True", label: "On" }, { value: "False", label: "Off" }], advanced: true, help: "FRITZ!Box certs are self-signed — turn off if HTTPS fails verification." },
      { key: "TIMEOUT", label: "Timeout (seconds)", type: "number", default: "10.0", placeholder: "10.0", advanced: true, help: "Request timeout." },
    ],
    esphome: {
      kind: "homeassistant",
      tier: "alternate",
      note: "The FRITZ!Box AHA-HTTP login (challenge-response) and XML device list have no ESP port. Bridge via Home Assistant's FRITZ!Smart Home integration and read the resulting power sensor over the native API.",
      haEntity: () => "sensor.fritz_smart_energy_power",
      warn: () => "The FRITZ!Box AHA-HTTP API has no ESP port — this reads the power sensor exposed by Home Assistant's FRITZ!Smart Home integration instead. Set entity_id to your meter's sensor.",
    },
  },
  {
    id: "script",
    label: "Custom script",
    section: "SCRIPT",
    blurb: "Run your own script that prints up to 3 integers (one per phase).",
    docPython: "docs/powermeters.md#script",
    fields: [
      { key: "COMMAND", label: "Command", type: "text", placeholder: "/path/to/your/script.sh", required: true, help: "Full path to an executable that prints power value(s) to stdout." },
    ],
    esphome: { kind: "unsupported", tier: "unsupported", note: "An ESP32 can't run host scripts, so this source has no ESPHome equivalent." },
  },
  {
    id: "sml",
    label: "SML meter (IR head)",
    section: "SML",
    blurb: "A smart meter read over a serial IR head that emits SML.",
    docPython: "docs/powermeters.md#sml",
    fields: [
      { key: "SERIAL", label: "Serial device", type: "text", placeholder: "/dev/ttyUSB0", required: true, help: "Path to the serial interface, e.g. /dev/ttyUSB0." },
      { key: "OBIS_POWER_CURRENT", label: "OBIS: aggregate power", type: "text", placeholder: "0100100700ff", advanced: true, help: "12-hex OBIS code. Leave blank for the common eHZ default." },
      { key: "OBIS_POWER_L1", label: "OBIS: L1", type: "text", placeholder: "0100240700ff", advanced: true, help: "Per-phase OBIS code (optional)." },
      { key: "OBIS_POWER_L2", label: "OBIS: L2", type: "text", placeholder: "0100380700ff", advanced: true, help: "Per-phase OBIS code (optional)." },
      { key: "OBIS_POWER_L3", label: "OBIS: L3", type: "text", placeholder: "01004c0700ff", advanced: true, help: "Per-phase OBIS code (optional)." },
    ],
    esphome: {
      kind: "sml",
      tier: "native",
      note: "Wire a photo-transistor to a UART RX pin and use the native sml component.",
    },
  },
];

export function getPowermeter(id: string): Powermeter | undefined {
  return POWERMETERS.find((p) => p.id === id);
}

// Meters that can read three phases. Others are single-phase only.
export const PHASE_CAPABLE: Set<string> = new Set([
  "shelly",
  "tasmota",
  "homeassistant",
  "vzlogger",
  "mqtt",
  "json_http",
  "sml",
]);

// CT002/CT003 active-steering options. Grouped for the form. These live in the
// [CT002]/[CT003] section (Python) or the ct002: block (ESPHome). `eyKey` is
// the ESPHome key when it differs / lives in a sub-block.
export const CT_BASIC: Field[] = [
  { key: "CT_MAC", ey: "ct_mac", label: "CT MAC", help: "12 hex digits from the Marstek app. Leave blank to answer any battery and mirror its MAC.", type: "text", placeholder: "(blank = any)" },
  { key: "UDP_PORT", ey: "udp_port", label: "UDP port", help: "Port the emulator listens on. Default 12345.", type: "number", placeholder: "12345" },
  { key: "WIFI_RSSI", ey: "wifi_rssi", label: "Reported WiFi RSSI", help: "Signal strength reported back to the battery. Default -50.", type: "number", placeholder: "-50" },
  { key: "CONSUMER_TTL", ey: "consumer_ttl", label: "Consumer TTL (seconds)", help: "Forget a battery this long after it goes silent. Blank (default) adapts to each battery's poll rate (~2 missed polls, like the real CT); set a number for a fixed window.", type: "number", placeholder: "(adaptive)" },
  { key: "DEDUPE_TIME_WINDOW", ey: "dedupe_window", label: "Dedupe window (seconds)", help: "Drop duplicate polls from the same battery within this window. 0 = off.", type: "number", placeholder: "0" },
];

export const CT_ACTIVE: Field[] = [
  { key: "ACTIVE_CONTROL", ey: "active_control", label: "Active control", help: "On (default): the emulator smooths the reading, splits the target across batteries and balances them. Off: relay raw readings and let batteries decide.", type: "select", default: "", options: [{ value: "", label: "Default (on)" }, { value: "True", label: "On" }, { value: "False", label: "Off" }] },
];

export const CT_BALANCER: Field[] = [
  { key: "FAIR_DISTRIBUTION", ey: "fair_distribution", label: "Fair distribution", help: "Share load evenly across batteries. Only matters with 2+ batteries.", type: "select", default: "", options: [{ value: "", label: "Default (on)" }, { value: "True", label: "On" }, { value: "False", label: "Off" }] },
  { key: "BALANCE_GAIN", ey: "balance_gain", label: "Balance gain", help: "How aggressively to correct imbalance. 0 = equal split only; 0.3–0.5 = faster. Default 0.2.", type: "number", placeholder: "0.2" },
  { key: "BALANCE_DEADBAND", ey: "balance_deadband", label: "Balance deadband (W)", help: "Ignore imbalance smaller than this. Default 15.", type: "number", placeholder: "15" },
  { key: "MAX_CORRECTION_PER_STEP", ey: "max_correction_per_step", label: "Max correction per step (W)", help: "Cap on per-cycle balance correction. Default 80.", type: "number", placeholder: "80" },
  { key: "ERROR_BOOST_THRESHOLD", ey: "error_boost_threshold", label: "Error boost threshold (W)", help: "Above this imbalance, gain is boosted. Default 150.", type: "number", placeholder: "150" },
  { key: "ERROR_BOOST_MAX", ey: "error_boost_max", label: "Error boost max", help: "Maximum extra gain multiplier. Default 0.5.", type: "number", placeholder: "0.5" },
  { key: "ERROR_REDUCE_THRESHOLD", ey: "error_reduce_threshold", label: "Error reduce threshold (W)", help: "Below this imbalance, gain is scaled down. Default 20.", type: "number", placeholder: "20" },
  { key: "MAX_TARGET_STEP", ey: "max_target_step", label: "Max target step (W)", help: "Hard clamp on per-cycle target change. 0 = off.", type: "number", placeholder: "0" },
  { key: "OSC_DAMP_MAX", ey: "osc_damp_max", label: "Oscillation damping", help: "Max gain reduction while a battery hunts (keeps reversing direction) under a laggy/delayed meter; genuine load/solar steps stay at full speed. 0 = off. Default 0.8.", type: "number", placeholder: "0.8" },
  { key: "OSC_DAMP_ALPHA", ey: "osc_damp_alpha", label: "Oscillation damping ramp-up", help: "How fast the hunting detector engages on repeated reversals. Higher = sooner/stronger. Default 0.15.", type: "number", placeholder: "0.15" },
  { key: "OSC_DAMP_DECAY", ey: "osc_damp_decay", label: "Oscillation damping decay", help: "How fast the detector relaxes when no longer hunting. Default 0.1.", type: "number", placeholder: "0.1" },
  { key: "OSC_DAMP_THRESHOLD", ey: "osc_damp_threshold", label: "Oscillation damping threshold (W)", help: "Corrections larger than this are a genuine demand step (kettle, solar ramp) and are never damped. Default 450.", type: "number", placeholder: "450" },
  { key: "GRID_PREDICT_TRUST", ey: "grid_predict_trust", label: "Grid prediction", help: "Keeps the grid closer to zero (less import/export, less overshoot and hunting) by adapting automatically to your power meter, including meters that report with a delay. 0.5 (default) needs no tuning; lower reacts more cautiously, 0 = off.", type: "number", placeholder: "0.5" },
];

// Applies to each DC-only battery individually (also with a single battery,
// independent of balancing). The ESPHome key lives under the `balancer:` block.
export const CT_DC_KEEPALIVE: Field[] = [
  { key: "MIN_DC_OUTPUT", ey: "min_dc_output", label: "Min DC output (W)", help: "Minimum discharge to keep a DC battery's inverter (e.g. Marstek B2500) from switching off at 0 W and sleeping under high PV surplus. Applied per DC-only battery; AC batteries (Venus) and Jupiter are unaffected. 0 = off; recommended >= 20.", type: "number", placeholder: "0" },
];

export const CT_EFFICIENCY: Field[] = [
  { key: "MIN_EFFICIENT_POWER", ey: "min_efficient_power", label: "Min efficient power (W)", help: "Concentrate small loads on fewer batteries so each stays efficient. 0 = off. Not recommended for DC batteries.", type: "number", placeholder: "0" },
  { key: "EFFICIENCY_ROTATION_INTERVAL", ey: "efficiency_rotation_interval", label: "Rotation interval (seconds)", help: "How often priority rotates between batteries. Default 900 (min 10).", type: "number", placeholder: "900" },
  { key: "PROBE_MIN_POWER", ey: "probe_min_power", label: "Probe min power (W)", help: "Floor sent when probing a newly promoted battery. Default 80.", type: "number", placeholder: "80" },
  { key: "EFFICIENCY_FADE_ALPHA", ey: "efficiency_fade_alpha", label: "Fade alpha", help: "How fast the old battery fades after a successful probe. Default 0.15.", type: "number", placeholder: "0.15" },
  { key: "EFFICIENCY_SATURATION_THRESHOLD", ey: "efficiency_saturation_threshold", label: "Saturation swap threshold", help: "Swap out a battery that can't follow its target. Default 0.4; raise for slow meters.", type: "number", placeholder: "0.4" },
];

export const CT_SATURATION: Field[] = [
  { key: "SATURATION_DETECTION", ey: "enabled", label: "Saturation detection", help: "Detect a full/empty battery and back off. On by default.", type: "select", default: "", options: [{ value: "", label: "Default (on)" }, { value: "True", label: "On" }, { value: "False", label: "Off" }] },
  { key: "SATURATION_ALPHA", ey: "alpha", label: "Saturation alpha", help: "How fast saturation is declared/recovered. Default 0.15.", type: "number", placeholder: "0.15" },
  { key: "MIN_TARGET_FOR_SATURATION", ey: "min_target", label: "Min target for saturation (W)", help: "Ignore saturation below this target. Default 20.", type: "number", placeholder: "20" },
  { key: "SATURATION_GRACE_SECONDS", ey: "grace_seconds", label: "Probe window (seconds)", help: "Max probe window when promoting a battery. Default 90.", type: "number", placeholder: "90" },
  { key: "SATURATION_STALL_TIMEOUT_SECONDS", ey: "stall_timeout_seconds", label: "Stall timeout (seconds)", help: "Stall escape for non-probe cases. Default 60.", type: "number", placeholder: "60" },
  { key: "SATURATION_DECAY_FACTOR", ey: "decay_factor", label: "Saturation decay factor", help: "How fast a swapped-out battery becomes eligible again. Default 0.995.", type: "number", placeholder: "0.995" },
];

export const MARSTEK_FIELDS: Field[] = [
  { key: "MAILBOX", ey: "mailbox", label: "Marstek account email", help: "Used once to register a managed CT device in the Marstek cloud.", type: "text", placeholder: "you@example.com" },
  { key: "PASSWORD", ey: "password", label: "Marstek account password", help: "Only needed for the one-time registration; you can remove it after.", type: "password", placeholder: "your_password" },
  { key: "BASE_URL", ey: "base_url", label: "API base URL", help: "https://eu.hamedata.com (EU) or https://us.hamedata.com (US).", type: "select", default: "https://eu.hamedata.com", options: [{ value: "https://eu.hamedata.com", label: "EU (eu.hamedata.com)" }, { value: "https://us.hamedata.com", label: "US (us.hamedata.com)" }] },
  { key: "TIMEZONE", ey: "timezone", label: "Timezone", help: "Your IANA timezone, e.g. Europe/Berlin.", type: "text", placeholder: "Europe/Berlin" },
];

export const MQTT_INSIGHTS_FIELDS: Field[] = [
  { key: "BROKER", ey: "__uri", label: "Broker host", help: "MQTT broker for publishing internal state to Home Assistant.", type: "text", placeholder: "192.168.1.100" },
  { key: "PORT", label: "Port", help: "Broker port. Default 1883.", type: "number", placeholder: "1883" },
  { key: "USERNAME", label: "Username", help: "Broker username, if required.", type: "text", placeholder: "(optional)" },
  { key: "PASSWORD", label: "Password", help: "Broker password, if required.", type: "password", placeholder: "(optional)" },
  { key: "TLS", label: "Use TLS", help: "Connect to the broker over TLS.", type: "checkbox", default: false, advanced: true },
  { key: "BASE_TOPIC", ey: "base_topic", label: "Base topic", help: "Namespace for all messages. Default astrameter.", type: "text", placeholder: "astrameter", advanced: true },
  { key: "HA_DISCOVERY", ey: "ha_discovery", label: "Home Assistant discovery", help: "Auto-create HA entities via MQTT discovery. On by default.", type: "select", default: "", options: [{ value: "", label: "Default (on)" }, { value: "true", label: "On" }, { value: "false", label: "Off" }], advanced: true },
  { key: "HA_DISCOVERY_PREFIX", ey: "ha_discovery_prefix", label: "Discovery prefix", help: "HA discovery prefix. Default homeassistant.", type: "text", placeholder: "homeassistant", advanced: true },
  { key: "MARSTEK_MQTT_ENABLED", ey: "marstek_mqtt_enabled", label: "Answer Marstek app polls", help: "Reply to Marstek-app MQTT polls on this broker (needs hame-relay ≥ 1.3.5 for live readings).", type: "select", default: "", options: [{ value: "", label: "Default (on)" }, { value: "true", label: "On" }, { value: "false", label: "Off" }], advanced: true },
  { key: "MARSTEK_MQTT_INTERVAL", ey: "marstek_mqtt_interval", label: "Marstek broadcast interval (s)", help: "Seconds between aggregate broadcasts when the app is quiet. 0 = polls only.", type: "number", placeholder: "300", advanced: true },
];

export const ESP_BOARDS: Option[] = [
  { value: "esp32-s3-devkitc-1", label: "ESP32-S3 DevKitC-1 (recommended)" },
  { value: "esp32dev", label: "Generic ESP32 (esp32dev)" },
  { value: "esp32-c3-devkitm-1", label: "ESP32-C3 DevKitM-1" },
  { value: "esp32-s3-devkitm-1", label: "ESP32-S3 DevKitM-1" },
  { value: "nodemcu-32s", label: "NodeMCU-32S" },
  { value: "m5stack-atom", label: "M5Stack Atom" },
];

// Affiliate links for the hardware we recommend to beginners.
export const HARDWARE = {
  single: {
    label: "ESP32-S3 DevKitC-1 (single board)",
    url: "https://amzn.to/3POffyU",
  },
  pack3: {
    label: "ESP32-S3 DevKitC-1 (3-pack — spares / multiple setups)",
    url: "https://amzn.to/4wUAtvN",
  },
};
