// Dashboard entry point: owns the poll loop, the action handlers and the
// single render call.

import { patch } from "./vdom.js";
import { PollTransport, TransportError, type Transport } from "./transport.js";
import {
  initialState,
  pollInterval,
  type AppState,
  type Tab,
} from "./model.js";
import { view, pageTitle, type Actions } from "./view.js";
import { initialConfigState, type ConfigState } from "./config-view.js";
import { normalizeAddonSchema } from "./option-meta.js";
import { recordSnapshot } from "./history.js";

const THEME_KEY = "astrameter.theme";
const THEMES = ["auto", "light", "dark"] as const;

let state: AppState = initialState();
let config: ConfigState = initialConfigState();
let transport: Transport = new PollTransport();
let root: HTMLElement;
let timer: number | undefined;

function applyTheme(theme: string): void {
  if (theme === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
}

function currentTheme(): string {
  try {
    return localStorage.getItem(THEME_KEY) || "auto";
  } catch {
    return "auto";
  }
}

const actions: Actions = {
  selectTab(tab: Tab) {
    location.hash = `#/${tab}`;
    setTab(tab);
  },

  toggleTheme() {
    const next = THEMES[(THEMES.indexOf(currentTheme() as any) + 1) % THEMES.length];
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      /* private mode — the choice just does not persist */
    }
    applyTheme(next);
  },

  async setConsumer(deviceId, consumerId, field, value) {
    const key = `${consumerId}:${field}`;
    state.busy[key] = true;
    // Hold the requested value until the server has confirmed it, so an
    // in-flight poll cannot re-render the control back to the old one.
    state.pending[key] = value;
    render();
    try {
      await transport.controlConsumer(deviceId, consumerId, field, value);
      await poll();
    } catch (err) {
      state.error = describe(err);
    } finally {
      delete state.busy[key];
      delete state.pending[key];
      render();
    }
  },

  async setDevice(deviceId, field, value) {
    const key = `${deviceId}:${field}`;
    state.busy[key] = true;
    if (field !== "force_rotation") state.pending[key] = value;
    render();
    try {
      await transport.controlDevice(deviceId, field, value);
      await poll();
    } catch (err) {
      state.error = describe(err);
    } finally {
      delete state.busy[key];
      delete state.pending[key];
      render();
    }
  },

  async loadConfig() {
    // Which surface to load depends on the mode, which only the first
    // snapshot tells us — so a deep link into this tab has to wait for it.
    // poll() calls back here once capabilities are known.
    const mode = state.snapshot?.capabilities?.config_mode;
    if (!mode) return;
    // Never refetch over unsaved edits, and never reload a surface already
    // loaded for this mode.
    if (config.loading || config.dirty || config.loadedMode === mode) return;
    config.loadedMode = mode;
    config.loading = true;
    config.error = null;
    render();
    try {
      if (mode === "ha_simple") {
        const data = await transport.getAddonOptions();
        config.options = data.options || {};
        config.schema = normalizeAddonSchema(data.schema);
        // Best-effort: a failed entity lookup leaves the picker as a plain
        // text box rather than blocking the whole form.
        try {
          config.entities = await transport.listPowerEntities();
        } catch {
          config.entities = [];
        }
        config.entitiesLoaded = true;
      } else {
        const [data, keyTypes] = await Promise.all([
          transport.getConfig(),
          transport.getKeyTypes(),
        ]);
        config.sections = data.sections || {};
        config.order = data.order || Object.keys(config.sections);
        config.keyTypes = keyTypes as ConfigState["keyTypes"];
        config.iniLoaded = true;
      }
      config.dirty = false;
    } catch (err) {
      config.error = describe(err);
      config.loadedMode = null; // let the user retry by re-entering the tab
    } finally {
      config.loading = false;
      render();
    }
  },

  editOption(key, value) {
    config.options[key] = value;
    config.dirty = true;
    config.message = null;
    render();
  },

  setKey(section, key, value) {
    const pairs = config.sections[section];
    if (!pairs) return;
    pairs[key] = value;
    touch();
  },

  renameKey(section, from, to) {
    const pairs = config.sections[section];
    const name = to.trim().toUpperCase();
    if (!pairs || !name || name === from) return;
    if (name in pairs) {
      config.error = `${section} already has a setting called ${name}.`;
      render();
      return;
    }
    // Rebuild so the renamed key keeps its position rather than jumping to
    // the end of the section.
    config.sections[section] = Object.fromEntries(
      Object.entries(pairs).map(([k, v]) => (k === from ? [name, v] : [k, v])),
    );
    touch();
  },

  removeKey(section, key) {
    const pairs = config.sections[section];
    if (!pairs) return;
    delete pairs[key];
    touch();
  },

  addKey(section) {
    const pairs = config.sections[section];
    if (!pairs) return;
    let name = "NEW_SETTING";
    for (let i = 2; name in pairs; i++) name = `NEW_SETTING_${i}`;
    pairs[name] = "";
    touch();
  },

  addSection() {
    let name = "NEW_SECTION";
    for (let i = 2; name in config.sections; i++) name = `NEW_SECTION_${i}`;
    config.sections[name] = {};
    config.order = [...config.order, name];
    touch();
  },

  removeSection(section) {
    delete config.sections[section];
    config.order = config.order.filter((s) => s !== section);
    touch();
  },

  async saveConfig(restart) {
    config.saving = true;
    config.error = null;
    config.message = null;
    render();
    try {
      const mode = state.snapshot?.capabilities?.config_mode;
      if (mode === "ha_simple") {
        await transport.saveAddonOptions(config.options, restart);
        config.message = restart
          ? "Saved. The add-on is restarting — this can take a minute."
          : "Saved. Restart the add-on to apply.";
      } else {
        await transport.saveConfig(config.sections, config.order);
        if (restart) await transport.restart();
        config.message = restart
          ? "Saved. AstraMeter is reloading."
          : "Saved. Restart to apply.";
      }
      config.dirty = false;
    } catch (err) {
      config.error = describe(err);
    } finally {
      config.saving = false;
      render();
    }
  },

  openEntityPicker(rowId) {
    if (config.openPicker === rowId) return;
    config.openPicker = rowId;
    // A fresh list starts with nothing highlighted, so Enter on typed text
    // keeps that text rather than taking whatever happened to be first.
    config.pickerIndex = -1;
    render();
  },

  moveEntityPicker(index, count) {
    // Absolute, not a delta on the stored value: typing filters the list, and
    // an index left pointing past the end of the new one is only clamped for
    // display — so the next arrow press would move a number nobody can see
    // and the highlight would sit still for several keys.
    config.pickerIndex = Math.max(-1, Math.min(index, count - 1));
    render();
  },

  askSwitchMode(mode) {
    config.confirmMode = mode;
    config.error = null;
    config.message = null;
    render();
  },

  async switchMode(mode) {
    config.saving = true;
    config.error = null;
    render();
    try {
      await transport.switchConfigMode(mode);
      config.confirmMode = null;
      config.message =
        "Configuration mode changed. The add-on is restarting — this can take a minute.";
      // The surface for the new mode is a different one, and the old mode's
      // data is now wrong. Drop it so the tab reloads once we are back.
      config.loadedMode = null;
      config.dirty = false;
    } catch (err) {
      config.error = describe(err);
    } finally {
      config.saving = false;
      render();
    }
  },

  async restart() {
    try {
      await transport.restart();
      config.message = "Restarting…";
    } catch (err) {
      config.error = describe(err);
    }
    render();
  },
};

