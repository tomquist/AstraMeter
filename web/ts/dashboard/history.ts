// A short trailing history of the figures the cards show, kept in the browser.
//
// Deliberately not a backend series: the page already polls every couple of
// seconds, so the samples it needs to draw a trend are the ones it has already
// received. Nothing is stored, nothing survives a reload, and the backend
// grows no retention policy, no memory ceiling and no extra route for it.
//
// The trade is honest and worth stating: the line starts empty on every load
// and only covers as long as the tab has been open.

import { gridTotal } from "./model.js";
import type { ConsumerStatus, StatusSnapshot } from "./types.js";

/**
 * Samples kept per series. At the default 2 s poll that is about five
 * minutes — long enough to see a battery answer a change in the house, short
 * enough that the arrays stay small on a tab left open all day.
 */
export const HISTORY_LIMIT = 150;

/** Trailing samples per series, oldest first. */
export type SeriesHistory = Record<string, number[]>;

/** The series id for one battery's reported power. */
export function batterySeries(consumer: ConsumerStatus): string {
  return `battery:${consumer.consumer_id ?? ""}`;
}

/** The series id for one power source's total. */
export function meterSeries(name: string | undefined): string {
  return `meter:${name ?? ""}`;
}

/** The series id for the grid total. */
export const GRID_SERIES = "grid";

function push(history: SeriesHistory, id: string, value: number | null | undefined): void {
  if (typeof value !== "number" || !Number.isFinite(value)) return;
  const series = history[id] || (history[id] = []);
  series.push(value);
  if (series.length > HISTORY_LIMIT) series.splice(0, series.length - HISTORY_LIMIT);
}

/**
 * Fold one snapshot into the history.
 *
 * Called once per *new* snapshot rather than once per poll: an unchanged
 * revision comes back as a 304 with no body, and recording the previous value
 * again would draw a flat line through a gap where nothing was measured.
 */
export function recordSnapshot(
  history: SeriesHistory,
  snapshot: StatusSnapshot | null,
): void {
  if (!snapshot) return;
  // Through the same helper the headline reads, not a second summing rule of
  // its own: two CT devices each carry a share of the house, so taking one
  // device's figure would record a fraction and taking both would record two
  // samples for one moment. It also brings the per-phase and power-source
  // fallbacks, so the trend cannot disagree with the number above it.
  push(history, GRID_SERIES, gridTotal(snapshot));
  for (const device of snapshot.devices || []) {
    for (const consumer of device.consumers || []) {
      push(history, batterySeries(consumer), consumer.reported_power_w);
    }
  }
  for (const meter of snapshot.powermeters || []) {
    // A failed read leaves the previous value in place, so recording it would
    // draw a flat line across a moment nothing was measured — the same reason
    // an unchanged revision is not re-recorded above. gridTotal skips these
    // for the same reason; the two must agree on what counts as a reading.
    if (meter.last_read_ok === false) continue;
    push(history, meterSeries(meter.name), meter.last_total_w);
  }
}

/** Samples for a series, oldest first; empty when nothing has been seen. */
export function seriesOf(history: SeriesHistory, id: string): number[] {
  return history[id] || [];
}

/** A sparkline needs a shape, and one point is a dot, not a trend. */
export const MIN_POINTS = 3;

export interface SparkGeometry {
  /** `x,y` pairs for a polyline, in the given viewBox. */
  points: string;
  /** Where 0 W sits, so the crossing between charge and discharge is visible. */
  zeroY: number | null;
  min: number;
  max: number;
}

/**
 * Lay out samples in a `width` × `height` viewBox.
 *
 * The range always includes zero: these are signed watts, and a window that
 * happens to hold only export would otherwise draw it climbing off a baseline
 * that is not the one the reader assumes. A flat series is centred rather
 * than divided by a zero span.
 */
export function sparkGeometry(
  values: number[],
  width: number,
  height: number,
): SparkGeometry | null {
  if (values.length < MIN_POINTS) return null;
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values);
  const span = max - min;
  const y = (value: number) =>
    span === 0 ? height / 2 : height - ((value - min) / span) * height;
  const step = width / (values.length - 1);
  return {
    points: values.map((v, i) => `${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(" "),
    zeroY: min <= 0 && max >= 0 ? Number(y(0).toFixed(1)) : null,
    min,
    max,
  };
}
