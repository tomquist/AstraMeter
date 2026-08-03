// The Configuration tab.
//
// Two surfaces, chosen by what the backend says it supports:
//   ha_simple    — a guided form rendered from the add-on's own option
//                  schema, saved through the Supervisor API
//   ha_advanced  — the raw config.ini editor, saved to /config/<name>
//   standalone   — the raw config.ini editor, saved to the bind mount
//
// The guided form is rendered from `data.schema` returned by Supervisor
// rather than from a mapping table of our own, so a new add-on option shows
// up here the moment config.yaml has it — unlabelled but usable — instead of
// silently going missing.

import { h, type VChild, type VNode } from "./vdom.js";
import type { AppState } from "./model.js";
import { OPTION_META, type OptionMeta, type OptionSpec } from "./option-meta.js";
import type { HaEntity } from "./transport.js";

/** Type metadata for one INI key, from GET /api/key-types. */
export interface KeySpec {
  type?: "boolean" | "integer" | "float" | "password" | "select" | "string";
  options?: string[];
  min?: number;
  max?: number;
}

export interface ConfigState {
  loading: boolean;
  /** Mode the currently held data was loaded for; null when never loaded. */
  loadedMode: string | null;
  /** Add-on options, as edited. */
  options: Record<string, unknown>;
  /** Supervisor's own schema, per option, as `normalizeAddonSchema` reads it
   *  off the wire — Supervisor's shape there is a list of field descriptors,
   *  not a mapping, and normalizing at the boundary keeps that out of here. */
  schema: Record<string, OptionSpec>;
  /** config.ini as a structured document, as edited. */
  sections: Record<string, Record<string, string>>;
  order: string[];
  keyTypes: Record<string, Record<string, KeySpec>>;
  /** Home Assistant power sensors, for the entity pickers. */
  entities: HaEntity[];
  entitiesLoaded: boolean;
  iniLoaded: boolean;
  dirty: boolean;
  saving: boolean;
  message: string | null;
  error: string | null;
}

export interface ConfigActions {
  loadConfig(): void;
  editOption(key: string, value: unknown): void;
  setKey(section: string, key: string, value: string): void;
  renameKey(section: string, from: string, to: string): void;
  removeKey(section: string, key: string): void;
  addKey(section: string): void;
  addSection(): void;
  removeSection(section: string): void;
  saveConfig(restart: boolean): void;
  switchMode(mode: "file" | "options"): void;
  restart(): void;
}

export function initialConfigState(): ConfigState {
  return {
    loading: false,
    loadedMode: null,
    options: {},
    schema: {},
    sections: {},
    order: [],
    keyTypes: {},
    entities: [],
    entitiesLoaded: false,
    iniLoaded: false,
    dirty: false,
    saving: false,
    message: null,
    error: null,
  };
}

/**
 * Type metadata for a key, matching a section by prefix.
 *
 * Sections can carry a suffix for multiple meters of one kind
 * (`[SHELLY_2]`), and the CT003 section reuses the CT002 table, so the
 * longest matching prefix wins rather than an exact name.
 */
export function specFor(
  keyTypes: Record<string, Record<string, KeySpec>>,
  section: string,
  key: string,
): KeySpec {
  const wanted = (section || "").toUpperCase();
  const prefixes = Object.keys(keyTypes).sort((a, b) => b.length - a.length);
  for (const prefix of prefixes) {
    if (wanted === prefix || wanted.startsWith(prefix + "_")) {
      return keyTypes[prefix][(key || "").toUpperCase()] || {};
    }
  }
  return {};
}

/** Every key the backend knows for a section, for the add-key suggestions. */
export function knownKeys(
  keyTypes: Record<string, Record<string, KeySpec>>,
  section: string,
): string[] {
  const wanted = (section || "").toUpperCase();
  const prefixes = Object.keys(keyTypes).sort((a, b) => b.length - a.length);
  for (const prefix of prefixes) {
    if (wanted === prefix || wanted.startsWith(prefix + "_")) {
      return Object.keys(keyTypes[prefix]);
    }
  }
  return [];
}

// Mirrors the backend's secret-key rule (status/secrets.py). Neither the INI
// type table nor Supervisor's schema marks every one of these, but the
// *backend* redacts by key name anywhere — so without this a password would
// be sent back as bullets in a plain, visible text box.
const SECRET_KEY = /(password|passwd|secret|token|api[_-]?key|accesstoken|mailbox)/i;

