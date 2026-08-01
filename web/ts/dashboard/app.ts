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
    render();
    try {
      await transport.controlConsumer(deviceId, consumerId, field, value);
      await poll();
    } catch (err) {
      state.error = describe(err);
    } finally {
      delete state.busy[key];
      render();
    }
  },

  async setDevice(deviceId, field, value) {
    try {
      await transport.controlDevice(deviceId, field, value);
      await poll();
    } catch (err) {
      state.error = describe(err);
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
        config.schema = data.schema || {};
      } else {
        const data = await transport.getConfig();
        config.iniText = renderIni(data.sections, data.order);
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

  editIni(text) {
    config.iniText = text;
    config.dirty = true;
    config.message = null;
    render();
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
        const { sections, order } = parseIni(config.iniText);
        await transport.saveConfig(sections, order);
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

  async switchMode(mode) {
    config.saving = true;
    config.error = null;
    render();
    try {
      await transport.switchConfigMode(mode);
      config.message =
        "Configuration mode changed. The add-on is restarting — this can take a minute.";
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

function describe(err: unknown): string {
  if (err instanceof TransportError) return err.message;
  if (err instanceof Error) return err.message;
  return String(err);
}

// ── config.ini text round-trip ──────────────────────────────────────

export function renderIni(
  sections: Record<string, Record<string, string>>,
  order: string[],
): string {
  const names = [...order, ...Object.keys(sections).filter((s) => !order.includes(s))];
  return names
    .map((name) => {
      const pairs = sections[name] || {};
      const body = Object.entries(pairs)
        .map(([k, v]) => `${k} = ${v}`)
        .join("\n");
      return `[${name}]\n${body}`;
    })
    .join("\n\n")
    .concat("\n");
}

export function parseIni(text: string): {
  sections: Record<string, Record<string, string>>;
  order: string[];
} {
  const sections: Record<string, Record<string, string>> = {};
  const order: string[] = [];
  let current: string | null = null;
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || line.startsWith(";")) continue;
    const header = /^\[(.+)\]$/.exec(line);
    if (header) {
      current = header[1].trim();
      if (!sections[current]) {
        sections[current] = {};
        order.push(current);
      }
      continue;
    }
    if (!current) continue;
    const eq = line.indexOf("=");
    if (eq === -1) continue;
    sections[current][line.slice(0, eq).trim()] = line.slice(eq + 1).trim();
  }
  return { sections, order };
}

// ── poll loop ───────────────────────────────────────────────────────

async function poll(): Promise<void> {
  try {
    const snapshot = await transport.fetchStatus();
    if (snapshot) {
      state.snapshot = snapshot;
      state.lastFrameAt = Date.now();
      beat();
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
  const base = pollInterval(state.snapshot);
  // Back off while offline so a down backend is not hammered, but stay
  // responsive enough that recovery feels immediate.
  const delay = state.connection === "offline" ? Math.min(base * 4, 10000) : base;
  timer = window.setTimeout(poll, delay);
}

function beat(): void {
  const dot = document.getElementById("pulse");
  if (!dot) return;
  dot.classList.remove("beat");
  void dot.offsetWidth; // restart the animation
  dot.classList.add("beat");
}

function render(): void {
  root.dataset.conn = state.connection;
  patch(root, view(state, actions, config));
  const title = pageTitle(state.snapshot);
  if (document.title !== title) document.title = title;
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
