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
import { OPTION_META, parseAddonSchema, type OptionSpec } from "./option-meta.js";

export interface ConfigState {
  loading: boolean;
  /** Add-on options, as edited. */
  options: Record<string, unknown>;
  schema: Record<string, string>;
  /** config.ini, as edited. */
  iniText: string;
  iniLoaded: boolean;
  dirty: boolean;
  saving: boolean;
  message: string | null;
  error: string | null;
}

export interface ConfigActions {
  loadConfig(): void;
  editOption(key: string, value: unknown): void;
  editIni(text: string): void;
  saveConfig(restart: boolean): void;
  switchMode(mode: "file" | "options"): void;
  restart(): void;
}

export function initialConfigState(): ConfigState {
  return {
    loading: false,
    options: {},
    schema: {},
    iniText: "",
    iniLoaded: false,
    dirty: false,
    saving: false,
    message: null,
    error: null,
  };
}

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
  const cards: VChild[] = [modeCard(mode, caps?.ha_options ?? false, actions)];

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
    cards.push(iniEditor(config, actions));
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
            optionField(key, config.schema[key], config.options[key], actions),
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
  rawSpec: string,
  value: unknown,
  actions: ConfigActions,
): VNode {
  const spec = parseAddonSchema(rawSpec);
  const meta = OPTION_META[key];
  const label = meta?.label ?? titleCase(key);

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
          type: inputType(spec),
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

function inputType(spec: OptionSpec): string {
  if (spec.type === "password") return "password";
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

function iniEditor(config: ConfigState, actions: ConfigActions): VNode {
  if (!config.iniLoaded) {
    return card(null, h("div", { class: "empty" }, "Loading config.ini…"));
  }
  return card(
    "config.ini",
    h(
      "p",
      { style: "margin:0 0 10px;color:var(--text-dim);font-size:.8rem" },
      "Passwords and tokens are shown as ",
      h("code", null, "••••••••"),
      ". Leave them as-is to keep the stored value.",
    ),
    h("textarea", {
      spellcheck: "false",
      value: config.iniText,
      "aria-label": "Configuration file contents",
      oninput: (e: Event) =>
        actions.editIni((e.target as HTMLTextAreaElement).value),
    }),
    actionBar(config, actions, "process"),
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
