// Labels for the guided form, and a parser for the add-on's own schema
// strings.
//
// The *types* come from Supervisor at runtime (`data.schema`), so this file
// holds only the human copy. An option missing from OPTION_META still
// renders — in a trailing "Other" group with a title-cased name — which is
// deliberately the drift behaviour: a new add-on option appears in the form
// as soon as config.yaml has it, rather than disappearing silently or
// failing a test.

export interface OptionSpec {
  type: string;
  min?: number;
  max?: number;
  options?: string[];
  optional: boolean;
  /**
   * The spec was not a validator string — Supervisor describes a repeated or
   * nested option as a list or an object. Shown, never edited: a text box
   * over one of those would write back a string and flatten the real value.
   */
  unsupported?: string;
}

/**
 * Parse a bashio validator string.
 *
 * Grammar: `type[(min,max)|(a|b|c)][?]`, e.g. `float(0,1)?`,
 * `list(critical|error|info)`, `int(0,)?`, `password?`.
 * An unknown type falls back to a text input rather than dropping the field.
 *
 * Takes `unknown` because the schema is Supervisor's, not ours: an option
 * declared as a list or a nested block arrives as an array or an object, and
 * assuming a string there threw inside render — which killed not just this
 * field but every later repaint of the whole page.
 */
export function parseAddonSchema(spec: unknown): OptionSpec {
  if (spec != null && typeof spec !== "string") {
    let shown: string;
    try {
      shown = JSON.stringify(spec) ?? String(spec);
    } catch {
      shown = String(spec);
    }
    return { type: "unsupported", optional: true, unsupported: shown };
  }
  const raw = (spec || "str").trim();
  const optional = raw.endsWith("?");
  const body = optional ? raw.slice(0, -1) : raw;
  const match = /^([a-z_]+)(?:\((.*)\))?$/.exec(body);
  if (!match) return { type: "str", optional };
  const type = match[1];
  const arg = match[2];
  if (arg == null) return { type, optional };
  if (type === "list" || type === "match") {
    return { type, options: arg.split("|").filter(Boolean), optional };
  }
  const [lo, hi] = arg.split(",");
  return {
    type,
    min: lo ? Number(lo) : undefined,
    max: hi ? Number(hi) : undefined,
    optional,
  };
}

/** Supervisor's descriptor type names, mapped onto the ones used above. */
const DESCRIPTOR_TYPES: Record<string, string> = {
  string: "str",
  integer: "int",
  float: "float",
  boolean: "bool",
  select: "list",
};

/**
 * One field descriptor from Supervisor's rendered schema.
 *
 * Shape: `{"name": "grid_predict_trust", "lengthMin": 0, "lengthMax": 1,
 * "optional": true, "type": "float"}`. `lengthMin`/`lengthMax` carry the
 * bounds for a number and the length limits for a string, so they are only
 * read as bounds for the numeric types.
 */
function specFromDescriptor(node: Record<string, unknown>): OptionSpec {
  const optional = node.optional === true || node.required !== true;
  // A repeated option or a nested block holds a list or an object; editing
  // one in a text box here would write a string back over the real value.
  if (node.multiple === true || node.type === "schema") {
    return {
      type: "unsupported",
      optional: true,
      unsupported: node.type === "schema" ? "a nested block" : "a repeated value",
    };
  }
  const kind = typeof node.type === "string" ? node.type : "string";
  const type =
    node.format === "password" ? "password" : (DESCRIPTOR_TYPES[kind] ?? "str");
  const spec: OptionSpec = { type, optional };
  if (type === "list") {
    spec.options = Array.isArray(node.options) ? node.options.map(String) : [];
  }
  if (type === "int" || type === "float") {
    if (typeof node.lengthMin === "number") spec.min = node.lengthMin;
    if (typeof node.lengthMax === "number") spec.max = node.lengthMax;
  }
  return spec;
}

/**
 * Supervisor's add-on schema as `{option name: spec}`, whichever shape it is.
 *
 * `/addons/self/info` does not return the `name: validator` mapping an add-on
 * declares in config.yaml — Supervisor renders that into a *list* of field
 * descriptors first, and the list is what a dashboard sees. Reading it as a
 * mapping keyed the whole form by array index, so every control came out
 * labelled 0, 1, 2 with the descriptor printed underneath. The mapping form
 * is still accepted: it is what the add-on itself declares, and what an
 * older or differently-shaped Supervisor may hand back.
 */
export function normalizeAddonSchema(raw: unknown): Record<string, OptionSpec> {
  const specs: Record<string, OptionSpec> = {};
  if (Array.isArray(raw)) {
    for (const entry of raw) {
      if (!entry || typeof entry !== "object" || Array.isArray(entry)) continue;
      const node = entry as Record<string, unknown>;
      if (typeof node.name !== "string" || !node.name) continue;
      specs[node.name] = specFromDescriptor(node);
    }
    return specs;
  }
  if (raw && typeof raw === "object") {
    for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
      specs[key] = parseAddonSchema(value);
    }
  }
  return specs;
}

export interface OptionMeta {
  label: string;
  group: string;
  help?: string;
  /** Render as a Home Assistant entity picker rather than a text box. */
  entity?: boolean;
  /**
   * The entity option this one must have the same number of sensors as.
   *
   * With import and export on separate sensors the two lists are zipped per
   * phase, and a length mismatch aborts start-up
   * (`powermeter/homeassistant.py`) — long after the page that caused it.
   */
  entityPeer?: string;
}

const GRID = "Grid measurement";
const DEVICE = "Emulated meter";
const CONTROL = "Battery control";
const TUNING = "Fine tuning";
const ADVANCED = "Advanced";

