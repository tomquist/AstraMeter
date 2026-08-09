// Lightweight assertions for the config generators. Run with:
//   node web/js/generate.test.mjs
import { generateConfigIni, generateEsphome, generateHomeAssistant } from "./generate.js";

let failures = 0;
function ok(cond, msg) {
  if (!cond) {
    failures++;
    console.error("✗ " + msg);
  } else {
    console.log("✓ " + msg);
  }
}
function has(haystack, needle, msg) {
  ok(haystack.includes(needle), `${msg}\n    expected to find: ${JSON.stringify(needle)}`);
}
function lacks(haystack, needle, msg) {
  ok(!haystack.includes(needle), `${msg}\n    expected NOT to find: ${JSON.stringify(needle)}`);
}

// ── config.ini: simple Shelly ────────────────────────────────────────────────
const shelly = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"], skipPowermeterTest: false },
  meters: [{ type: "shelly", phases: 1, fields: { TYPE: "3EMPro", IP: "192.168.1.50" }, tuning: {} }],
});
has(shelly, "[GENERAL]", "shelly: has GENERAL");
has(shelly, "DEVICE_TYPE = shellypro3em", "shelly: device type");
has(shelly, "[SHELLY]", "shelly: section header");
has(shelly, "TYPE = 3EMPro", "shelly: model");
has(shelly, "IP = 192.168.1.50", "shelly: ip");
lacks(shelly, "USER =", "shelly: omits blank user");

// ── config.ini: HA 3-phase + tuning + transform ──────────────────────────────
const ha = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["ct002"], throttleInterval: "2" },
  meters: [
    {
      type: "homeassistant",
      phases: 3,
      fields: {
        IP: "10.0.0.5",
        ACCESSTOKEN: "tok",
        CURRENT_POWER_ENTITY: "sensor.l1, sensor.l2, sensor.l3",
        HTTPS: true,
      },
      tuning: { THROTTLE_INTERVAL: "3", POWER_MULTIPLIER: "-1", PID_KP: "0.5" },
    },
  ],
  ct: {
    fields: {
      CT_MAC: "001122334455",
      BALANCE_GAIN: "0.3",
      ACTIVE_CONTROL: "True",
      CLOUD_REPORTING: "True",
      CLOUD_REPORTING_INTERVAL: "30",
    },
  },
});
has(ha, "[HOMEASSISTANT]", "ha: section");
has(ha, "HTTPS = True", "ha: https bool");
has(ha, "CURRENT_POWER_ENTITY = sensor.l1, sensor.l2, sensor.l3", "ha: 3-phase entity list");
has(ha, "THROTTLE_INTERVAL = 3", "ha: per-meter throttle override");
has(ha, "POWER_MULTIPLIER = -1", "ha: transform");
has(ha, "PID_KP = 0.5", "ha: pid");
has(ha, "[CT002]", "ha: CT002 section emitted");
has(ha, "CT_MAC = 001122334455", "ha: ct mac");
has(ha, "BALANCE_GAIN = 0.3", "ha: balancer option");
has(ha, "CLOUD_REPORTING = True", "ha: cloud reporting on");
has(ha, "CLOUD_REPORTING_INTERVAL = 30", "ha: cloud reporting interval");

// ── config.ini: EmLog calculate defaults to True (schema default) ────────────
const emlog = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "emlog", phases: 1, fields: { IP: "10.0.0.7" }, tuning: {} }],
});
has(emlog, "JSON_POWER_CALCULATE = True", "emlog: calculate defaults to True");
const tasmotaCalc = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "tasmota", phases: 1, fields: { IP: "10.0.0.8" }, tuning: {} }],
});
lacks(tasmotaCalc, "JSON_POWER_CALCULATE", "tasmota: calculate omitted by default (Python default False)");

// ── config.ini: MQTT 3-phase topics ──────────────────────────────────────────
const mqtt = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "mqtt", phases: 3, fields: { BROKER: "b.example", TOPIC: "p/l1, p/l2, p/l3" }, tuning: {} }],
});
has(mqtt, "TOPICS = p/l1, p/l2, p/l3", "mqtt: TOPIC promoted to TOPICS in 3-phase");
lacks(mqtt, "\nTOPIC =", "mqtt: no singular TOPIC");

