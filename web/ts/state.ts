// state.js — the app's state model and the (pure, DOM-free) persistence helpers:
// defaults, a defensive JSON parse, and `migrate()`, which both fills in keys
// added since a saved state was written AND constrains untrusted restored input
// (share link / project file) to known-good shapes. Kept separate from app.js
// so it can be unit-tested in Node without a DOM (see state.test.mjs).
import { getPowermeter, type Fields } from "./schema.js";

export const STORAGE_KEY = "astrameter-generator-state-v1";

export interface Meter {
  type: string;
  suffix: string;
  phases: number;
  fields: Fields;
  tuning: Fields;
  netmask: string;
}

export interface State {
  target: "python" | "esphome";
  general: {
    deviceTypes: string[];
    deviceIds: string;
    skipPowermeterTest: boolean;
    webConfigEnabled: boolean;
    webServerPort: string;
    throttleInterval: string;
    waitForNextMessage: string;
    dedupeTimeWindow: string;
  };
  meters: Meter[];
  ct: { fields: Fields };
  marstek: { enabled: boolean; fields: Fields };
  mqttInsights: { enabled: boolean; fields: Fields };
  esphome: {
    name: string;
    friendlyName: string;
    board: string;
    framework: string;
    ctType: string;
  };
}

export function newMeter(type: string = "homeassistant"): Meter {
  return { type, suffix: "", phases: 1, fields: {}, tuning: {}, netmask: "" };
}

export function defaultState(): State {
  return {
    target: "python",
    general: {
      deviceTypes: ["shellypro3em"],
      deviceIds: "",
      skipPowermeterTest: false,
      webConfigEnabled: false,
      webServerPort: "",
      throttleInterval: "",
      waitForNextMessage: "",
      dedupeTimeWindow: "",
    },
    meters: [newMeter("shelly")],
    ct: { fields: {} },
    marstek: { enabled: false, fields: {} },
    mqttInsights: { enabled: false, fields: {} },
    esphome: {
      name: "astrameter-ct002",
      friendlyName: "AstraMeter CT002",
      board: "esp32-s3-devkitc-1",
      framework: "esp-idf",
      ctType: "HME-4",
    },
  };
}

// Defensive JSON parse for untrusted input (share link + project file): drop
// __proto__/constructor/prototype keys so a crafted payload can't attempt
// prototype pollution. Not currently exploitable (migrate uses spreads, not a
// recursive merge), but cheap insurance as the merge logic evolves.
const UNSAFE_KEYS = new Set(["__proto__", "constructor", "prototype"]);
export function safeParse(text: string): unknown {
  return JSON.parse(text, (key, value) => (UNSAFE_KEYS.has(key) ? undefined : value));
}

// A plain-object guard. Note `typeof [] === "object"`, so arrays must be
// rejected explicitly — otherwise a restored array would pass as a fields map.
function asStr(v: unknown, fallback: string): string {
  return typeof v === "string" ? v : fallback;
}
function asBool(v: unknown, fallback: boolean): boolean {
  return typeof v === "boolean" ? v : fallback;
}
function asObject(v: unknown): Fields {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Fields) : {};
}

// Coerce one restored meter into a known-good shape. Constrains `type` to a real
// powermeter id and forces the value-bearing fields to strings/objects, so
// restored state can never carry an unexpected type into the renderer.
export function cleanMeter(m: any): Meter {
  const base = newMeter();
  const src = m && typeof m === "object" ? m : {};
  const type = getPowermeter(src.type) ? src.type : base.type;
  return {
    type,
    suffix: typeof src.suffix === "string" ? src.suffix : "",
    phases: src.phases === 3 ? 3 : 1,
    netmask: typeof src.netmask === "string" ? src.netmask : "",
    fields: asObject(src.fields),
    tuning: asObject(src.tuning),
  };
}

// Fill in any keys added since the saved state was written, and constrain
// restored values to known-good shapes (untrusted: share link / project file).
export function migrate(s: any): State {
  const d = defaultState();
  s = s && typeof s === "object" ? s : {};
  const meters = Array.isArray(s.meters) && s.meters.length ? s.meters : d.meters;
  return {
    ...d,
    ...s,
    target: s.target === "esphome" ? "esphome" : "python",
    general: ((): State["general"] => {
      const sg = s.general && typeof s.general === "object" ? s.general : {};
      const dg = d.general;
      return {
        deviceTypes: Array.isArray(sg.deviceTypes) ? sg.deviceTypes.map((t: unknown) => String(t)) : dg.deviceTypes,
        deviceIds: asStr(sg.deviceIds, dg.deviceIds),
        skipPowermeterTest: asBool(sg.skipPowermeterTest, dg.skipPowermeterTest),
        webConfigEnabled: asBool(sg.webConfigEnabled, dg.webConfigEnabled),
        webServerPort: asStr(sg.webServerPort, dg.webServerPort),
        throttleInterval: asStr(sg.throttleInterval, dg.throttleInterval),
        waitForNextMessage: asStr(sg.waitForNextMessage, dg.waitForNextMessage),
        dedupeTimeWindow: asStr(sg.dedupeTimeWindow, dg.dedupeTimeWindow),
      };
    })(),
    esphome: { ...d.esphome, ...(s.esphome || {}) },
    ct: { fields: asObject(s.ct && s.ct.fields) },
    marstek: { enabled: !!(s.marstek && s.marstek.enabled), fields: asObject(s.marstek && s.marstek.fields) },
    mqttInsights: { enabled: !!(s.mqttInsights && s.mqttInsights.enabled), fields: asObject(s.mqttInsights && s.mqttInsights.fields) },
    meters: meters.map(cleanMeter),
  };
}