function card(title: string | null, ...body: VChild[]): VNode {
  return h("section", { class: "card" }, title ? h("h2", null, title) : null, ...body);
}

export function configView(
  state: AppState,
  config: ConfigState,
  actions: ConfigActions,
): VChild[] {
  const caps = state.snapshot?.capabilities;
  const mode = caps?.config_mode ?? "standalone";
  // The mode switch writes, so a read-only dashboard must not offer it: the
  // backend refuses the call and the user gets an error for a button we drew.
  const cards: VChild[] = [
    modeCard(mode, Boolean(caps?.ha_options && caps?.controls), actions),
  ];

  if (config.error) {
    cards.unshift(h("div", { class: "banner err" }, config.error));
  }
  if (config.message) {
    cards.unshift(h("div", { class: "banner info" }, config.message));
  }
  if (!caps?.controls) {
    cards.push(
      h(
        "div",
        { class: "banner warn" },
        "This dashboard is read-only. Turn on the add-on's " +
          "“Allow changes from the dashboard” option to edit your configuration here.",
      ),
    );
    return cards;
  }

  if (mode === "ha_simple") {
    cards.push(guidedForm(config, actions));
  } else {
    cards.push(...iniEditor(config, actions), ...keyDatalists(config));
  }
  return cards;
}

function modeCard(
  mode: string,
  haOptions: boolean,
  actions: ConfigActions,
): VNode {
  const isSimple = mode === "ha_simple";
  const isAddon = mode.startsWith("ha_");
  return card(
    "Configuration mode",
    h(
      "p",
      { style: "margin:0 0 10px" },
      isSimple
        ? "Guided setup — AstraMeter is configured from the add-on options, " +
            "and rewrites its config file on every start."
        : isAddon
          ? "Config file — AstraMeter reads a config.ini you control, and the " +
              "add-on options are ignored."
          : "Config file — AstraMeter reads the config.ini mounted into this container.",
    ),
    isAddon && haOptions
      ? h(
          "div",
          { style: "display:flex;gap:8px;flex-wrap:wrap;align-items:center" },
          h(
            "button",
            {
              class: "btn sm",
              onclick: () => actions.switchMode(isSimple ? "file" : "options"),
            },
            isSimple ? "Switch to a config file" : "Switch to guided setup",
          ),
          h(
            "span",
            { class: "help", style: "color:var(--text-faint);font-size:.75rem" },
            isSimple
              ? "Copies what is running now into /config/astrameter.ini, then restarts."
              : "Goes back to the add-on options. Your config file is kept, not deleted.",
          ),
        )
      : null,
  );
}

// ── guided form ─────────────────────────────────────────────────────

function guidedForm(config: ConfigState, actions: ConfigActions): VNode {
  if (config.loading) {
    return card(null, h("div", { class: "empty" }, "Loading add-on options…"));
  }
  const keys = Object.keys(config.schema);
  if (!keys.length) {
    return card(
      null,
      h(
        "div",
        { class: "empty" },
        h("strong", null, "Add-on options unavailable"),
        "AstraMeter could not reach the Home Assistant Supervisor.",
      ),
    );
  }

  const groups = new Map<string, string[]>();
  for (const key of keys) {
    const group = OPTION_META[key]?.group ?? "Other";
    if (!groups.has(group)) groups.set(group, []);
    groups.get(group)!.push(key);
  }

  const sections: VChild[] = [];
  for (const [group, groupKeys] of groups) {
    sections.push(
      h(
        "div",
        { style: "margin-bottom:18px" },
        h(
          "h3",
          { style: "font-size:.85rem;margin:0 0 8px;color:var(--text-dim)" },
          group,
        ),
        h(
          "div",
          { class: "fields" },
          ...groupKeys.map((key) =>
            optionField(key, config.schema[key], config.options[key], config, actions),
          ),
        ),
      ),
    );
  }

  return card(
    "Guided setup",
    ...sections,
    actionBar(config, actions, "supervisor"),
  );
}