// ── config.ini: Fronius single- vs three-phase (PER_PHASE flag) ──────────────
const fronius1 = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "fronius", phases: 1, fields: { IP: "10.0.0.9" }, tuning: {} }],
});
has(fronius1, "[FRONIUS]", "fronius: section header");
lacks(fronius1, "PER_PHASE", "fronius: no PER_PHASE in single-phase");
const fronius3 = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "fronius", phases: 3, fields: { IP: "10.0.0.9" }, tuning: {} }],
});
has(fronius3, "PER_PHASE = True", "fronius: PER_PHASE emitted in three-phase");
const refoss1 = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellyproem50"] },
  meters: [{ type: "refoss", phases: 1, fields: { IP: "192.168.1.150", CHANNELS: "1" }, tuning: {} }],
});
has(refoss1, "[REFOSS]", "refoss: section header");
has(refoss1, "IP = 192.168.1.150", "refoss: IP emitted");
has(refoss1, "CHANNELS = 1", "refoss: single-phase CHANNELS");
const refoss3 = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "refoss", phases: 3, fields: { IP: "192.168.1.150", CHANNELS: "1" }, tuning: {} }],
});
has(refoss3, "CHANNELS = 1,2,3", "refoss: three-phase defaults CHANNELS when not a three-id list");
const refoss456 = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "refoss", phases: 3, fields: { IP: "192.168.1.150", CHANNELS: "4,5,6" }, tuning: {} }],
});
has(refoss456, "CHANNELS = 4,5,6", "refoss: three-phase preserves explicit CHANNELS");
const eyRefoss456 = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "refoss", phases: 3, fields: { IP: "192.168.1.150", CHANNELS: "4,5,6" }, tuning: {} }],
});
has(
  eyRefoss456,
  "url: http://192.168.1.150/rpc/Em.Status.Get?id=65535",
  "esp/refoss: polls configured IP",
);
has(eyRefoss456, 'root["status"][3]["power"]', "esp/refoss: L1 uses selected channel 4");
has(eyRefoss456, 'root["status"][4]["power"]', "esp/refoss: L2 uses selected channel 5");
has(eyRefoss456, 'root["status"][5]["power"]', "esp/refoss: L3 uses selected channel 6");
lacks(eyRefoss456, 'root["status"][0]["power"]', "esp/refoss: does not hardcode status[0] for 4,5,6");
const refossFloat = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellyproem50"] },
  meters: [{ type: "refoss", phases: 1, fields: { IP: "192.168.1.150", CHANNELS: "1.0" }, tuning: {} }],
});
has(refossFloat, "CHANNELS = 1", "refoss: rejects 1.0, defaults single-phase CHANNELS");
lacks(refossFloat, "CHANNELS = 1.0", "refoss: does not emit float CHANNELS");
const refossSci = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "refoss", phases: 3, fields: { IP: "192.168.1.150", CHANNELS: "1e2" }, tuning: {} }],
});
has(refossSci, "CHANNELS = 1,2,3", "refoss: rejects 1e2, defaults three-phase CHANNELS");
const refossMixed = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "refoss", phases: 3, fields: { IP: "192.168.1.150", CHANNELS: "4,5,1.0" }, tuning: {} }],
});
has(refossMixed, "CHANNELS = 1,2,3", "refoss: rejects mixed invalid three-phase CHANNELS");
const eyRefossFloat = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "refoss", phases: 3, fields: { IP: "192.168.1.150", CHANNELS: "1.0" }, tuning: {} }],
});
has(eyRefossFloat, 'root["status"][0]["power"]', "esp/refoss: 1.0 falls back to status[0]");
has(eyRefossFloat, 'root["status"][1]["power"]', "esp/refoss: 1.0 falls back to status[1]");
has(eyRefossFloat, 'root["status"][2]["power"]', "esp/refoss: 1.0 falls back to status[2]");
const refossFour = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "refoss", phases: 3, fields: { IP: "192.168.1.150", CHANNELS: "4,5,6,7" }, tuning: {} }],
});
has(refossFour, "CHANNELS = 1,2,3", "refoss: four-id three-phase falls back to 1,2,3");
lacks(refossFour, "CHANNELS = 4,5,6,7", "refoss: does not emit four-id CHANNELS for three-phase");
const eyRefossFour = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "refoss", phases: 3, fields: { IP: "192.168.1.150", CHANNELS: "4,5,6,7" }, tuning: {} }],
});
has(eyRefossFour, 'root["status"][0]["power"]', "esp/refoss: four-id falls back to status[0]");
has(eyRefossFour, 'root["status"][1]["power"]', "esp/refoss: four-id falls back to status[1]");
has(eyRefossFour, 'root["status"][2]["power"]', "esp/refoss: four-id falls back to status[2]");
lacks(eyRefossFour, 'root["status"][3]["power"]', "esp/refoss: four-id does not use truncated 4,5,6");

