// App state and the derived values the views read.
//
// Kept free of DOM so it is testable in plain Node, matching how the rest of
// web/ts is tested.

import type {
  ConsumerStatus,
  DeviceStatus,
  PowermeterStatus,
  ShellyBatteryStatus,
  StatusSnapshot,
} from "./types.js";
import type { ConnectionState } from "./transport.js";
import type { SeriesHistory } from "./history.js";

export type Tab = "overview" | "batteries" | "sources" | "config" | "diagnostics";

export interface AppState {
  tab: Tab;
  snapshot: StatusSnapshot | null;
  connection: ConnectionState;
  /** Consecutive failed polls; drives the offline banner copy. */
  failures: number;
  lastFrameAt: number | null;
  error: string | null;
  notice: string | null;
  /** Controls awaiting a server round trip, keyed `consumerId:field`. */
  busy: Record<string, boolean>;
  /**
   * The value the user just asked for, held until the server confirms it.
   *
   * Without this the next poll — up to a full interval later — re-renders the
   * control with the server's *old* value and visibly snaps the switch back
   * under the user's finger.
   */
  pending: Record<string, unknown>;
  /**
   * Trailing samples per series, for the trend lines on the cards.
   *
   * Accumulated in the browser from the polls the page already makes — the
   * backend keeps no history (see history.ts).
   */
  history: SeriesHistory;
}

export function initialState(): AppState {
  return {
    tab: "overview",
    snapshot: null,
    connection: "connecting",
    failures: 0,
    lastFrameAt: null,
    error: null,
    notice: null,
    busy: {},
    pending: {},
    history: {},
  };
}

/** The user's in-flight value for a control, else the reported one. */
export function pendingOr<T>(state: AppState, key: string, actual: T): T {
  return (key in state.pending ? (state.pending[key] as T) : actual);
}

export type Severity = "ok" | "warn" | "err" | "idle";

export interface Health {
  severity: Severity;
  glyph: string;
  label: string;
}

// Colour never carries meaning alone: each severity has its own glyph so the
// page survives greyscale printing and colour-blindness.
const GLYPH: Record<Severity, string> = {
  ok: "●",
  warn: "▲",
  err: "✕",
  idle: "○",
};

export function health(severity: Severity, label: string): Health {
  return { severity, glyph: GLYPH[severity], label };
}

/** The one-line answer to "is it working?". */
export function overallHealth(state: AppState): Health {
  if (state.connection === "offline") return health("err", "Disconnected");
  if (!state.snapshot) return health("idle", "Connecting");
  const devices = state.snapshot.devices || [];
  if (devices.length === 0) return health("warn", "Starting up");
  const meterDown = (state.snapshot.powermeters || []).some(
    (m) => m.online === false || m.last_read_ok === false,
  );
  if (meterDown) return health("err", "Meter unavailable");
  if (devices.some((d) => d.grid?.meter_failed)) {
    return health("err", "Meter unavailable");
  }
  const consumers = reportingConsumers(state.snapshot);
  const polling = shellyBatteries(state.snapshot);
  if (consumers.length === 0 && polling.length === 0) {
    return health("warn", "No batteries reporting");
  }
  if (consumers.some((c) => c.expired)) return health("warn", "Battery missing");
  if (polling.some((b) => b.active === false)) {
    return health("warn", "Battery missing");
  }
  // A Shelly emulator serves readings; it does not steer anything, so saying
  // "Steering" there would claim a behaviour this device does not have.
  if (consumers.length === 0) return health("ok", "Serving readings");
  return health("ok", "Steering");
}

export function allConsumers(snapshot: StatusSnapshot | null): ConsumerStatus[] {
  if (!snapshot) return [];
  return (snapshot.devices || []).flatMap((d) => d.consumers || []);
}

/**
 * Whether the emulator has ever heard from this battery.
 *
 * A consumer can exist without one: a retained MQTT command — or a control
 * written while the battery was away — creates a placeholder to hold that
 * setting until the battery turns up, and the emulator deliberately never
 * expires it. It is a saved preference, not hardware, and the steering loop
 * already ignores it. Counting it as a battery invents a device the user
 * does not have.
 */
export function hasReported(consumer: ConsumerStatus): boolean {
  // The backend states this; absence means "a battery", so a reduced document
  // that omits it does not turn every battery into a placeholder.
  return consumer.never_reported !== true;
}

/** The batteries actually on the network. */
export function reportingConsumers(
  snapshot: StatusSnapshot | null,
): ConsumerStatus[] {
  return allConsumers(snapshot).filter(hasReported);
}

