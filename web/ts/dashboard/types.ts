// The status document served by GET /api/status.
//
// Only `schema_version`, `generated_at` and `capabilities` are guaranteed.
// EVERY other field is optional at every depth, and a backend may omit whole
// sections — an ESPHome build will serve a small fraction of this document.
// The UI must therefore render what it receives and omit the rest; it must
// never substitute 0 or "—" for a missing value, because that reports "the
// backend didn't send this" and "the real value is zero" identically.

export interface Capabilities {
  backend?: "python" | "esphome";
  stream?: boolean; // a push transport exists (reserved; false today)
  poll_interval_ms?: number; // backend-advised floor; client clamps
  config_mode?: "ha_simple" | "ha_advanced" | "standalone";
  config_writable?: boolean; // the raw config.ini editor may write
  ha_options?: boolean; // Supervisor options API reachable
  controls?: boolean; // per-battery write endpoints exist
  restart_process?: boolean;
  restart_supervisor?: boolean;
  balancer_internals?: boolean;
  ingress?: boolean; // this request arrived via HA ingress
}

export interface ServiceInfo {
  version?: string;
  git_commit?: string;
  log_level?: string;
  config_path?: string;
  config_mtime_at?: string;
  runtime?: "docker" | "ha_addon";
  addon_slug?: string;
  restart_pending?: boolean;
  started_at?: string;
  web?: { port?: number; ingress?: boolean };
}

export interface PowermeterStatus {
  name?: string; // config section, e.g. "HOMEWIZARD"
  kind?: string; // innermost class, e.g. "HomewizardPowermeter"
  pipeline?: string[]; // wrappers applied, innermost first
  // Tri-state. null/undefined = a pull meter whose liveness cannot be known
  // without I/O; that is NOT the same as false ("known down").
  online?: boolean | null;
  last_read_age_s?: number;
  last_read_ok?: boolean;
  last_values_w?: number[];
  last_total_w?: number;
}

export interface BalancerConsumer {
  last_target_w?: number;
  last_intent_w?: number;
  last_intent_reading_w?: number;
  saturation?: number; // 0..1
  saturation_grace_remaining_s?: number;
  fade_weight?: number; // 0..1
  deprioritized?: boolean;
  pace?: { cap_w?: number; sign?: number };
  oscillation?: { score?: number; last_sign?: number };
}

export interface ConsumerStatus {
  consumer_id?: string; // battery MAC, or "ip:port" before one is known
  device_type?: string;
  capabilities?: {
    builtin_inverter?: boolean;
    ac_input?: boolean;
    dc_input?: boolean;
  };
  last_ip?: string;
  phase?: string; // "A" | "B" | "C" | "D" | "0"
  bucket?: string; // "A" | "B" | "C" | "ABC" | "x"
  participates?: boolean;
  reported_power_w?: number; // + discharge, − charge
  last_instructed_power_w?: number;
  target_w?: { l1?: number; l2?: number; l3?: number };
  last_seen_at?: string;
  last_seen_age_s?: number;
  /** Set only for a placeholder holding a setting for an absent battery. */
  never_reported?: boolean;
  poll_interval_s?: number;
  /** How often we actually reply; exceeds poll_interval_s when a dedupe window drops polls. */
  answer_interval_s?: number;
  ttl_s?: number;
  expired?: boolean;
  in_flight?: boolean;
  mode?: "auto" | "manual" | "inactive";
  active?: boolean;
  manual_enabled?: boolean;
  manual_target_w?: number;
  distribution_weight?: number;
  efficiency_window_weight_pct?: number; // 0..100, same unit as the MQTT entity
  min_dc_output_w?: number;
  min_dc_output_applicable?: boolean;
  balancer?: BalancerConsumer;
}

export interface BucketStatus {
  chrg_w?: number;
  dchrg_w?: number;
  count?: number;
  active?: boolean;
}

/**
 * The balancer's verdict on how well the loop is holding the grid at zero.
 *
 * `verdict` is one of idle / warmup / stable / off_target / limited, but it
 * stays a plain string here: a backend serving a reduced document may add a
 * verdict this bundle predates, and the page must render what it is given
 * rather than drop the card.
 *
 * `score_pct` is absent while there is nothing to score — the backend omits it
 * rather than sending a 100 it has no evidence for.
 */
export interface ControlQualityStatus {
  verdict?: string;
  score_pct?: number;
  error_w?: number;
  in_band_fraction?: number;
  crossings_per_min?: number;
  band_w?: number;
  samples?: number;
}

export interface DeviceStatus {
  kind?: "ct002" | "shelly";
  device_id?: string;
  device_type?: string;
  ct_type?: string;
  ct_mac?: string;
  udp_port?: number;
  wifi_rssi_dbm?: number;
  running?: boolean;
  started_at?: string;
  control?: {
    active_control?: boolean;
    consumer_ttl_s?: number;
    dedupe_window_s?: number;
    debug_status?: boolean;
    info_idx?: number;
  };
  grid?: {
    l1_w?: number;
    l2_w?: number;
    l3_w?: number;
    grid_total_w?: number;
    sample_at?: string;
    meter_failed?: boolean;
    consecutive_meter_failures?: number;
  };
  buckets?: Record<string, BucketStatus>;
  balancer?: Record<string, any> & { control_quality?: ControlQualityStatus };
  consumers?: ConsumerStatus[];
  orphan_overrides?: Record<string, any>[];
  // Shelly only. A Shelly emulator steers nothing, so its batteries carry
  // liveness rather than power: it serves the meter reading and they poll it.
  batteries?: ShellyBatteryStatus[];
  inactive_timeout_s?: number;
}

export interface ShellyBatteryStatus {
  ip?: string;
  last_seen_at?: string;
  last_seen_age_s?: number;
  poll_interval_s?: number;
  active?: boolean;
}

export interface StatusSnapshot {
  schema_version: number;
  generated_at: string;
  capabilities: Capabilities;
  seq?: number;
  rev?: number;
  uptime_s?: number;
  service?: ServiceInfo;
  powermeters?: PowermeterStatus[];
  devices?: DeviceStatus[];
  integrations?: Record<string, any>;
}