export const OPTION_META: Record<string, OptionMeta> = {
  power_input_alias: {
    label: "Grid power sensor",
    group: GRID,
    help:
      "The Home Assistant entity holding your grid power in watts. " +
      "One sensor for a whole-house total, or one per phase for a " +
      "three-phase meter.",
    entity: true,
  },
  power_output_alias: {
    label: "Export power sensor",
    group: GRID,
    help:
      "Only if import and export are two separate sensors. Give the same " +
      "number of sensors as the grid power above — they are paired per phase.",
    entity: true,
    entityPeer: "power_input_alias",
  },
  power_offset: {
    label: "Power offset (W)",
    group: GRID,
    help: "Added to the reading, in watts. One value, or one per phase.",
  },
  power_multiplier: {
    label: "Power multiplier",
    group: GRID,
    // The upgrade trap: a kW sensor used to need 1000 here, and since units
    // are converted automatically that same 1000 now scales it twice.
    help:
      "Usually 1. A kW sensor is converted to watts on its own — if you set " +
      "1000 to work around that, remove it or the reading is scaled twice. " +
      "Use −1 to flip a backwards CT.",
  },
  throttle_interval: {
    label: "Throttle interval (s)",
    group: GRID,
    help: "Minimum time between meter reads. 0 reads as fast as the batteries poll.",
  },
  wait_for_next_message: {
    label: "Wait for a fresh reading",
    group: GRID,
    help: "Answer a battery only once a new meter value has arrived.",
  },
  device_types: {
    label: "Emulated device",
    group: DEVICE,
    help: "Which meter AstraMeter pretends to be. Use ct002 for Marstek batteries.",
  },
  ct_mac: { label: "CT MAC address", group: DEVICE },
  active_control: {
    label: "Active control",
    group: CONTROL,
    help: "Let AstraMeter compute each battery's target instead of relaying the raw total.",
  },
  fair_distribution: {
    label: "Share load between batteries",
    group: CONTROL,
  },
  min_efficient_power: {
    label: "Minimum efficient power (W)",
    group: CONTROL,
    help: "Below this, concentrate on fewer batteries instead of running them all inefficiently.",
  },
  efficiency_rotation_interval: {
    label: "Rotation interval (s)",
    group: CONTROL,
    help: "How often the batteries swap who takes the load.",
  },
  min_dc_output: { label: "Minimum DC output (W)", group: CONTROL },
  grid_predict_trust: {
    label: "Grid prediction trust",
    group: CONTROL,
    help: "0 trusts the meter only; 1 trusts the model. 0.5 is a good default.",
  },
  dashboard: {
    label: "Show the dashboard",
    group: ADVANCED,
    help: "Opens from the Home Assistant sidebar.",
  },
  dashboard_allow_write: {
    label: "Allow changes from the dashboard",
    group: ADVANCED,
    help: "Lets the dashboard edit this configuration and control batteries.",
  },
  dashboard_direct_access: {
    label: "Allow access outside Home Assistant",
    group: ADVANCED,
    help: "Exposes the dashboard on port 52500 with no authentication. Trusted networks only.",
  },
  custom_config: {
    label: "Custom config file",
    group: ADVANCED,
    help: "Filename in /config to use instead of these options.",
  },
  log_level: { label: "Log level", group: ADVANCED },
  mqtt_uri: { label: "MQTT broker URL", group: ADVANCED },
  marstek_auto_register_ct_device: {
    label: "Register a CT with Marstek",
    group: ADVANCED,
  },
  marstek_mailbox: { label: "Marstek account e-mail", group: ADVANCED },
  marstek_password: { label: "Marstek password", group: ADVANCED },
  cloud_reporting: { label: "Report to the Marstek cloud", group: ADVANCED },
  cloud_reporting_host: { label: "Cloud host", group: ADVANCED },
  cloud_reporting_interval: { label: "Cloud interval (s)", group: ADVANCED },
  balance_gain: { label: "Balance gain", group: TUNING },
  balance_deadband: { label: "Balance deadband (W)", group: TUNING },
  max_correction_per_step: { label: "Max correction per step (W)", group: TUNING },
  error_boost_threshold: { label: "Error boost threshold (W)", group: TUNING },
  error_boost_max: { label: "Error boost max", group: TUNING },
  error_reduce_threshold: { label: "Error reduce threshold (W)", group: TUNING },
  max_target_step: { label: "Max target step (W)", group: TUNING },
  pace_base_step: { label: "Pace base step (W)", group: TUNING },
  pace_max_step: { label: "Pace max step (W)", group: TUNING },
  osc_damp_max: { label: "Oscillation damping max", group: TUNING },
  osc_damp_alpha: { label: "Oscillation damping alpha", group: TUNING },
  osc_damp_decay: { label: "Oscillation damping decay", group: TUNING },
  osc_damp_threshold: { label: "Oscillation threshold (W)", group: TUNING },
  concentrate_deadband: { label: "Concentrate deadband (W)", group: TUNING },
  import_trim_w: { label: "Import trim (W)", group: TUNING },
  dedupe_time_window: { label: "Dedupe window (s)", group: TUNING },
  smooth_target_alpha: { label: "Target smoothing", group: TUNING },
  max_smooth_step: { label: "Max smoothing step (W)", group: TUNING },
  deadband: { label: "Deadband (W)", group: TUNING },
  hampel_window: { label: "Spike filter window", group: TUNING },
  hampel_n_sigma: { label: "Spike filter sigma", group: TUNING },
  hampel_min_threshold: { label: "Spike filter threshold (W)", group: TUNING },
  pid_kp: { label: "PID proportional", group: TUNING },
  pid_ki: { label: "PID integral", group: TUNING },
  pid_kd: { label: "PID derivative", group: TUNING },
  pid_output_max: { label: "PID output max", group: TUNING },
  pid_mode: { label: "PID mode", group: TUNING },
};
