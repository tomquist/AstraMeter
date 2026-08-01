// App state and the derived values the views read.
//
// Kept free of DOM so it is testable in plain Node, matching how the rest of
// web/ts is tested.

import type {
  ConsumerStatus,
  DeviceStatus,
  PowermeterStatus,
  StatusSnapshot,
} from "./types.js";
import type { ConnectionState } from "./transport.js";

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
  };
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
  const batteries = allConsumers(state.snapshot);
  if (batteries.length === 0) return health("warn", "No batteries reporting");
  if (batteries.some((c) => c.expired)) return health("warn", "Battery missing");
  return health("ok", "Steering");
}

export function allConsumers(snapshot: StatusSnapshot | null): ConsumerStatus[] {
  if (!snapshot) return [];
  return (snapshot.devices || []).flatMap((d) => d.consumers || []);
}

/** Devices that actually steer batteries (CT002/CT003). */
export function ctDevices(snapshot: StatusSnapshot | null): DeviceStatus[] {
  if (!snapshot) return [];
  return (snapshot.devices || []).filter((d) => d.kind === "ct002");
}

/** Whole-house grid power: + import, − export. */
export function gridTotal(snapshot: StatusSnapshot | null): number | undefined {
  const device = ctDevices(snapshot).find((d) => d.grid);
  const grid = device?.grid;
  if (!grid) return undefined;
  if (grid.grid_total_w != null) return grid.grid_total_w;
  const phases = [grid.l1_w, grid.l2_w, grid.l3_w].filter(
    (v): v is number => v != null,
  );
  return phases.length ? phases.reduce((a, b) => a + b, 0) : undefined;
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