function optionField(
  key: string,
  spec: OptionSpec,
  value: unknown,
  config: ConfigState,
  actions: ConfigActions,
): VNode {
  const meta = OPTION_META[key];
  const label = meta?.label ?? titleCase(key);

  // A repeated or nested option. Editing it here would write a string back
  // over a list, so it is shown and left to the add-on's own Configuration
  // page — which does understand the shape.
  if (spec.unsupported) {
    return h(
      "label",
      { class: "field" },
      h("span", { class: "name" }, label),
      h("input", {
        type: "text",
        value: value == null ? "" : JSON.stringify(value),
        readonly: true,
        disabled: true,
      }),
      h(
        "span",
        { class: "help" },
        `Type ${spec.unsupported} — edit this one on the add-on's ` +
          "Configuration page.",
      ),
    );
  }

  if (meta?.entity) return entityField(key, label, value, meta, config, actions);

  if (spec.type === "bool") {
    return h(
      "label",
      { class: "row" },
      h("input", {
        type: "checkbox",
        checked: Boolean(value),
        onchange: (e: Event) =>
          actions.editOption(key, (e.target as HTMLInputElement).checked),
      }),
      h("span", null, label),
    );
  }

  return h(
    "label",
    { class: "field" },
    h("span", { class: "name" }, label),
    spec.type === "list"
      ? h(
          "select",
          {
            onchange: (e: Event) =>
              actions.editOption(key, (e.target as HTMLSelectElement).value),
          },
          ...(spec.options || []).map((opt) =>
            h("option", { value: opt, selected: String(value) === opt }, opt),
          ),
        )
      : h("input", {
          type: inputType(spec, key),
          value: value == null ? "" : String(value),
          min: spec.min,
          max: spec.max,
          step: spec.type === "float" ? "any" : undefined,
          placeholder: spec.optional ? "optional" : "",
          oninput: (e: Event) =>
            actions.editOption(key, coerce(spec, (e.target as HTMLInputElement).value)),
        }),
    meta?.help ? h("span", { class: "help" }, meta.help) : null,
  );
}

/**
 * How many sensors one of these options holds: one per phase.
 *
 * The option is a comma-separated list — one entity for a whole-house total,
 * or one per phase for a three-phase meter (`config/addon.py::_entities`).
 */
const MAX_PHASES = 3;

/**
 * The entity ids in a stored option, by position.
 *
 * Blanks in the middle are kept: they are a row the user has just cleared,
 * and dropping one here would slide every later phase up under their cursor.
 * A trailing blank is dropped — that is only ever the empty row this control
 * offers for the next phase.
 */
export function entityList(value: unknown): string[] {
  const parts = String(value ?? "")
    .split(",")
    .map((part) => part.trim());
  while (parts.length && !parts[parts.length - 1]) parts.pop();
  return parts;
}

/**
 * A Home Assistant entity picker: searchable comboboxes listing only sensors
 * that could plausibly carry grid power, one per phase.
 *
 * A native `<input list=…>` rather than a custom dropdown: the browser gives
 * substring search, keyboard navigation and mobile behaviour for free, and a
 * hand-typed id still works — which matters because the list is best-effort
 * and an entity can be missing when Home Assistant is still starting.
 *
 * One row per configured sensor, plus an empty one while under the cap — so a
 * three-phase meter can be entered here at all, and so the fact that it takes
 * more than one sensor is visible rather than hidden in a comma-separated
 * string a picker would overwrite on the next selection.
 */
function entityField(
  key: string,
  label: string,
  value: unknown,
  meta: OptionMeta,
  config: ConfigState,
  actions: ConfigActions,
): VNode {
  const entities = entityList(value);
  const rows = entities.length < MAX_PHASES ? [...entities, ""] : entities;
  const perPhase = entities.length > 1;
  const listId = `ha-entities-${key}`;

  // Positions are held while typing (see entityList) and compacted on blur,
  // so clearing a row cannot pull the next phase up mid-edit.
  const write = (index: number, next: string, compact: boolean) => {
    const merged = rows.map((entity, i) => (i === index ? next.trim() : entity));
    actions.editOption(
      key,
      (compact ? merged.filter(Boolean) : merged).join(", ").trimEnd(),
    );
  };

  return h(
    "div",
    { class: "field entity-field" },
    h("span", { class: "name" }, label),
    ...rows.map((entity, index) =>
      entityRow(entity, index, {
        key,
        label,
        listId,
        perPhase,
        rows,
        config,
        write,
        actions,
      }),
    ),
    h(
      "datalist",
      { id: listId },
      ...config.entities.map((entity) =>
        h(
          "option",
          { value: entity.entity_id },
          // The friendly name and live reading are what a user actually
          // recognises; the id alone is often unreadable.
          [entity.name, entity.state != null ? `${entity.state} ${entity.unit || ""}`.trim() : null]
            .filter(Boolean)
            .join(" · "),
        ),
      ),
    ),
    phaseMismatch(entities, meta, config),
    meta.help ? h("span", { class: "help" }, meta.help) : null,
    config.entitiesLoaded && config.entities.length === 0
      ? h("span", { class: "help" }, "No power sensors found — type an entity id.")
      : null,
  );
}