// ── config.ini: Tibber Pulse timeout (#551) ──────────────────────────────────
const tibber = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["ct002"] },
  meters: [{ type: "tibber_pulse", phases: 1, fields: { IP: "192.168.1.140", PASSWORD: "AD56-54BA", TIMEOUT: "10" }, tuning: {} }],
});
has(tibber, "[TIBBER_PULSE]", "tibber: section header");
has(tibber, "TIMEOUT = 10", "tibber: timeout override emitted");

// ── config.ini: ESPHome native API ───────────────────────────────────────────
const esphomeNative = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["ct002"] },
  meters: [
    {
      type: "esphomenative",
      phases: 1,
      fields: {
        ADDRESS: "device.local",
        PORT: "6053",
        API_KEY: "5BqtR16i91/+rwUl+QrJewKFOnyS/whHc3v9ySSKpb8=",
        OBJECT_ID: "grid_power",
        CLIENT_INFO: "AstraMeter",
      },
      tuning: {},
    },
  ],
});
has(esphomeNative, "[ESPHOMENATIVE]", "esphomenative: section header");
has(esphomeNative, "ADDRESS = device.local", "esphomenative: address");
has(esphomeNative, "PORT = 6053", "esphomenative: port");
has(esphomeNative, "API_KEY = 5BqtR16i91/+rwUl+QrJewKFOnyS/whHc3v9ySSKpb8=", "esphomenative: api key");
has(esphomeNative, "OBJECT_ID = grid_power", "esphomenative: object id");
has(esphomeNative, "CLIENT_INFO = AstraMeter", "esphomenative: client info");

// The native-API meter is Python-only — it must not emit an esphome sensor block
// (on the ESP the same source is read via the homeassistant platform instead).
const esphomeNativeEy = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "esphomenative", phases: 1, fields: { ADDRESS: "device.local", OBJECT_ID: "grid_power" }, tuning: {} }],
  ct: { fields: {} },
});
has(esphomeNativeEy, "entity_id: sensor.grid_power", "esphomenative: ESP reads the entity via homeassistant platform");

// ── config.ini: multi-meter NETMASK ──────────────────────────────────────────
const multi = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [
    { type: "shelly", suffix: "1", phases: 1, fields: { TYPE: "1PM", IP: "1.1.1.1" }, tuning: {}, netmask: "192.168.1.0/24" },
    { type: "shelly", suffix: "2", phases: 1, fields: { TYPE: "1PM", IP: "2.2.2.2" }, tuning: {}, netmask: "192.168.2.0/24" },
  ],
});
has(multi, "[SHELLY_1]", "multi: suffixed section 1");
has(multi, "[SHELLY_2]", "multi: suffixed section 2");
has(multi, "NETMASK = 192.168.1.0/24", "multi: netmask emitted");

// ── config.ini: Marstek + MQTT insights ──────────────────────────────────────
const extras = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["ct002"] },
  meters: [{ type: "shelly", phases: 1, fields: { TYPE: "1PM", IP: "1.1.1.1" }, tuning: {} }],
  marstek: { enabled: true, fields: { MAILBOX: "a@b.c", PASSWORD: "pw" } },
  mqttInsights: { enabled: true, fields: { BROKER: "192.168.1.9" } },
});
has(extras, "[MARSTEK]\nENABLE = True", "extras: marstek enabled");
has(extras, "MAILBOX = a@b.c", "extras: marstek mailbox");
has(extras, "[MQTT_INSIGHTS]", "extras: insights section");
has(extras, "BROKER = 192.168.1.9", "extras: insights broker");

// ── config.ini: enabled-but-empty extras are omitted (default-on safety) ──────
const extrasEmpty = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["ct002"] },
  meters: [{ type: "shelly", phases: 1, fields: { TYPE: "1PM", IP: "1.1.1.1" }, tuning: {} }],
  marstek: { enabled: true, fields: {} },
  mqttInsights: { enabled: true, fields: {} },
});
lacks(extrasEmpty, "[MARSTEK]", "extras-empty: omits marstek without credentials");
lacks(extrasEmpty, "[MQTT_INSIGHTS]", "extras-empty: omits insights without a broker");

