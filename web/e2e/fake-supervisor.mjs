// A stand-in Home Assistant Supervisor for the end-to-end tests.
//
// It serves the repository's OWN `ha_addon/config.yaml` options and schema,
// so the guided form is rendered from the add-on's real option definitions
// rather than a fixture that can drift away from them. It also implements
// the parts of the contract the dashboard depends on: options are a FULL
// REPLACE of the persisted overlay, and an out-of-range value is rejected
// with Supervisor's own message shape.
//
// This is the one thing that cannot be run for real in CI. Everything below
// the Supervisor boundary is the actual software.
import http from "node:http";
import { readFileSync, writeFileSync } from "node:fs";

import { dirname, resolve, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const CONFIG_YAML = resolve(HERE, "../../ha_addon/config.yaml");
const PORT = Number(process.env.FAKE_SUPERVISOR_PORT || 8199);
const STATE =
  process.env.FAKE_SUPERVISOR_STATE || join(HERE, "addon-options.json");

// Minimal YAML read of just the two blocks we need.
function parseBlocks(text) {
  const out = { options: {}, schema: {} };
  let current = null;
  for (const line of text.split("\n")) {
    if (/^options:\s*$/.test(line)) { current = "options"; continue; }
    if (/^schema:\s*$/.test(line)) { current = "schema"; continue; }
    if (/^\S/.test(line)) { current = null; continue; }
    if (!current) continue;
    const m = /^  ([a-z0-9_]+):\s*(.*)$/.exec(line);
    if (!m) continue;
    let [, key, raw] = m;
    raw = raw.trim();
    if (current === "schema") { out.schema[key] = raw; continue; }
    if (raw === "true" || raw === "false") out.options[key] = raw === "true";
    else if (/^-?\d+$/.test(raw)) out.options[key] = parseInt(raw, 10);
    else if (/^-?\d*\.\d+$/.test(raw)) out.options[key] = parseFloat(raw);
    else out.options[key] = raw.replace(/^"|"$/g, "");
  }
  return out;
}

const base = parseBlocks(readFileSync(CONFIG_YAML, "utf8"));
let options = { ...base.options };
try { options = { ...options, ...JSON.parse(readFileSync(STATE, "utf8")) }; } catch {}
// A stored secret, to prove the sentinel round-trip.
if (!options.marstek_password) options.marstek_password = "super-secret-pw";

const server = http.createServer((req, res) => {
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", () => {
    const send = (code, obj) => {
      res.writeHead(code, { "content-type": "application/json" });
      res.end(JSON.stringify(obj));
    };
    if (process.env.FAKE_SUPERVISOR_VERBOSE) {
      console.log(`  ${req.method} ${req.url}`);
    }

    if (req.url === "/addons/self/info") {
      return send(200, {
        result: "ok",
        data: {
          slug: "a0ef98c5_b2500_meter",
          version: "next",
          state: "started",
          ingress_panel: true,
          ingress_url: "/api/hassio_ingress/7f3c9aa1b2/",
          options,
          schema: base.schema,
        },
      });
    }
    if (req.url === "/addons/self/options" && req.method === "POST") {
      const payload = JSON.parse(body || "{}");
      if ("ingress_panel" in payload) return send(200, { result: "ok" });
      const next = payload.options || {};
      // Supervisor validates; reject a bad value the way it really would.
      for (const [k, v] of Object.entries(next)) {
        const spec = base.schema[k];
        if (spec && spec.startsWith("float(0,1)") && (v < 0 || v > 1)) {
          return send(400, {
            result: "error",
            message: `expected float in range [0, 1] for dictionary value @ data['${k}']. Got ${JSON.stringify(v)}`,
          });
        }
      }
      options = next;
      writeFileSync(STATE, JSON.stringify(options, null, 2));
      return send(200, { result: "ok" });
    }
    if (req.url === "/core/api/states") {
      // A realistic mix: power sensors with and without a device class, plus
      // entities that must NOT appear in the picker.
      return send(200, [
        { entity_id: "sensor.current_power_in", state: "412.8", attributes: { friendly_name: "Grid power", device_class: "power", unit_of_measurement: "W" } },
        { entity_id: "sensor.shelly_em_total_power", state: "-38.2", attributes: { friendly_name: "Shelly EM total power", device_class: "power", unit_of_measurement: "W" } },
        { entity_id: "sensor.p1_meter_active_power", state: "1.24", attributes: { friendly_name: "P1 meter active power", unit_of_measurement: "kW" } },
        { entity_id: "sensor.solar_inverter_power", state: "3204", attributes: { friendly_name: "Solar inverter", device_class: "power", unit_of_measurement: "W" } },
        { entity_id: "sensor.house_energy_today", state: "12.4", attributes: { friendly_name: "Energy today", device_class: "energy", unit_of_measurement: "kWh" } },
        { entity_id: "sensor.outside_temperature", state: "18.1", attributes: { friendly_name: "Outside temperature", device_class: "temperature", unit_of_measurement: "°C" } },
        { entity_id: "light.kitchen", state: "on", attributes: { friendly_name: "Kitchen light" } },
      ]);
    }
    if (req.url === "/addons/self/restart" && req.method === "POST") {
      return send(200, { result: "ok" });
    }
    send(404, { result: "error", message: "not found" });
  });
});

server.listen(PORT, "127.0.0.1", () =>
  console.log(
    `stand-in supervisor on http://127.0.0.1:${PORT} ` +
      `(${Object.keys(base.schema).length} schema keys from the real config.yaml)`,
  ),
);
