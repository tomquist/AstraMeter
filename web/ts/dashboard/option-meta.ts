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
  /**
   * What this option does, in one or two sentences.
   *
   * Required in practice: `optionCoverage` in `dashboard.test.ts` fails on an
   * add-on option that has none. A form of bare labels is not a guided setup
   * — the add-on's own Configuration page documents every one of these, and
   * the point of this form is to be at least as good.
   */
  help?: string;
  /**
   * The value that applies when the box is left empty, shown in it.
   *
   * Only where AstraMeter's own default is meant to be a fine answer, so an
   * empty field says what will happen rather than "optional". Never repeated
   * in `help` — it would then be stated twice on the same field.
   */
  placeholder?: string;
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
const READING = "Meter reading";
const FILTERS = "Signal filters";
const TUNING = "Balancer tuning";
const CLOUD = "Marstek cloud";
const ADDON = "Add-on and dashboard";

/** Where an option lands when this table has never heard of it. */
export const OTHER_GROUP = "Other";

export interface GroupMeta {
  title: string;
  /** One line under the heading: what the group is for. */
  blurb: string;
  /**
   * Folded shut until the user asks for it.
   *
   * Everything here has a working default, so a first-time setup is the two
   * open groups and nothing else. A form that opens with fifty numeric boxes
   * is not guided, however well each one is labelled.
   */
  advanced?: boolean;
}

/**
 * The groups, in the order they are shown.
 *
 * Supervisor hands the options back in `config.yaml` order, which is the
 * order they were added in — so grouping has to be declared here, and the
 * declaration is what puts the two that matter at the top.
 */