// ── ESPHome: Home Assistant native, 3-phase ──────────────────────────────────
const eyHa = generateEsphome({
  target: "esphome",
  esphome: { name: "my-ct002", ctType: "HME-4", board: "esp32dev" },
  meters: [
    {
      type: "homeassistant",
      phases: 3,
      fields: { CURRENT_POWER_ENTITY: "sensor.l1, sensor.l2, sensor.l3" },
      tuning: { POWER_OFFSET: "-20" },
    },
  ],
  ct: {
    fields: {
      BALANCE_GAIN: "0.3",
      CLOUD_REPORTING: "True",
      CLOUD_REPORTING_HOST: "eu.hamedata.com",
      CLOUD_REPORTING_INTERVAL: "30",
    },
  },
});
has(eyHa, "name: my-ct002", "esp/ha: name");
// Cloud reporting emits a single http_request + a cloud_reporting: sub-block.
has(eyHa, "http_request:", "esp/ha: http_request for cloud reporting");
has(eyHa, "cloud_reporting:", "esp/ha: cloud_reporting sub-block");
has(eyHa, 'host: "eu.hamedata.com"', "esp/ha: cloud host quoted");
has(eyHa, "interval: 30s", "esp/ha: cloud interval");
has(eyHa, "external_components:", "esp/ha: external component");
has(eyHa, "platform: homeassistant", "esp/ha: native sensor");
has(eyHa, "entity_id: sensor.l1", "esp/ha: l1 entity");
has(eyHa, "entity_id: sensor.l3", "esp/ha: l3 entity");
has(eyHa, "power_sensor_l3: grid_l3", "esp/ha: 3-phase wiring");
has(eyHa, "- offset: -20", "esp/ha: offset filter on sensor");
has(eyHa, "ct_type: HME-4", "esp/ha: ct_type");
has(eyHa, "balance_gain: 0.3", "esp/ha: balancer sub-block");
// a single offset must apply to all three phase sensors, not just L1
ok((eyHa.match(/- offset: -20/g) || []).length === 3, "esp/ha: single offset applied to all 3 phases");

// per-phase offsets map to the matching phase sensor
const eyPerPhase = generateEsphome({
  target: "esphome",
  esphome: { ctType: "HME-4" },
  meters: [
    {
      type: "homeassistant",
      phases: 3,
      fields: { CURRENT_POWER_ENTITY: "sensor.l1, sensor.l2, sensor.l3" },
      tuning: { POWER_MULTIPLIER: "1,0,1" },
    },
  ],
  ct: { fields: {} },
});
has(eyPerPhase, "- multiply: 1", "esp/ha: per-phase multiplier L1");
has(eyPerPhase, "- multiply: 0", "esp/ha: per-phase multiplier L2 (null phase)");

// ── ESPHome: MQTT + insights + marstek ────────────────────────────────────────
const eyMqtt = generateEsphome({
  target: "esphome",
  esphome: { ctType: "HME-3" },
  meters: [{ type: "mqtt", phases: 1, fields: { BROKER: "192.168.1.10", TOPIC: "home/p" }, tuning: { DEADBAND: "20" } }],
  ct: { fields: { ACTIVE_CONTROL: "False" } },
  mqttInsights: { enabled: true, fields: { BROKER: "192.168.1.10", BASE_TOPIC: "astrameter", HA_DISCOVERY: "true" } },
  marstek: { enabled: true, fields: { MAILBOX: "a@b.c", TIMEZONE: "Europe/Berlin" } },
});
has(eyMqtt, "platform: mqtt_subscribe", "esp/mqtt: subscribe sensor");
has(eyMqtt, "topic: home/p", "esp/mqtt: topic");
has(eyMqtt, "active_control: false", "esp/mqtt: active control off");
has(eyMqtt, "deadband: 20", "esp/mqtt: deadband filter");
has(eyMqtt, "mqtt_insights:", "esp/mqtt: insights sub-block");
has(eyMqtt, "marstek_registration:", "esp/mqtt: marstek sub-block");
has(eyMqtt, "device_type: ct003", "esp/mqtt: ct003 from HME-3");
has(eyMqtt, "ct_type: HME-3", "esp/mqtt: ct_type HME-3");

// ── ESPHome: declarative per-meter behaviour (esphome.haEntity / headersField / warn) ──
const eyEsphomeSrc = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "esphome", phases: 1, fields: { IP: "10.0.0.7", ID: "my_grid" }, tuning: {} }],
  ct: { fields: {} },
});
has(eyEsphomeSrc, "entity_id: sensor.my_grid", "esp/esphome-source: haEntity uses the ID field");