/**
 * Warn when two paired entity options hold a different number of phases.
 *
 * Import and export are zipped per phase, so a mismatch is rejected at
 * start-up — which is a crash on the next restart rather than a message on
 * the page that caused it. Only ever a warning: the other option may simply
 * not be filled in yet.
 */
function phaseMismatch(
  entities: string[],
  meta: OptionMeta,
  config: ConfigState,
): VChild {
  if (!meta.entityPeer || !entities.length) return null;
  const peer = entityList(config.options[meta.entityPeer]);
  if (!peer.length || peer.length === entities.length) return null;
  const peerLabel = OPTION_META[meta.entityPeer]?.label ?? meta.entityPeer;
  return h(
    "span",
    { class: "help warn-text" },
    `${entities.length} sensor${entities.length === 1 ? "" : "s"} here against ` +
      `${peer.length} for ${peerLabel} — they are paired per phase, so the ` +
      "counts have to match.",
  );
}

interface RowContext {
  key: string;
  label: string;
  listId: string;
  perPhase: boolean;
  rows: string[];
  config: ConfigState;
  write(index: number, next: string, compact: boolean): void;
  actions: ConfigActions;
}

/** One phase's combobox, with what it currently resolves to underneath. */
function entityRow(entity: string, index: number, ctx: RowContext): VNode {
  const { config } = ctx;
  const match = config.entities.find((e) => e.entity_id === entity);
  // Say when a configured entity is not in the list: a typo here is the
  // single easiest way to misconfigure AstraMeter, and it otherwise only
  // surfaces as a start-up failure much later.
  const unknown = entity && config.entitiesLoaded && !match;
  // Named per phase only once there is more than one, so a single whole-house
  // sensor is not mislabelled as a phase of something.
  const name =
    ctx.perPhase || index > 0 ? `${ctx.label} phase ${index + 1}` : ctx.label;
  const removable = ctx.rows.filter(Boolean).length > 1 && Boolean(entity);

  return h(
    "div",
    { class: "entity-row" },
    h("input", {
      type: "text",
      class: unknown ? "warn-input" : false,
      value: entity,
      list: ctx.listId,
      spellcheck: "false",
      autocomplete: "off",
      placeholder: index > 0
        ? `Another sensor for phase ${index + 1} — optional`
        : config.entitiesLoaded
          ? "Search sensors — type to filter"
          : "sensor.your_grid_power",
      "aria-label": name,
      oninput: (e: Event) => ctx.write(index, (e.target as HTMLInputElement).value, false),
      // Blur is the safe moment to close a gap: the input the user is in is
      // never rewritten by a repaint (see UNCONTROLLED in vdom.ts), so
      // compacting while it has focus would leave the DOM and the value
      // disagreeing about which row holds what.
      onchange: (e: Event) => ctx.write(index, (e.target as HTMLInputElement).value, true),
    }),
    removable
      ? h(
          "button",
          {
            class: "btn sm iconbtn",
            title: `Remove ${name}`,
            "aria-label": `Remove ${name}`,
            onclick: () => ctx.write(index, "", true),
          },
          "✕",
        )
      : null,
    match
      ? h(
          "span",
          { class: "help" },
          `${match.name}${match.state != null ? ` — currently ${match.state} ${match.unit || ""}`.trimEnd() : ""}`,
        )
      : unknown
        ? h("span", { class: "help warn-text" }, "Not found in Home Assistant right now.")
        : null,
  );
}