/** Devices that actually steer batteries (CT002/CT003). */
export function ctDevices(snapshot: StatusSnapshot | null): DeviceStatus[] {
  if (!snapshot) return [];
  return (snapshot.devices || []).filter((d) => d.kind === "ct002");
}

/**
 * Shelly emulators, which serve a meter reading and steer nothing.
 *
 * They report their batteries as liveness (`batteries`), not as steerable
 * consumers, so they are deliberately a separate list from `ctDevices` rather
 * than something the CT views try to render.
 */
export function shellyDevices(snapshot: StatusSnapshot | null): DeviceStatus[] {
  if (!snapshot) return [];
  return (snapshot.devices || []).filter((d) => d.kind === "shelly");
}

/** Batteries polling a Shelly emulator, across every such device. */
export function shellyBatteries(
  snapshot: StatusSnapshot | null,
): ShellyBatteryStatus[] {
  return shellyDevices(snapshot).flatMap((d) => d.batteries || []);
}

/** Whole-house grid power: + import, − export.
 *
 * Summed over every reporting emulator, because `allConsumers` aggregates
 * batteries across all of them — taking one emulator's grid reading would
 * show a house total against contributions from a larger set.
 */
export function gridTotal(snapshot: StatusSnapshot | null): number | undefined {
  let total: number | undefined;
  for (const device of ctDevices(snapshot)) {
    const grid = device.grid;
    if (!grid) continue;
    let value = grid.grid_total_w;
    if (value == null) {
      const phases = [grid.l1_w, grid.l2_w, grid.l3_w].filter(
        (v): v is number => v != null,
      );
      value = phases.length ? phases.reduce((a, b) => a + b, 0) : undefined;
    }
    if (value != null) total = (total ?? 0) + value;
  }
  if (total != null) return total;
  // No CT emulator served a reading — either none is configured (a Shelly
  // install) or none has answered yet. The power source measured the same
  // quantity, summed the same way, so the hero can show the house total
  // instead of a dash.
  for (const meter of snapshot?.powermeters || []) {
    if (meter.last_read_ok === false) continue;
    if (meter.last_total_w != null) total = (total ?? 0) + meter.last_total_w;
  }
  return total;
}

/**
 * A battery's contribution to the grid, signed the same way the grid is.
 *
 * A discharging battery (positive reported power) pushes the grid down, so
 * its contribution is negative. Plotting it on the grid's own axis is what
 * makes the hero readable: the battery bars are visibly what pulls the grid
 * bar toward zero.
 */
export function contribution(consumer: ConsumerStatus): number | undefined {
  const reported = consumer.reported_power_w;
  if (reported == null) return undefined;
  return -reported;
}

/** Largest magnitude on the rail, so grid and batteries share one scale. */
export function railScale(
  grid: number | undefined,
  consumers: ConsumerStatus[],
): number {
  const magnitudes = [Math.abs(grid ?? 0)];
  for (const c of consumers) {
    const value = contribution(c);
    if (value != null) magnitudes.push(Math.abs(value));
  }
  const peak = Math.max(...magnitudes, 1);
  // Round up to a friendly step so the axis labels are readable and the
  // scale does not jitter on every frame.
  const step = peak <= 200 ? 100 : peak <= 1000 ? 250 : peak <= 3000 ? 500 : 1000;
  return Math.ceil(peak / step) * step;
}

export function batteryHealth(consumer: ConsumerStatus): Health {
  // Dominates everything below: with no report there is no power, no phase
  // and no balancer state to describe — only a setting waiting for hardware.
  if (!hasReported(consumer)) return health("idle", "Never reported");
  if (consumer.expired) return health("err", "Not reporting");
  if (consumer.active === false) return health("idle", "Disabled");
  if (consumer.manual_enabled) return health("warn", "Manual");
  const saturation = consumer.balancer?.saturation;
  if (saturation != null && saturation >= 0.85) return health("warn", "Saturated");
  if (consumer.balancer?.deprioritized) return health("idle", "Parked");
  return health("ok", "Auto");
}

export function meterHealth(meter: PowermeterStatus): Health {
  if (meter.online === false) return health("err", "Offline");
  if (meter.last_read_ok === false) return health("err", "Read failed");
  // A push meter that has gone quiet is stale long before it is "offline".
  if (meter.last_read_age_s != null && meter.last_read_age_s > 60) {
    return health("warn", "Stale");
  }
  if (meter.online == null && meter.last_read_ok == null) {
    return health("idle", "Idle");
  }
  return health("ok", "Live");
}

/** Poll cadence, clamped so a bad backend value cannot melt the browser. */
export function pollInterval(snapshot: StatusSnapshot | null): number {
  const advised = snapshot?.capabilities?.poll_interval_ms ?? 2000;
  return Math.min(30000, Math.max(500, advised));
}
