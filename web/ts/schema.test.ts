// Structural validation for schema.js. This is the safety net that makes the
// schema safe to edit: it fails loudly on typos and convention breaks (a
// misspelled field `type`, a `select` with no `options`, an unknown
// `esphome.kind`, a duplicate section, a stray property name) instead of
// silently producing wrong config. Run with:
//   node web/js/schema.test.mjs
import {
  POWERMETERS,
  PHASE_CAPABLE,
  PER_METER_TUNING,
  CT_BASIC,
  CT_ACTIVE,
  CT_BALANCER,
  CT_EFFICIENCY,
  CT_SATURATION,
  MARSTEK_FIELDS,
  MQTT_INSIGHTS_FIELDS,
  ESP_BOARDS,
} from "./schema.js";

let failures = 0;
function check(cond, msg) {
  if (!cond) {
    failures++;
    console.error("✗ " + msg);
  }
}

// Allowed shapes — anything outside these lists is almost certainly a typo.
const FIELD_TYPES = new Set(["text", "number", "password", "select", "checkbox"]);
const FIELD_PROPS = new Set(["key", "label", "help", "type", "default", "placeholder", "options", "required", "phase", "advanced", "ey"]);
const PM_PROPS = new Set(["id", "label", "section", "blurb", "docPython", "fields", "esphome", "phaseListKeys"]);
const ESP_KINDS = new Set(["homeassistant", "mqtt", "sml", "modbus", "http", "unsupported"]);
const ESP_TIERS = new Set(["native", "generic", "alternate", "unsupported"]);
const ESP_PROPS = new Set(["kind", "tier", "note", "url1", "url3", "lambda1", "lambda3", "jsonRoot", "haEntity", "headersField", "warn"]);

// Validate one field descriptor (used by meters and the shared option groups).
function validateField(field, where) {
  check(typeof field.key === "string" && field.key, `${where}: field has a non-empty key`);
  const id = `${where}.${field.key}`;
  check(typeof field.label === "string" && field.label, `${id}: has a label`);
  check(FIELD_TYPES.has(field.type), `${id}: type "${field.type}" must be one of ${[...FIELD_TYPES].join(", ")}`);
  for (const prop of Object.keys(field)) {
    check(FIELD_PROPS.has(prop), `${id}: unknown property "${prop}" (typo?)`);
  }
  if (field.type === "select") {
    check(Array.isArray(field.options) && field.options.length > 0, `${id}: select must have options`);
    for (const opt of field.options || []) {
      check(typeof opt.value === "string" && typeof opt.label === "string", `${id}: each option needs string value+label`);
    }
  } else {
    check(field.options === undefined, `${id}: only selects may have options`);
  }
  if (field.phase) {
    check(field.type === "text" || field.type === "number", `${id}: phase fields must be text/number`);
  }
}

// ── powermeters ──
const ids = new Set();
const sections = new Set();
for (const pm of POWERMETERS) {
  const where = `powermeter[${pm.id}]`;
  check(typeof pm.id === "string" && pm.id, `${where}: has an id`);
  check(!ids.has(pm.id), `${where}: id is unique`);
  ids.add(pm.id);
  check(typeof pm.label === "string" && pm.label, `${where}: has a label`);
  check(typeof pm.section === "string" && /^[A-Z0-9_]+$/.test(pm.section || ""), `${where}: section is UPPER_SNAKE`);
  check(!sections.has(pm.section), `${where}: section "${pm.section}" is unique`);
  sections.add(pm.section);
  check(Array.isArray(pm.fields) && pm.fields.length > 0, `${where}: has at least one field`);

  for (const prop of Object.keys(pm)) {
    check(PM_PROPS.has(prop), `${where}: unknown property "${prop}" (typo?)`);
  }

  const fieldKeys = new Set();
  for (const field of pm.fields || []) {
    validateField(field, where);
    check(!fieldKeys.has(field.key), `${where}: duplicate field key "${field.key}"`);
    fieldKeys.add(field.key);
  }

  // esphome block
  const esp = pm.esphome;
  check(esp && typeof esp === "object", `${where}: has an esphome block`);
  if (esp) {
    check(ESP_KINDS.has(esp.kind), `${where}: esphome.kind "${esp.kind}" invalid`);
    check(ESP_TIERS.has(esp.tier), `${where}: esphome.tier "${esp.tier}" invalid`);
    check(typeof esp.note === "string" && esp.note, `${where}: esphome.note present`);
    for (const prop of Object.keys(esp)) {
      check(ESP_PROPS.has(prop), `${where}: unknown esphome property "${prop}" (typo?)`);
    }
    if (esp.kind === "http") {
      check(esp.url1 !== undefined, `${where}: http esphome needs url1`);
      check(esp.lambda1 !== undefined, `${where}: http esphome needs lambda1`);
    }
    // functions where the generator calls them
    for (const fn of ["url1", "url3", "haEntity", "warn"]) {
      if (esp[fn] !== undefined && fn !== "warn") {
        check(typeof esp[fn] === "function", `${where}: esphome.${fn} must be a function`);
      }
    }
    if (esp.warn !== undefined) {
      check(typeof esp.warn === "function" || typeof esp.warn === "string", `${where}: esphome.warn must be a function or string`);
    }
    if (esp.headersField !== undefined) {
      check(fieldKeys.has(esp.headersField), `${where}: esphome.headersField "${esp.headersField}" must name a real field`);
    }
  }

  if (pm.phaseListKeys) {
    check(typeof pm.phaseListKeys.topic === "string" && typeof pm.phaseListKeys.jsonPath === "string", `${where}: phaseListKeys needs topic+jsonPath`);
  }
}

// ── PHASE_CAPABLE references real meters ──
for (const id of PHASE_CAPABLE) {
  check(ids.has(id), `PHASE_CAPABLE: "${id}" is not a known powermeter id`);
}

// ── shared option groups validate as fields ──
const groups = {
  PER_METER_TUNING,
  CT_BASIC,
  CT_ACTIVE,
  CT_BALANCER,
  CT_EFFICIENCY,
  CT_SATURATION,
  MARSTEK_FIELDS,
  MQTT_INSIGHTS_FIELDS,
};
for (const [name, group] of Object.entries(groups)) {
  check(Array.isArray(group) && group.length > 0, `${name}: non-empty array`);
  const keys = new Set();
  for (const field of group) {
    validateField(field, name);
    check(!keys.has(field.key), `${name}: duplicate key "${field.key}"`);
    keys.add(field.key);
  }
}

// ── ESP_BOARDS ──
check(Array.isArray(ESP_BOARDS) && ESP_BOARDS.length > 0, "ESP_BOARDS: non-empty");
for (const b of ESP_BOARDS) {
  check(typeof b.value === "string" && typeof b.label === "string", `ESP_BOARDS: each entry needs value+label (${JSON.stringify(b)})`);
}

if (failures) {
  console.error(`\n${failures} schema problem(s) found`);
  process.exit(1);
}
console.log(`✓ schema valid — ${POWERMETERS.length} powermeters, ${Object.keys(groups).length} option groups`);