const eyJsonHeaders = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "json_http", phases: 1, fields: { URL: "http://x/api", HEADERS: "Authorization: Bearer t; X-Env: prod" }, tuning: {} }],
  ct: { fields: {} },
});
has(eyJsonHeaders, "headers:", "esp/json_http: headersField emits a headers block");
has(eyJsonHeaders, "Authorization: Bearer t", "esp/json_http: first header");
has(eyJsonHeaders, "X-Env: prod", "esp/json_http: second header");
// The url/capture_response/on_response keys must be nested *under* the
// http_request.get action (indented one level deeper than the list item),
// otherwise ESPHome rejects them as sibling actions (issue #477).
has(eyJsonHeaders, "      - http_request.get:\n          url: http://x/api", "esp/json_http: url nested under http_request.get");
has(eyJsonHeaders, "          capture_response: true", "esp/json_http: capture_response nested under action");
has(eyJsonHeaders, "          on_response:", "esp/json_http: on_response nested under action");
has(eyJsonHeaders, "          headers:", "esp/json_http: headers nested under action");
// Large JSON responses (e.g. the Fronius Solar API) overflow ESPHome's small
// default receive buffer and truncate the body, so json::parse_json fails.
// Enlarge the buffer on both the client and the per-request action (issue #534).
has(eyJsonHeaders, "buffer_size_rx: 4096", "esp/http: enlarged http_request rx buffer");
has(eyJsonHeaders, "          max_response_buffer_size: 4096", "esp/http: enlarged per-request response buffer");

// An HTTP-polled meter plus cloud_reporting both need http_request:, but ESPHome
// allows only one. They must merge into a single block that keeps the RX buffer
// and uses the longer 20s timeout — not two blocks that drop one or the other.
const eyHttpPlusCloud = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "fronius", phases: 1, fields: { IP: "10.0.0.9" }, tuning: {} }],
  ct: { fields: { CLOUD_REPORTING: "True", CLOUD_REPORTING_HOST: "eu.hamedata.com" } },
});
ok((eyHttpPlusCloud.match(/^http_request:/gm) || []).length === 1, "esp/http+cloud: exactly one http_request block");
has(eyHttpPlusCloud, "buffer_size_rx: 4096", "esp/http+cloud: rx buffer survives merge");
has(eyHttpPlusCloud, "timeout: 20s", "esp/http+cloud: timeout bumped to 20s");
lacks(eyHttpPlusCloud, "timeout: 5s", "esp/http+cloud: meter's 5s timeout replaced, not duplicated");
has(eyJsonHeaders, "            Authorization: Bearer t", "esp/json_http: header entry nested under headers");

const eyModbusTcp = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "modbus", phases: 1, fields: { HOST: "10.0.0.9", TRANSPORT: "TCP" }, tuning: {} }],
  ct: { fields: {} },
});
has(eyModbusTcp, "RS485 serial only", "esp/modbus: TCP transport warns (declarative warn)");
const eyModbusUdp = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "modbus", phases: 1, fields: { HOST: "10.0.0.9", TRANSPORT: "UDP" }, tuning: {} }],
  ct: { fields: {} },
});
lacks(eyModbusUdp, "RS485 serial only", "esp/modbus: UDP transport does not warn");

// ── ESPHome: SML ──────────────────────────────────────────────────────────────
const eySml = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "sml", phases: 1, fields: { SERIAL: "/dev/ttyUSB0" }, tuning: {} }],
  ct: { fields: {} },
});
has(eySml, "platform: sml", "esp/sml: sml sensor");
has(eySml, 'obis_code: "1-0:16.7.0"', "esp/sml: default obis");

// ── ESPHome: unsupported meter warns ──────────────────────────────────────────
const eyEnvoy = generateEsphome({
  target: "esphome",
  esphome: {},
  meters: [{ type: "envoy", phases: 1, fields: { HOST: "1.2.3.4" }, tuning: {} }],
  ct: { fields: {} },
});
has(eyEnvoy, "no ESPHome component yet", "esp/envoy: warning emitted");
has(eyEnvoy, "TODO: publish your grid power", "esp/envoy: placeholder sensor");

