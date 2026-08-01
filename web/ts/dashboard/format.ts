// Value formatting. Every function here treats undefined as "absent" and
// returns null for it, so a caller can omit the row rather than print a
// placeholder that reads like a measurement.

/** A watt value with a sign that always means the same thing. */
export function watts(value: number | undefined | null): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  const rounded = Math.round(value);
  if (Math.abs(rounded) >= 1000) {
    return `${(rounded / 1000).toFixed(2)} kW`;
  }
  return `${rounded} W`;
}

/** Watts with an explicit sign, for values where direction is the point. */
export function signedWatts(value: number | undefined | null): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  const text = watts(Math.abs(value));
  if (text === null) return null;
  if (Math.round(value) === 0) return text;
  return `${value < 0 ? "−" : "+"}${text}`;
}

export function percent(
  value: number | undefined | null,
  digits = 0,
): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  return `${(value * 100).toFixed(digits)}%`;
}

export function seconds(value: number | undefined | null): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  if (value < 1) return `${value.toFixed(1)} s`;
  if (value < 90) return `${Math.round(value)} s`;
  if (value < 5400) return `${Math.round(value / 60)} min`;
  return `${(value / 3600).toFixed(1)} h`;
}

/** A short relative age, e.g. "4 s ago". */
export function ago(value: number | undefined | null): string | null {
  const text = seconds(value);
  return text === null ? null : `${text} ago`;
}

/**
 * Absolute wall-clock time.
 *
 * Used in place of every relative age once the stream goes offline: a frozen
 * "0.4 s ago" is indistinguishable from a fresh one, whereas a clock time
 * that stops advancing is obviously stale.
 */
export function clockTime(iso: string | undefined | null): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return date.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function duration(value: number | undefined | null): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  const total = Math.floor(value);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  if (minutes) return `${minutes}m`;
  return `${total}s`;
}

/** Phase letter as the label users see in Home Assistant. */
export function phaseLabel(phase: string | undefined): string | null {
  if (!phase) return null;
  if (phase === "D") return "All phases";
  if (phase === "0") return "Unassigned";
  return `Phase ${phase}`;
}

/** A battery MAC in the grouped form the Marstek app shows. */
export function macLabel(id: string | undefined): string | null {
  if (!id) return null;
  const clean = id.replace(/[^0-9a-fA-F]/g, "");
  if (clean.length !== 12) return id;
  return (clean.toUpperCase().match(/.{2}/g) || []).join(":");
}

/** Short, stable name for a battery card heading. */
export function batteryName(
  consumerId: string | undefined,
  deviceType: string | undefined,
): string {
  const tail = (consumerId || "").replace(/[^0-9a-fA-F]/g, "").slice(-4);
  const model = (deviceType || "").trim();
  if (model && tail) return `${model} ·${tail.toUpperCase()}`;
  if (tail) return `Battery ·${tail.toUpperCase()}`;
  return "Battery";
}