export const GROUPS: GroupMeta[] = [
  { title: GRID, blurb: "Where AstraMeter reads your grid power." },
  { title: DEVICE, blurb: "What your batteries think they are talking to." },
  {
    title: CONTROL,
    blurb:
      "How the load is shared out across ct002/ct003 batteries. The defaults " +
      "suit most homes.",
    advanced: true,
  },
  {
    title: READING,
    blurb: "How often the meter is read, and how the raw value is corrected.",
    advanced: true,
  },
  {
    title: FILTERS,
    blurb: "For a noisy or slow meter. All off unless you turn one on.",
    advanced: true,
  },
  {
    title: TUNING,
    blurb:
      "The ct002/ct003 control loop's own constants. Already tuned — change " +
      "one to chase a specific problem, not on spec.",
    advanced: true,
  },
  {
    title: CLOUD,
    blurb: "Marstek's own services. Local control needs none of this.",
    advanced: true,
  },
  {
    title: ADDON,
    blurb: "Dashboard access, logging, and where the add-on gets its settings.",
    advanced: true,
  },
  {
    title: OTHER_GROUP,
    blurb: "Options this dashboard has no description for yet.",
    advanced: true,
  },
];

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
  device_types: {
    label: "Emulated device",
    group: DEVICE,
    help:
      "Which meter AstraMeter pretends to be. ct002 or ct003 for Marstek " +
      "batteries — the only types it can steer several of; a Shelly type " +
      "otherwise. Comma-separated to emulate more than one.",
  },
  ct_mac: {
    label: "CT MAC address",
    group: DEVICE,
    help:
      "Answer only batteries that ask for this CT. Left empty AstraMeter " +
      "answers whoever asks, which is what almost everyone wants.",
    placeholder: "answer any battery",
  },

  // ── battery control ──
  active_control: {
    label: "Active control",
    group: CONTROL,
    help:
      "Work out each battery's own target instead of relaying the raw grid " +
      "total and letting every battery react to it separately.",
  },
  fair_distribution: {
    label: "Share load between batteries",
    group: CONTROL,
    help: "Even the load out across batteries. Only matters with two or more.",
  },
  grid_predict_trust: {
    label: "Grid prediction trust",
    group: CONTROL,
    help:
      "How far to trust AstraMeter's model of what the grid is about to do, " +
      "which is what absorbs a meter that reports late. 0 steers on the raw " +
      "reading only; 1 on the model alone.",
    placeholder: "0.5",
  },
  min_efficient_power: {
    label: "Minimum efficient power (W)",
    group: CONTROL,
    help:
      "Idle spare batteries until the working ones each carry at least this " +
      "much, rather than running them all at a trickle. 0 keeps them all on.",
    placeholder: "0",
  },
  efficiency_rotation_interval: {
    label: "Rotation interval (s)",
    group: CONTROL,
    help: "How often the batteries swap who takes the load, so wear stays even.",
    placeholder: "900",
  },
  min_dc_output: {
    label: "Minimum DC output (W)",
    group: CONTROL,
    help:
      "Keep a DC-only battery discharging at least this much so its inverter " +
      "does not shut off at 0 W and fall asleep under heavy solar. 20 or more " +
      "if you need it; 0 is off.",
    placeholder: "0",
  },

  // ── meter reading ──
  throttle_interval: {
    label: "Throttle interval (s)",
    group: READING,
    help:
      "Shortest time between meter reads. 0 reads as fast as the batteries " +
      "poll, which suits a local meter; 2–3 suits a slow or cloud-backed one.",
    placeholder: "0",
  },
  wait_for_next_message: {
    label: "Wait for a fresh reading",
    group: READING,
    help:
      "Wait up to 2 seconds for a new value before answering a battery. Turn " +
      "it off if your sensor updates more slowly than that, so replies stay " +
      "prompt on the last known value.",
  },
  dedupe_time_window: {
    label: "Dedupe window (s)",
    group: READING,
    help: "Ignore a repeat poll from the same battery within this long. 0 is off.",
    placeholder: "0",
  },
  power_offset: {
    label: "Power offset (W)",
    group: READING,
    help:
      "Added to every reading. A small negative value keeps a little import " +
      "in hand so you never export by accident. One value, or one per phase.",
    placeholder: "0",
  },
  power_multiplier: {
    label: "Power multiplier",
    group: READING,
    // The upgrade trap: a kW sensor used to need 1000 here, and since units
    // are converted automatically that same 1000 now scales it twice.
    help:
      "Every reading is multiplied by this. A kW sensor is converted to watts " +
      "on its own — if you set 1000 to work around that, remove it or the " +
      "reading is scaled twice. Use −1 to flip a backwards CT.",
    placeholder: "1",
  },

  // ── signal filters ──
  smooth_target_alpha: {
    label: "Target smoothing",
    group: FILTERS,
    help:
      "Smooths the reading before it is acted on: higher follows the meter " +
      "faster, lower filters more noise. 0 is off.",
    placeholder: "0",
  },
  max_smooth_step: {
    label: "Max smoothing step (W)",
    group: FILTERS,
    help: "Caps how far the smoothed value may move per reading. 0 is no limit.",
    placeholder: "0",
  },
  deadband: {
    label: "Deadband (W)",
    group: FILTERS,
    help:
      "Report a flat 0 W whenever the grid is within this much of zero, so " +
      "the batteries stop hunting around it. 10–30 is sensible; 0 is off.",
    placeholder: "0",
  },
  hampel_window: {
    label: "Spike filter window",
    group: FILTERS,
    help:
      "Rejects one-off nonsense readings from a flaky source by comparing " +
      "against this many recent ones. 0 is off; 5–7 works well.",
    placeholder: "0",
  },
  hampel_n_sigma: {
    label: "Spike filter sigma",
    group: FILTERS,
    help: "How far from the recent median counts as a spike. Lower rejects more.",
    placeholder: "3",
  },
  hampel_min_threshold: {
    label: "Spike filter threshold (W)",
    group: FILTERS,
    help:
      "Floor under that threshold, so a long flat stretch does not leave the " +
      "filter rejecting every small change. Around 50 is a good start.",
    placeholder: "0",
  },
  pid_kp: {
    label: "PID proportional",
    group: FILTERS,
    help:
      "Layers a net-zero PID controller on top of the meter reading. 0 is " +
      "off; 0.5 is a safe start with a single battery.",
    placeholder: "0",
  },
  pid_ki: {
    label: "PID integral",
    group: FILTERS,
    help: "Integral gain. Usually left at 0 — it risks winding up.",
    placeholder: "0",
  },
  pid_kd: {
    label: "PID derivative",
    group: FILTERS,
    help: "Derivative gain. Usually left at 0 — it is noisy on a real meter.",
    placeholder: "0",
  },
  pid_output_max: {
    label: "PID output max (W)",
    group: FILTERS,
    help: "Caps what the PID may add or subtract, in watts.",
    placeholder: "800",
  },
  pid_mode: {
    label: "PID mode",
    group: FILTERS,
    help:
      "bias adds the PID's output to the meter reading (recommended); " +
      "replace steers on the PID output alone.",
  },

  // ── balancer tuning ──
  balance_gain: {
    label: "Balance gain",
    group: TUNING,
    help:
      "How hard an imbalance between batteries is corrected. 0 splits the " +
      "load evenly and corrects nothing after that.",
    placeholder: "0.2",
  },
  balance_deadband: {
    label: "Balance deadband (W)",
    group: TUNING,
    help:
      "Leave an imbalance smaller than this alone. Kept above the batteries' " +
      "own ~20 W deadband, below which they ignore a correction anyway.",
    placeholder: "25",
  },
  max_correction_per_step: {
    label: "Max correction per step (W)",
    group: TUNING,
    help: "Cap on how much of an imbalance is corrected in one cycle.",
    placeholder: "80",
  },
  error_boost_threshold: {
    label: "Error boost threshold (W)",
    group: TUNING,
    help: "Above this imbalance the balancing gain is boosted.",
    placeholder: "150",
  },
  error_boost_max: {
    label: "Error boost max",
    group: TUNING,
    help: "How much extra gain that boost may add.",
    placeholder: "0.5",
  },
  error_reduce_threshold: {
    label: "Error reduce threshold (W)",
    group: TUNING,
    help: "Below this imbalance the gain is scaled back down again.",
    placeholder: "20",
  },
  max_target_step: {
    label: "Max target step (W)",
    group: TUNING,
    help: "Hard clamp on how far a battery's target may move per cycle. 0 is off.",
    placeholder: "0",
  },
  pace_base_step: {
    label: "Pace base step (W)",
    group: TUNING,
    help:
      "Starting cap on how far a battery's command may move per poll. It " +
      "grows toward the max step only while the battery is seen following, " +
      "which stops its own ramp overshooting on a late meter. 0 is off.",
    placeholder: "30",
  },
  pace_max_step: {
    label: "Pace max step (W)",
    group: TUNING,
    help: "Ceiling that cap grows to once a battery is tracking its command.",
    placeholder: "100",
  },
  osc_damp_max: {
    label: "Oscillation damping max",
    group: TUNING,
    help:
      "How far to cut the gain while a battery hunts — keeps reversing " +
      "direction — on a laggy meter. 0 is off.",
    placeholder: "0.95",
  },
  osc_damp_alpha: {
    label: "Oscillation damping alpha",
    group: TUNING,
    help: "How quickly hunting is recognised from repeated reversals.",
    placeholder: "0.3",
  },
  osc_damp_decay: {
    label: "Oscillation damping decay",
    group: TUNING,
    help: "How quickly damping relaxes once the hunting stops.",
    placeholder: "0.05",
  },
  osc_damp_threshold: {
    label: "Oscillation threshold (W)",
    group: TUNING,
    help:
      "A correction bigger than this is a real load step — a kettle, cloud " +
      "over the panels — and is never damped.",
    placeholder: "300",
  },
  concentrate_deadband: {
    label: "Concentrate deadband (W)",
    group: TUNING,
    help:
      "With several batteries: while the grid is within this much of zero, " +
      "hand the whole correction to one of them instead of splitting it into " +
      "shares each battery is too coarse to act on. 0 is off.",
    placeholder: "60",
  },
  import_trim_w: {
    label: "Import trim (W)",
    group: TUNING,
    help:
      "Trims away the last few watts of import a battery's own deadband " +
      "leaves behind, once the grid has been steady for a while. 0 is off.",
    placeholder: "15",
  },

  // ── Marstek cloud ──
  marstek_auto_register_ct_device: {
    label: "Register a CT with Marstek",
    group: CLOUD,
    help:
      "Register this emulated CT with your Marstek account once, so their app " +
      "shows it like a real one. Needs the e-mail and password below.",
  },
  marstek_mailbox: {
    label: "Marstek account e-mail",
    group: CLOUD,
    help: "The account the CT is registered to.",
  },
  marstek_password: {
    label: "Marstek password",
    group: CLOUD,
    help:
      "That account's password. Stored by the add-on and never sent back to " +
      "this page.",
  },
  cloud_reporting: {
    label: "Report to the Marstek cloud",
    group: CLOUD,
    help:
      "Also send your readings to Marstek, the way a real CT does. Off unless " +
      "you want their app to show them; local control does not need it.",
  },
  cloud_reporting_host: {
    label: "Cloud host",
    group: CLOUD,
    help: "Which Marstek endpoint those reports go to.",
    placeholder: "eu.hamedata.com",
  },
  cloud_reporting_interval: {
    label: "Cloud interval (s)",
    group: CLOUD,
    help: "How often a report is sent.",
    placeholder: "60",
  },

  // ── add-on and dashboard ──
  dashboard_allow_write: {
    label: "Allow changes from the dashboard",
    group: ADDON,
    help:
      "Lets this dashboard edit your configuration and control batteries. " +
      "Turn it off to make the whole dashboard read-only.",
  },
  dashboard_direct_access: {
    label: "Allow access outside Home Assistant",
    group: ADDON,
    help:
      "Also serves the dashboard on port 52500 with no login at all — Home " +
      "Assistant's own does not cover it. Trusted networks only.",
  },
  custom_config: {
    label: "Custom config file",
    group: ADDON,
    help:
      "Filename of a config.ini in /config to read instead of these options. " +
      "Migrating below fills this in for you.",
    placeholder: "use these options",
  },
  log_level: {
    label: "Log level",
    group: ADDON,
    help: "How much the add-on writes to its log. debug while chasing a problem.",
  },
  mqtt_uri: {
    label: "MQTT broker URL",
    group: ADDON,
    help:
      "An external broker for MQTT Insights, e.g. mqtt://user:pass@host:1883. " +
      "Empty uses Home Assistant's own.",
    placeholder: "use Home Assistant's broker",
  },
};