// ── Home Assistant add-on options: full filter set ───────────────────────────
const haOpts = generateHomeAssistant({
  target: "homeassistant",
  general: { deviceTypes: ["ct002"], throttleInterval: "2", waitForNextMessage: "false" },
  meters: [
    {
      type: "homeassistant",
      phases: 1,
      fields: { CURRENT_POWER_ENTITY: "sensor.grid_power" },
      tuning: {
        POWER_OFFSET: "-20",
        SMOOTH_TARGET_ALPHA: "0.3",
        DEADBAND: "5",
        HAMPEL_WINDOW: "5",
        HAMPEL_N_SIGMA: "3.0",
        HAMPEL_MIN_THRESHOLD: "50",
        PID_KP: "0.5",
        PID_OUTPUT_MAX: "800",
        PID_MODE: "bias",
      },
    },
  ],
  ct: {
    fields: {
      CT_MAC: "abc123",
      MIN_DC_OUTPUT: "30",
      ACTIVE_CONTROL: "False",
      CLOUD_REPORTING: "True",
      CLOUD_REPORTING_INTERVAL: "30",
      GRID_PREDICT_TRUST: "0.7",
      FAIR_DISTRIBUTION: "False",
      BALANCE_GAIN: "0.3",
      PACE_BASE_STEP: "150",
      PACE_MAX_STEP: "600",
      OSC_DAMP_MAX: "0.5",
      CONCENTRATE_DEADBAND: "0",
      IMPORT_TRIM_W: "20",
    },
  },
});
has(haOpts, "power_input_alias: \"sensor.grid_power\"", "ha-opts: power input alias");
has(haOpts, "cloud_reporting: true", "ha-opts: cloud reporting on");
has(haOpts, "cloud_reporting_interval: 30", "ha-opts: cloud interval");
has(haOpts, "device_types: \"ct002\"", "ha-opts: device types");
has(haOpts, "throttle_interval: 2", "ha-opts: throttle interval");
has(haOpts, "wait_for_next_message: false", "ha-opts: wait for next message");
has(haOpts, "ct_mac: \"abc123\"", "ha-opts: ct mac");
has(haOpts, "active_control: false", "ha-opts: active control off");
has(haOpts, "min_dc_output: 30", "ha-opts: min dc output");
has(haOpts, "grid_predict_trust: 0.7", "ha-opts: grid predict trust");
has(haOpts, "fair_distribution: false", "ha-opts: fair distribution off");
has(haOpts, "balance_gain: 0.3", "ha-opts: balance gain");
has(haOpts, "pace_base_step: 150", "ha-opts: pace base step");
has(haOpts, "pace_max_step: 600", "ha-opts: pace max step");
has(haOpts, "osc_damp_max: 0.5", "ha-opts: oscillation damping");
has(haOpts, "concentrate_deadband: 0", "ha-opts: concentrate deadband (explicit 0 kept)");
has(haOpts, "import_trim_w: 20", "ha-opts: steady-import trim");
has(haOpts, 'power_offset: "-20"', "ha-opts: power offset (quoted str)");
has(haOpts, "smooth_target_alpha: 0.3", "ha-opts: smoothing alpha");
has(haOpts, "deadband: 5", "ha-opts: deadband");
has(haOpts, "hampel_window: 5", "ha-opts: hampel window");
has(haOpts, "hampel_n_sigma: 3.0", "ha-opts: hampel sigma");
has(haOpts, "hampel_min_threshold: 50", "ha-opts: hampel min threshold");
has(haOpts, "pid_kp: 0.5", "ha-opts: pid kp");
has(haOpts, "pid_output_max: 800", "ha-opts: pid output max");
has(haOpts, "pid_mode: \"bias\"", "ha-opts: pid mode");
has(haOpts, "Custom Config", "ha-opts: mentions custom config fallback");

// ── Home Assistant add-on options: empty filters omitted ─────────────────────
const haMin = generateHomeAssistant({
  target: "homeassistant",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [{ type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} }],
  ct: { fields: {} },
});
lacks(haMin, "hampel_window", "ha-opts: omits unset hampel");
lacks(haMin, "pid_kp", "ha-opts: omits unset pid");
lacks(haMin, "deadband", "ha-opts: omits unset deadband");
lacks(haMin, "min_dc_output", "ha-opts: omits unset min dc output");
lacks(haMin, "active_control", "ha-opts: omits default active control");
lacks(haMin, "pace_base_step", "ha-opts: omits unset pace base step");
lacks(haMin, "grid_predict_trust", "ha-opts: omits unset grid predict trust");
lacks(haMin, "fair_distribution", "ha-opts: omits unset fair distribution");
lacks(haMin, "import_trim_w", "ha-opts: omits unset import trim");

