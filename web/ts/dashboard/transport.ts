// How the UI reaches its backend.
//
// The whole point of this indirection is that an ESPHome build can supply a
// different implementation without the views changing: they consume a
// Transport and a Capabilities object, never a URL and never a backend
// identity. Today there is exactly one implementation (polling over
// fetch); the interface is the seam where a second one attaches.
//
// Every URL is document-relative with NO leading slash. Home Assistant
// ingress serves the page from /api/hassio_ingress/<token>/, so a leading
// slash escapes the prefix and 404s.

import type { StatusSnapshot } from "./types.js";

export type ConnectionState = "connecting" | "live" | "offline";

/** Deadline for any single request; long enough for a slow restart to answer. */
const REQUEST_TIMEOUT_MS = 15000;

export interface HaEntity {
  entity_id: string;
  name?: string;
  unit?: string;
  device_class?: string;
  state?: string;
}

export interface Transport {
  /** Latest snapshot, or null when nothing changed since the last poll. */
  fetchStatus(): Promise<StatusSnapshot | null>;
  /** Write a per-battery control value. */
  controlConsumer(
    deviceId: string,
    consumerId: string,
    field: string,
    value: unknown,
  ): Promise<void>;
  /** Write a device-wide control value. */
  controlDevice(deviceId: string, field: string, value: unknown): Promise<void>;
  /** Raw config.ini as {sections, order}, secrets already redacted. */
  getConfig(): Promise<{ sections: Record<string, Record<string, string>>; order: string[] }>;
  saveConfig(
    sections: Record<string, Record<string, string>>,
    order: string[],
  ): Promise<void>;
  /** Per-section key type metadata, so the editor can render typed controls. */
  getKeyTypes(): Promise<Record<string, Record<string, unknown>>>;
  /** Add-on options plus the schema to render them from. */
  getAddonOptions(): Promise<{
    options: Record<string, unknown>;
    schema: Record<string, string>;
    slug?: string;
  }>;
  saveAddonOptions(
    options: Record<string, unknown>,
    restart: boolean,
  ): Promise<void>;
  /** Home Assistant sensors that could be a grid-power source. */
  listPowerEntities(): Promise<HaEntity[]>;
  switchConfigMode(mode: "file" | "options", filename?: string): Promise<void>;
  restart(): Promise<void>;
}

export class TransportError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  path: string,
  init?: RequestInit & { etag?: string },
): Promise<{ data: T | null; etag: string | null; status: number }> {
  const headers: Record<string, string> = {};
  if (init?.etag) headers["If-None-Match"] = init.etag;
  if (init?.body) headers["Content-Type"] = "application/json";
  // Without a deadline a hung connection never settles, and the poll loop
  // only re-arms once the previous request finishes — so the page would sit
  // on stale values forever instead of reporting itself offline.
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), REQUEST_TIMEOUT_MS);
  let response: Response;
  try {
    response = await fetch(path, { ...init, headers, signal: abort.signal });
  } finally {
    clearTimeout(timer);
  }
  if (response.status === 304) {
    return { data: null, etag: init?.etag ?? null, status: 304 };
  }
  const text = await response.text();
  let parsed: any = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }
  if (!response.ok) {
    const message =
      (parsed && typeof parsed.error === "string" && parsed.error) ||
      `Request failed (${response.status})`;
    throw new TransportError(message, response.status);
  }
  return {
    data: parsed as T,
    etag: response.headers.get("ETag"),
    status: response.status,
  };
}

/** HTTP polling transport with ETag revalidation. */
export class PollTransport implements Transport {
  private etag: string | null = null;

  async fetchStatus(): Promise<StatusSnapshot | null> {
    const { data, etag } = await request<StatusSnapshot>("api/status", {
      etag: this.etag ?? undefined,
    });
    if (data === null) return null;
    // Adopt the ETag only after the body parsed, so a truncated or invalid
    // response cannot pin the client into a 304 loop against bad state.
    this.etag = etag;
    return data;
  }

  async controlConsumer(
    deviceId: string,
    consumerId: string,
    field: string,
    value: unknown,
  ): Promise<void> {
    await request("api/control/consumer", {
      method: "POST",
      body: JSON.stringify({
        device_id: deviceId,
        consumer_id: consumerId,
        field,
        value,
      }),
    });
  }

  async controlDevice(
    deviceId: string,
    field: string,
    value: unknown,
  ): Promise<void> {
    await request("api/control/device", {
      method: "POST",
      body: JSON.stringify({ device_id: deviceId, field, value }),
    });
  }

  async getConfig() {
    const { data } = await request<{
      sections: Record<string, Record<string, string>>;
      order: string[];
    }>("api/config");
    return data ?? { sections: {}, order: [] };
  }

  async saveConfig(
    sections: Record<string, Record<string, string>>,
    order: string[],
  ): Promise<void> {
    await request("api/config", {
      method: "POST",
      body: JSON.stringify({ sections, order }),
    });
  }

  async getKeyTypes() {
    const { data } = await request<Record<string, Record<string, unknown>>>(
      "api/key-types",
    );
    return data ?? {};
  }

  async getAddonOptions() {
    const { data } = await request<{
      options: Record<string, unknown>;
      schema: Record<string, string>;
      slug?: string;
    }>("api/addon/options");
    return data ?? { options: {}, schema: {} };
  }

  async saveAddonOptions(
    options: Record<string, unknown>,
    restart: boolean,
  ): Promise<void> {
    await request("api/addon/options", {
      method: "POST",
      body: JSON.stringify({ options, restart }),
    });
  }

  async listPowerEntities(): Promise<HaEntity[]> {
    const { data } = await request<{ entities: HaEntity[] }>("api/ha/entities");
    return data?.entities ?? [];
  }

  async switchConfigMode(mode: "file" | "options", filename?: string) {
    await request("api/config-mode", {
      method: "POST",
      body: JSON.stringify({ mode, filename }),
    });
  }

  async restart(): Promise<void> {
    await request("api/restart", { method: "POST" });
  }
}