function inputType(spec: OptionSpec, key: string): string {
  // The backend replaces a secret with bullets by key name, so anything it
  // redacts has to be masked here too — otherwise a Supervisor that describes
  // a password as a plain string (no `format`) puts the bullets in an open
  // text box, and the field stops reading as a credential at all.
  if (spec.type === "password" || (spec.type === "str" && SECRET_KEY.test(key))) {
    return "password";
  }
  if (spec.type === "int" || spec.type === "float" || spec.type === "port") {
    return "number";
  }
  return "text";
}

function coerce(spec: OptionSpec, raw: string): unknown {
  if (raw === "") return "";
  if (spec.type === "int" || spec.type === "port") {
    const parsed = parseInt(raw, 10);
    return Number.isNaN(parsed) ? raw : parsed;
  }
  if (spec.type === "float") {
    const parsed = parseFloat(raw);
    return Number.isNaN(parsed) ? raw : parsed;
  }
  return raw;
}

function titleCase(key: string): string {
  return key
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}

// ── raw config.ini ──────────────────────────────────────────────────

/**
 * The structured `config.ini` editor: one collapsible card per section, a
 * typed control per key, and add/remove for both.
 *
 * This is the same shape as the standalone editor at `/config`, rendered
 * from the same `SECTION_KEY_TYPES` metadata the backend already serves, so
 * a user does not meet two different editors for one file.
 */
function iniEditor(config: ConfigState, actions: ConfigActions): VChild[] {
  if (!config.iniLoaded) {
    return [card(null, h("div", { class: "empty" }, "Loading config.ini…"))];
  }
  const names = config.order.filter((name) => config.sections[name]);
  if (!names.length) {
    return [
      card(
        null,
        h(
          "div",
          { class: "empty" },
          h("strong", null, "This config file is empty"),
          "Add a section to get started.",
        ),
        h(
          "div",
          { style: "text-align:center" },
          h(
            "button",
            { class: "btn sm", onclick: () => actions.addSection() },
            "+ Add section",
          ),
        ),
      ),
    ];
  }

  return [
    h(
      "p",
      { class: "hint" },
      "Passwords and tokens are shown as ",
      h("code", null, "••••••••"),
      ". Leave them as-is to keep the stored value.",
    ),
    ...names.map((name) => sectionCard(name, config, actions)),
    h(
      "div",
      { style: "display:flex;gap:8px" },
      h(
        "button",
        { class: "btn sm", onclick: () => actions.addSection() },
        "+ Add section",
      ),
    ),
    actionBar(config, actions, "process"),
  ];
}

function sectionCard(
  name: string,
  config: ConfigState,
  actions: ConfigActions,
): VNode {
  const pairs = config.sections[name] || {};
  const keys = Object.keys(pairs);
  return h(
    "details",
    // Open by default on first render; the user's later toggling is theirs
    // to keep (see UNCONTROLLED in vdom.ts).
    { class: "card section", open: true },
    h(
      "summary",
      null,
      h("span", { class: "sec-name" }, `[${name}]`),
      h("span", { class: "sec-count" }, `${keys.length} setting${keys.length === 1 ? "" : "s"}`),
    ),
    keys.length
      ? h(
          "div",
          { class: "keyrows" },
          ...keys.map((key) => keyRow(name, key, pairs[key], config, actions)),
        )
      : h("p", { class: "hint" }, "No settings in this section yet."),
    h(
      "div",
      { class: "sec-actions" },
      h(
        "button",
        { class: "btn sm", onclick: () => actions.addKey(name) },
        "+ Add setting",
      ),
      h(
        "button",
        {
          class: "btn sm danger",
          onclick: () => actions.removeSection(name),
        },
        "Remove section",
      ),
    ),
  );
}