// ── Home Assistant add-on options: calculate from in/out ─────────────────────
const haCalc = generateHomeAssistant({
  target: "homeassistant",
  general: { deviceTypes: ["shellypro3em"] },
  meters: [
    {
      type: "homeassistant",
      phases: 1,
      fields: { POWER_CALCULATE: "True", POWER_INPUT_ALIAS: "sensor.in", POWER_OUTPUT_ALIAS: "sensor.out" },
      tuning: {},
    },
  ],
  ct: { fields: {} },
});
has(haCalc, "power_input_alias: \"sensor.in\"", "ha-opts: calc input alias");
has(haCalc, "power_output_alias: \"sensor.out\"", "ha-opts: calc output alias");

// ── Home Assistant add-on options: all-digit MAC stays a quoted string ───────
const haMac = generateHomeAssistant({
  target: "homeassistant",
  general: { deviceTypes: ["ct002"] },
  meters: [{ type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} }],
  ct: { fields: { CT_MAC: "001122334455" } },
});
has(haMac, 'ct_mac: "001122334455"', "ha-opts: all-digit ct_mac is quoted (keeps leading zeros)");

// ── Home Assistant add-on options: mqtt_uri TLS + credential encoding ────────
const haUri = generateHomeAssistant({
  target: "homeassistant",
  general: { deviceTypes: ["ct002"] },
  meters: [{ type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} }],
  ct: { fields: {} },
  mqttInsights: { enabled: true, fields: { BROKER: "broker.local", PORT: "1883", USERNAME: "a@b", PASSWORD: "p:w/d", TLS: "false" } },
});
has(haUri, "mqtt://a%40b:p%3Aw%2Fd@broker.local:1883", "ha-opts: mqtt_uri encodes creds and TLS string 'false' stays mqtt");
lacks(haUri, "mqtts://", "ha-opts: string 'false' TLS is not treated as enabled");


// ── dashboard options (config.ini + Home Assistant add-on) ──
const dashOn = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["ct002"], dashboardEnabled: true, dashboardAllowWrite: true, webServerPort: "8123" },
  meters: [{ type: "shelly", phases: 1, fields: { TYPE: "3EMPro", IP: "192.168.1.50" }, tuning: {} }],
});
has(dashOn, "DASHBOARD_ENABLED = True", "dashboard: enabled flag");
has(dashOn, "DASHBOARD_ALLOW_WRITE = True", "dashboard: write flag");
has(dashOn, "WEB_SERVER_PORT = 8123", "dashboard: port emitted without the INI editor");

const dashReadOnly = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["ct002"], dashboardEnabled: true, dashboardAllowWrite: false },
  meters: [{ type: "shelly", phases: 1, fields: { TYPE: "3EMPro", IP: "192.168.1.50" }, tuning: {} }],
});
has(dashReadOnly, "DASHBOARD_ENABLED = True", "dashboard: on");
has(dashReadOnly, "DASHBOARD_ALLOW_WRITE = False", "dashboard: asking for read-only is written out");

// Unset means the defaults, and both of them are on.
const dashDefault = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["ct002"], webServerPort: "8123" },
  meters: [{ type: "shelly", phases: 1, fields: { TYPE: "3EMPro", IP: "192.168.1.50" }, tuning: {} }],
});
has(dashDefault, "DASHBOARD_ENABLED = True", "dashboard: on by default");
has(dashDefault, "DASHBOARD_ALLOW_WRITE = True", "dashboard: writable by default");
has(dashDefault, "WEB_SERVER_PORT = 8123", "dashboard: default-on dashboard still carries the port");

const dashOff = generateConfigIni({
  target: "python",
  general: { deviceTypes: ["ct002"], dashboardEnabled: false, dashboardAllowWrite: true, webServerPort: "8123" },
  meters: [{ type: "shelly", phases: 1, fields: { TYPE: "3EMPro", IP: "192.168.1.50" }, tuning: {} }],
});
has(dashOff, "DASHBOARD_ENABLED = False", "dashboard: turning it off is written out");
lacks(dashOff, "DASHBOARD_ALLOW_WRITE", "dashboard: no write flag for a dashboard that never runs");
has(dashOff, "WEB_SERVER_PORT = 8123", "dashboard: a chosen port survives turning the dashboard off");

