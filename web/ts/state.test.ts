// Tests for the state model + sanitisation (state.js). These run in plain Node
// — no DOM — because the logic is pure. They lock in the hardening that keeps
// untrusted restored input (share link / project file) safe. Run with:
//   node web/js/state.test.mjs
import { defaultState, newMeter, safeParse, cleanMeter, migrate } from "./state.js";

let failures = 0;
function ok(cond, msg) {
  if (!cond) {
    failures++;
    console.error("✗ " + msg);
  }
}

// ── defaults ──
const d = defaultState();
ok(d.target === "python", "default target is python");
ok(Array.isArray(d.meters) && d.meters.length === 1, "default has one meter");
ok(newMeter().fields && typeof newMeter().fields === "object", "newMeter has a fields object");

// ── safeParse drops prototype-polluting keys ──
const parsed = safeParse('{"a":1,"__proto__":{"polluted":true},"b":{"constructor":2}}');
ok(parsed.a === 1, "safeParse keeps normal keys");
ok(({}).polluted === undefined, "safeParse: Object.prototype not polluted");
ok(!Object.prototype.hasOwnProperty.call(parsed, "polluted"), "safeParse: no inherited pollution on result");
ok(parsed.b && parsed.b.constructor === Object, "safeParse strips nested constructor key");

// ── cleanMeter constrains an untrusted meter ──
const hostile = cleanMeter({
  type: "<img src=x onerror=alert(1)>",
  suffix: { not: "a string" },
  phases: 99,
  netmask: 12345,
  fields: { IP: "<script>alert(1)</script>" },
  tuning: "nope",
});
ok(hostile.type === "homeassistant", "cleanMeter: unknown/hostile type falls back to a known id");
ok(hostile.suffix === "", "cleanMeter: non-string suffix coerced to ''");
ok(hostile.phases === 1, "cleanMeter: out-of-range phases coerced to 1");
ok(hostile.netmask === "", "cleanMeter: non-string netmask coerced to ''");
ok(hostile.fields.IP === "<script>alert(1)</script>", "cleanMeter: field VALUES preserved verbatim (rendered safely elsewhere)");
ok(hostile.tuning && typeof hostile.tuning === "object" && Object.keys(hostile.tuning).length === 0, "cleanMeter: non-object tuning coerced to {}");

const good = cleanMeter({ type: "shelly", suffix: "garage", phases: 3, netmask: "192.168.1.0/24", fields: { IP: "1.2.3.4" }, tuning: {} });
ok(good.type === "shelly" && good.phases === 3 && good.suffix === "garage", "cleanMeter: valid meter preserved");

// ── migrate ──
ok(migrate(null).target === "python", "migrate(null) returns a usable default-ish state");
ok(migrate({ target: "weird" }).target === "python", "migrate: invalid target constrained to python");
ok(migrate({ target: "esphome" }).target === "esphome", "migrate: esphome target preserved");

const fromHostileLink = migrate(
  safeParse(
    JSON.stringify({
      target: "esphome",
      general: { deviceTypes: ["ct002"] },
      meters: [{ type: "javascript:alert(1)", phases: 1, fields: { TOKEN: "<svg onload=alert(1)>" }, tuning: {} }],
    }),
  ),
);
ok(fromHostileLink.target === "esphome", "migrate(hostile): keeps a valid target");
ok(fromHostileLink.general.deviceTypes[0] === "ct002", "migrate(hostile): merges general");
ok(fromHostileLink.meters[0].type === "homeassistant", "migrate(hostile): constrains bad meter type");
ok(fromHostileLink.meters[0].fields.TOKEN === "<svg onload=alert(1)>", "migrate(hostile): field value preserved (not executed; rendered via value/textContent)");
ok(({}).polluted === undefined, "migrate(hostile): no prototype pollution");

// ── migrate coerces ct/marstek/mqttInsights sub-sections ──
const hostileSubs = migrate({
  ct: { fields: ["array", "not", "object"] },
  marstek: { enabled: "yes-string", fields: 42 },
  mqttInsights: { enabled: 1, fields: null },
});
ok(!Array.isArray(hostileSubs.ct.fields) && typeof hostileSubs.ct.fields === "object", "migrate: ct.fields array coerced to {}");
ok(hostileSubs.marstek.enabled === true, "migrate: non-boolean marstek.enabled coerced to true");
ok(!Array.isArray(hostileSubs.marstek.fields) && typeof hostileSubs.marstek.fields === "object" && Object.keys(hostileSubs.marstek.fields).length === 0, "migrate: numeric marstek.fields coerced to {}");
ok(hostileSubs.mqttInsights.enabled === true, "migrate: truthy mqttInsights.enabled coerced to bool true");
ok(typeof hostileSubs.mqttInsights.fields === "object" && hostileSubs.mqttInsights.fields !== null, "migrate: null mqttInsights.fields coerced to {}");
ok(migrate({}).marstek.enabled === false && migrate({}).mqttInsights.enabled === false, "migrate: missing enabled defaults to false");

// cleanMeter rejects array fields/tuning (typeof [] === 'object')
const arrFields = cleanMeter({ type: "shelly", fields: ["x"], tuning: ["y"] });
ok(!Array.isArray(arrFields.fields) && Object.keys(arrFields.fields).length === 0, "cleanMeter: array fields coerced to {}");
ok(!Array.isArray(arrFields.tuning) && Object.keys(arrFields.tuning).length === 0, "cleanMeter: array tuning coerced to {}");

// migrate coerces untrusted general sub-fields (no crash in generalSection)
const hg = migrate({ general: { deviceTypes: "shellypro3em", deviceIds: 123, skipPowermeterTest: "yes", webServerPort: 555 } });
ok(Array.isArray(hg.general.deviceTypes), "migrate: non-array deviceTypes coerced to an array");
ok(typeof hg.general.deviceIds === "string", "migrate: numeric deviceIds coerced to string");
ok(typeof hg.general.skipPowermeterTest === "boolean", "migrate: non-boolean skipPowermeterTest coerced to boolean");
ok(typeof hg.general.webServerPort === "string", "migrate: numeric webServerPort coerced to string");
const okg = migrate({ general: { deviceTypes: ["ct002", "ct003"], deviceIds: "x-1" } });
ok(okg.general.deviceTypes.length === 2 && okg.general.deviceIds === "x-1", "migrate: valid general preserved");

// migrate fills in newly-added keys for an old saved state
const old = migrate({ target: "python", meters: [{ type: "shelly", fields: { IP: "1.1.1.1" } }] });
ok(old.esphome && old.esphome.board, "migrate: backfills missing esphome defaults");
ok(old.ct && old.ct.fields, "migrate: backfills missing ct.fields");
ok(old.meters[0].phases === 1 && old.meters[0].tuning, "migrate: backfills missing meter keys");

if (failures) {
  console.error(`\n${failures} FAILED`);
  process.exit(1);
}
console.log("✓ state model + sanitisation OK");