/** Mark the config document edited and re-render. */
function touch(): void {
  config.dirty = true;
  config.message = null;
  config.error = null;
  render();
}

function describe(err: unknown): string {
  if (err instanceof TransportError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

// ── poll loop ───────────────────────────────────────────────────────

async function poll(): Promise<void> {
  try {
    const snapshot = await transport.fetchStatus();
    if (snapshot) {
      state.snapshot = snapshot;
      state.lastFrameAt = Date.now();
      // Only on a new revision: an unchanged one comes back as a 304 with no
      // body, and re-recording the last value would draw a flat line across
      // a stretch where nothing was actually measured.
      recordSnapshot(state.history, snapshot);
    }
    state.connection = "live";
    state.failures = 0;
    state.error = null;
    if (state.tab === "config") actions.loadConfig();
  } catch (err) {
    state.failures += 1;
    // One dropped poll is normal on a busy network; two is a real outage.
    if (state.failures >= 2) state.connection = "offline";
    if (err instanceof TransportError && err.status === 403) {
      state.error =
        "This dashboard is not reachable from here. Open it from the " +
        "Home Assistant sidebar.";
    }
  }
  render();
  schedule();
}

function schedule(): void {
  window.clearTimeout(timer);
  // A poll already in flight when the page was hidden still lands here and
  // would restart the loop behind a hidden iframe. visibilitychange arms it
  // again on the way back.
  if (document.visibilityState === "hidden") return;
  const base = pollInterval(state.snapshot);
  // Back off while offline so a down backend is not hammered, but stay
  // responsive enough that recovery feels immediate.
  const delay = state.connection === "offline" ? Math.min(base * 4, 10000) : base;
  timer = window.setTimeout(poll, delay);
}

function render(): void {
  root.dataset.conn = state.connection;
  try {
    patch(root, view(state, actions, config));
  } catch (err) {
    // A render that throws used to take the whole page with it: every later
    // poll died at the same place, so the last painted frame froze on screen
    // — including tabs that had nothing to do with the fault. One bad value
    // from Supervisor or a device must not cost the user their dashboard.
    renderFailure(err);
    return;
  }
  const title = pageTitle(state.snapshot);
  if (document.title !== title) document.title = title;
}

/** Last resort: hand-built DOM, so a bug in the view cannot recurse here. */
function renderFailure(err: unknown): void {
  console.error("dashboard render failed", err);
  root.textContent = "";
  const box = document.createElement("section");
  box.className = "card";
  const title = document.createElement("strong");
  title.textContent = "The dashboard could not draw this state";
  const detail = document.createElement("p");
  detail.className = "hint";
  detail.textContent =
    `${err instanceof Error ? err.message : String(err)} — reload to retry. ` +
    "Please report this with the message above.";
  box.append(title, detail);
  root.append(box);
}

function routeFromHash(): Tab {
  const hash = location.hash.replace(/^#\/?/, "");
  const tabs: Tab[] = ["overview", "batteries", "sources", "config", "diagnostics"];
  return tabs.includes(hash as Tab) ? (hash as Tab) : "overview";
}

/**
 * The single place a tab becomes active.
 *
 * Every route into the Configuration tab — click, deep link, back button —
 * has to trigger its load, so this must not be duplicated per entry point.
 */
function setTab(tab: Tab): void {
  state.tab = tab;
  // Leaving the tab drops anything that was mid-interaction there: a
  // confirmation left armed would still be one tap from restarting the add-on
  // when the user came back for something else, and an open suggestion list
  // would be drawn over a field nobody is in.
  if (state.tab !== "config") {
    config.confirmMode = null;
    config.openPicker = null;
  }
  if (tab === "config") actions.loadConfig();
  render();
}

export function start(el: HTMLElement, custom?: Transport): void {
  root = el;
  if (custom) transport = custom;
  applyTheme(currentTheme());
  window.addEventListener("hashchange", () => setTab(routeFromHash()));
  setTab(routeFromHash());
  // Pause polling while the panel is hidden: HA keeps the iframe alive in a
  // background tab, and a hidden page has nobody to inform.
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") poll();
    else window.clearTimeout(timer);
  });
  poll();
}

declare global {
  interface Window {
    __astrameterStart?: typeof start;
  }
}

if (typeof document !== "undefined") {
  const mount = document.getElementById("app");
  if (mount) start(mount);
}