// On an ESP32 the firmware serves the dashboard unless told not to, so the
// on-and-read-only case is the default and writes nothing at all.
const eyDash = generateEsphome({
  target: "esphome",
  esphome: { name: "my-ct002", ctType: "HME-4", board: "esp32dev" },
  general: { deviceTypes: ["ct002"], esphomeDashboard: true },
  meters: [
    { type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} },
  ],
});
lacks(eyDash, "dashboard", "esp/dashboard: on is the default, so nothing is emitted");

const eyDashWritable = generateEsphome({
  target: "esphome",
  esphome: { name: "my-ct002", ctType: "HME-4", board: "esp32dev" },
  general: { deviceTypes: ["ct002"], esphomeDashboard: true, esphomeControls: true },
  meters: [
    { type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} },
  ],
});
has(eyDashWritable, "  dashboard:", "esp/dashboard: sub-block appears once it carries an option");
has(eyDashWritable, "    controls: true", "esp/dashboard: controls are opt-in and emitted");

// The service ships writes on; a board has no login to sit behind, so its
// controls must not follow that flag.
const eyDashServiceWrites = generateEsphome({
  target: "esphome",
  esphome: { name: "my-ct002", ctType: "HME-4", board: "esp32dev" },
  general: { deviceTypes: ["ct002"], esphomeDashboard: true, dashboardAllowWrite: true },
  meters: [
    { type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} },
  ],
});
lacks(eyDashServiceWrites, "controls: true", "esp/dashboard: DASHBOARD_ALLOW_WRITE does not turn on board controls");

const eyNoDash = generateEsphome({
  target: "esphome",
  esphome: { name: "my-ct002", ctType: "HME-4", board: "esp32dev" },
  general: { deviceTypes: ["ct002"], esphomeDashboard: false },
  meters: [
    { type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} },
  ],
});
has(eyNoDash, "  dashboard: false", "esp/dashboard: turning it off is what has to be written down");

// A state with no general block at all is "nothing said", not "off" — the
// firmware default stands.
const eyDashUnsaid = generateEsphome({
  target: "esphome",
  esphome: { name: "my-ct002", ctType: "HME-4", board: "esp32dev" },
  meters: [
    { type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} },
  ],
});
lacks(eyDashUnsaid, "dashboard", "esp/dashboard: nothing said leaves the default alone");

// The add-on ships the dashboard on and writable, so only a deviation from
// those defaults is worth emitting into the options.
const haDashDefault = generateHomeAssistant({
  target: "homeassistant",
  general: { deviceTypes: ["ct002"], dashboardAllowWrite: true },
  meters: [{ type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} }],
  ct: { fields: {} },
});
lacks(haDashDefault, "dashboard_allow_write", "ha-opts: nothing emitted when writes match the add-on default");
lacks(haDashDefault, "dashboard_direct_access", "ha-opts: nothing emitted when the port stays behind ingress");

// Reaching the page on the add-on's port instead of through ingress.
const haDirect = generateHomeAssistant({
  target: "homeassistant",
  general: { deviceTypes: ["ct002"], dashboardAllowWrite: true, dashboardDirectAccess: true },
  meters: [{ type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} }],
  ct: { fields: {} },
});
has(haDirect, "dashboard_direct_access: true", "ha-opts: direct access is emitted when asked for");

// The add-on's sidebar panel *is* the dashboard, so it cannot be turned off
// there — no `dashboard` option exists to emit, whatever the form says.
const haDashOff = generateHomeAssistant({
  target: "homeassistant",
  general: { deviceTypes: ["ct002"], dashboardEnabled: false, dashboardAllowWrite: true },
  meters: [{ type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} }],
  ct: { fields: {} },
});
lacks(haDashOff, "dashboard:", "ha-opts: the add-on has no option to disable the dashboard");

const haDashReadOnly = generateHomeAssistant({
  target: "homeassistant",
  general: { deviceTypes: ["ct002"], dashboardEnabled: true, dashboardAllowWrite: false },
  meters: [{ type: "homeassistant", phases: 1, fields: { CURRENT_POWER_ENTITY: "sensor.p" }, tuning: {} }],
  ct: { fields: {} },
});
has(haDashReadOnly, "dashboard_allow_write: false", "ha-opts: read-only dashboard is emitted");


console.log("\n" + (failures ? `${failures} FAILED` : "ALL PASSED"));
process.exit(failures ? 1 : 0);