function keyRow(
  section: string,
  key: string,
  value: string,
  config: ConfigState,
  actions: ConfigActions,
): VNode {
  const spec = specFor(config.keyTypes, section, key);
  const listId = `keys-${section}`.replace(/[^A-Za-z0-9-]/g, "_");
  return h(
    "div",
    { class: "keyrow" },
    // The key is an editable combobox so a known setting can be picked from
    // the list while an unrecognised one can still be typed by hand.
    h("input", {
      class: "keyname",
      value: key,
      list: listId,
      spellcheck: "false",
      // Names the row's own key, not just its section: a screen-reader user
      // tabbing through would otherwise hear the same label for every row.
      "aria-label": `Setting name: ${key}`,
      onchange: (e: Event) =>
        actions.renameKey(section, key, (e.target as HTMLInputElement).value),
    }),
    valueControl(section, key, value, spec, actions),
    h(
      "button",
      {
        class: "btn sm iconbtn",
        title: `Remove ${key}`,
        "aria-label": `Remove ${key}`,
        onclick: () => actions.removeKey(section, key),
      },
      "✕",
    ),
  );
}

function valueControl(
  section: string,
  key: string,
  value: string,
  spec: KeySpec,
  actions: ConfigActions,
): VNode {
  const set = (v: string) => actions.setKey(section, key, v);

  if (spec.type === "boolean") {
    // Written back as True/False: that is what configparser's getboolean
    // round-trips and what the rest of the file already uses.
    const on = ["true", "yes", "on", "1"].includes(String(value).toLowerCase());
    return h(
      "select",
      {
        class: "keyval",
        "aria-label": `${key} value`,
        onchange: (e: Event) => set((e.target as HTMLSelectElement).value),
      },
      h("option", { value: "True", selected: on }, "True"),
      h("option", { value: "False", selected: !on }, "False"),
    );
  }

  if (spec.type === "select") {
    const options = spec.options || [];
    const unknown = value && !options.includes(value);
    return h(
      "select",
      {
        class: "keyval",
        "aria-label": `${key} value`,
        onchange: (e: Event) => set((e.target as HTMLSelectElement).value),
      },
      // Keep an unrecognised stored value selectable so opening the editor
      // cannot silently rewrite it to the first option.
      unknown ? h("option", { value, selected: true }, `${value} (current)`) : null,
      ...options.map((opt) =>
        h("option", { value: opt, selected: value === opt }, opt),
      ),
    );
  }

  if (spec.type === "integer" || spec.type === "float") {
    return h("input", {
      class: "keyval",
      type: "number",
      step: spec.type === "float" ? "any" : "1",
      min: spec.min,
      max: spec.max,
      value,
      "aria-label": `${key} value`,
      oninput: (e: Event) => set((e.target as HTMLInputElement).value),
    });
  }

  if (spec.type === "password" || (!spec.type && SECRET_KEY.test(key))) {
    return h("input", {
      class: "keyval",
      type: "password",
      value,
      autocomplete: "off",
      "aria-label": `${key} value`,
      oninput: (e: Event) => set((e.target as HTMLInputElement).value),
    });
  }

  return h("input", {
    class: "keyval",
    type: "text",
    value,
    spellcheck: "false",
    "aria-label": `${key} value`,
    oninput: (e: Event) => set((e.target as HTMLInputElement).value),
  });
}

/** One datalist per section, so the key comboboxes can suggest known keys. */
function keyDatalists(config: ConfigState): VChild[] {
  return config.order
    .filter((name) => config.sections[name])
    .map((name) =>
      h(
        "datalist",
        { id: `keys-${name}`.replace(/[^A-Za-z0-9-]/g, "_") },
        ...knownKeys(config.keyTypes, name).map((key) => h("option", { value: key })),
      ),
    );
}

function actionBar(
  config: ConfigState,
  actions: ConfigActions,
  tier: "process" | "supervisor",
): VNode {
  return h(
    "div",
    {
      style:
        "display:flex;gap:10px;align-items:center;margin-top:14px;" +
        "padding-top:12px;border-top:1px solid var(--divider)",
    },
    h(
      "button",
      {
        class: "btn primary",
        disabled: !config.dirty || config.saving,
        onclick: () => actions.saveConfig(true),
      },
      config.saving ? "Saving…" : "Save and restart",
    ),
    h(
      "button",
      {
        class: "btn",
        disabled: !config.dirty || config.saving,
        onclick: () => actions.saveConfig(false),
      },
      "Save only",
    ),
    h(
      "span",
      { style: "color:var(--text-dim);font-size:.78rem" },
      config.dirty
        ? tier === "supervisor"
          ? "Saving restarts the add-on — this can take a minute."
          : "Saving reloads AstraMeter; the dashboard stays up."
        : "No unsaved changes.",
    ),
  );
}
