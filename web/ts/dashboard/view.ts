// The whole page as a pure function of state: view(state, actions) => VNode[].
//
// Pure means testable — the tests render to a string in Node and assert on
// the output, with no jsdom.

import { h, type VNode, type VChild } from "./vdom.js";
import {
  ago,
  batteryName,
  clockTime,
  duration,
  macLabel,
  percent,
  phaseLabel,
  seconds,
  signedWatts,
  watts,
} from "./format.js";
import {
  allConsumers,
  batteryHealth,
  contribution,
  ctDevices,
  gridTotal,
  meterHealth,
  overallHealth,
  railScale,
  type AppState,
  type Health,
  type Tab,
} from "./model.js";
import type { ConsumerStatus, DeviceStatus, StatusSnapshot } from "./types.js";
import { configView, type ConfigActions, type ConfigState } from "./config-view.js";

export interface Actions extends ConfigActions {
  selectTab(tab: Tab): void;
  toggleTheme(): void;
  setConsumer(
    deviceId: string,
    consumerId: string,
    field: string,
    value: unknown,
  ): void;
  setDevice(deviceId: string, field: string, value: unknown): void;
}

const TABS: { id: Tab; label: string }[] = [
  { id: "overview", label: "Overview" },
  { id: "batteries", label: "Batteries" },
  { id: "sources", label: "Power source" },
  { id: "config", label: "Configuration" },
  { id: "diagnostics", label: "Diagnostics" },
];

function chip(status: Health): VNode {
  return h(
    "span",
    { class: `chip ${status.severity}` },
    h("span", { class: "glyph", "aria-hidden": "true" }, status.glyph),
    status.label,
  );
}

/** A definition row that disappears entirely when the value is absent. */
function row(label: string, value: string | null | undefined): VChild[] {
  if (value == null) return [];
  return [h("dt", null, label), h("dd", null, value)];
}

function card(title: string | null, ...body: VChild[]): VNode {
  return h("section", { class: "card" }, title ? h("h2", null, title) : null, ...body);
}

// ── the hero ────────────────────────────────────────────────────────

/**
 * The Balance Rail: one signed axis with zero pinned at centre, export left
 * and import right, with each battery's contribution plotted on the SAME
 * axis underneath. That shared axis is the point — it shows magnitude, sign
 * and the causal relationship (the batteries are what pull the grid to zero)
 * as one spatial fact, which no gauge or KPI tile can express.
 */
function balanceRail(state: AppState, offline: boolean): VNode {
  const snapshot = state.snapshot;
  const grid = gridTotal(snapshot);
  const consumers = allConsumers(snapshot).filter((c) => !c.expired);
  const scale = railScale(grid, consumers);

  const importing = (grid ?? 0) > 0;
  const direction = grid == null ? "" : importing ? "import" : "export";

  // Half the track per side; scaleX drives the fill so only the compositor
  // is involved.
  const fillStyle = (value: number | undefined, positiveIsRight: boolean) => {
    if (value == null) return "transform:scaleX(0)";
    const frac = Math.min(Math.abs(value) / scale, 1);
    const toRight = positiveIsRight ? value > 0 : value < 0;
    return toRight
      ? `left:50%;right:auto;width:50%;transform:scaleX(${frac.toFixed(4)});transform-origin:left center`
      : `right:50%;left:auto;width:50%;transform:scaleX(${frac.toFixed(4)});transform-origin:right center`;
  };

  const headline =
    grid == null
      ? h("span", { class: "rail-value" }, "—")
      : h(
          "span",
          { class: `rail-value ${direction}` },
          signedWatts(grid) ?? "—",
        );

  return card(
    null,
    h(
      "div",
      { class: "rail" },
      h(
        "div",
        { class: "rail-head" },
        headline,
        h(
          "span",
          { class: "rail-label" },
          grid == null
            ? "Waiting for a grid reading"
            : importing
              ? "importing from the grid"
              : "exporting to the grid",
        ),
      ),
      h(
        "div",
        { class: "rail-track", role: "img", "aria-label": railAria(grid) },
        h("div", { class: "rail-zero" }),
        grid == null
          ? null
          : h("div", {
              class: `rail-fill ${direction}`,
              style: fillStyle(grid, true),
            }),
      ),
      h(
        "div",
        { class: "rail-scale" },
        h("span", null, `◀ export ${watts(scale) ?? ""}`),
        h("span", null, "0"),
        h("span", null, `import ${watts(scale) ?? ""} ▶`),
      ),
      consumers.length
        ? h(
            "div",
            { class: "contrib" },
            // The rows are plotted in the GRID's frame, not the battery's:
            // a charging battery pushes the grid toward import, so its bar
            // goes right. Stating the frame here is what stops the row from
            // contradicting the battery-frame numbers on the Batteries tab.
            h(
              "div",
              { class: "contrib-caption" },
              "Each battery's effect on the grid",
            ),
            ...consumers.map((c) => contributionRow(c, fillStyle)),
          )
        : null,
      offline
        ? h(
            "div",
            { class: "rail-scale" },
            h(
              "span",
              null,
              `Last update ${clockTime(snapshot?.generated_at) ?? "unknown"}`,
            ),
          )
        : null,
    ),
  );
}

function railAria(grid: number | undefined): string {
  if (grid == null) return "Grid power unknown";
  return grid > 0
    ? `Importing ${Math.round(grid)} watts from the grid`
    : `Exporting ${Math.round(Math.abs(grid))} watts to the grid`;
}

function contributionRow(
  consumer: ConsumerStatus,
  fillStyle: (v: number | undefined, r: boolean) => string,
): VNode {
  const value = contribution(consumer);
  const charging = (consumer.reported_power_w ?? 0) < 0;
  return h(
    "div",
    { class: "contrib-row" },
    h(
      "span",
      { class: "contrib-name" },
      batteryName(consumer.consumer_id, consumer.device_type),
    ),
    h(
      "div",
      { class: "contrib-track" },
      h("div", {
        class: `contrib-fill ${charging ? "charge" : "discharge"}`,
        style: fillStyle(value, true),
      }),
    ),
    h(
      "span",
      { class: "contrib-val", title: charging ? "charging" : "discharging" },
      signedWatts(value) ?? "—",
    ),
  );
}

// ── overview ────────────────────────────────────────────────────────

function overview(state: AppState, offline: boolean): VChild[] {
  const snapshot = state.snapshot;
  if (!snapshot) return [coldStart()];
  const devices = ctDevices(snapshot);
  const consumers = allConsumers(snapshot);

  return [
    balanceRail(state, offline),
    h(
      "div",
      { class: "grid" },
      ...devices.map((d) => deviceCard(d, offline)),
      ...(snapshot.powermeters || []).map((m) => meterCard(m, offline)),
    ),
    consumers.length === 0 ? noBatteries() : null,
  ];
}

function coldStart(): VNode {
  return card(
    null,
    h(
      "div",
      { class: "empty" },
      h("strong", null, "Connecting to AstraMeter…"),
      "Reading the first status snapshot.",
    ),
  );
}

function noBatteries(): VNode {
  return card(
    null,
    h(
      "div",
      { class: "empty" },
      h("strong", null, "No batteries have reported yet"),
      "AstraMeter is listening. In the Marstek app, set the battery's mode to " +
        "Automatic and select the AstraMeter CT as its meter.",
    ),
  );
}

function deviceCard(device: DeviceStatus, offline: boolean): VNode {
  const grid = device.grid;
  const reporting = (device.consumers || []).filter((c) => !c.expired).length;
  return card(
    device.ct_type ? `${device.ct_type} emulator` : "Meter emulator",
    h(
      "div",
      { class: "batt-head" },
      chip(
        device.running === false
          ? { severity: "err", glyph: "✕", label: "Stopped" }
          : device.control?.active_control
            ? { severity: "ok", glyph: "●", label: "Active control" }
            : { severity: "idle", glyph: "◎", label: "Relay mode" },
      ),
      h("span", { class: "spacer", style: "flex:1" }),
      h("span", { class: "mono" }, `UDP ${device.udp_port ?? "—"}`),
    ),
    h(
      "dl",
      { class: "kv" },
      ...row("Batteries", `${reporting}`),
      ...row("Phase A", signedWatts(grid?.l1_w)),
      ...row("Phase B", signedWatts(grid?.l2_w)),
      ...row("Phase C", signedWatts(grid?.l3_w)),
      ...row(
        offline ? "Reading at" : "Reading",
        offline ? clockTime(grid?.sample_at) : ago(secondsSince(grid?.sample_at)),
      ),
      ...row("CT MAC", macLabel(device.ct_mac)),
    ),
  );
}

function secondsSince(iso: string | undefined): number | undefined {
  if (!iso) return undefined;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return undefined;
  return Math.max(0, (Date.now() - then) / 1000);
}

function meterCard(
  meter: import("./types.js").PowermeterStatus,
  offline: boolean,
): VNode {
  const status = meterHealth(meter);
  return card(
    meter.name ? `Power source · ${meter.name}` : "Power source",
    h("div", { class: "batt-head" }, chip(status)),
    h(
      "dl",
      { class: "kv" },
      ...row("Reads via", meter.kind),
      ...row("Total", signedWatts(meter.last_total_w)),
      ...row(
        offline ? "Last read at" : "Last read",
        offline ? null : ago(meter.last_read_age_s),
      ),
      ...row("Filters", (meter.pipeline || []).join(" → ") || null),
    ),
  );
}

// ── batteries ───────────────────────────────────────────────────────

function batteries(state: AppState, actions: Actions): VChild[] {
  const snapshot = state.snapshot;
  const devices = ctDevices(snapshot);
  const any = devices.some((d) => (d.consumers || []).length > 0);
  if (!any) return [noBatteries()];
  const writable = Boolean(snapshot?.capabilities?.controls);
  return devices.flatMap((device) => [
    h(
      "div",
      { class: "grid" },
      ...(device.consumers || []).map((c) =>
        batteryCard(device, c, writable, state, actions),
      ),
    ),
  ]);
}

function batteryCard(
  device: DeviceStatus,
  consumer: ConsumerStatus,
  writable: boolean,
  state: AppState,
  actions: Actions,
): VNode {
  const status = batteryHealth(consumer);
  const power = consumer.reported_power_w;
  const charging = (power ?? 0) < 0;
  const deviceId = device.device_id || "";
  const consumerId = consumer.consumer_id || "";
  const busy = (field: string) => Boolean(state.busy[`${consumerId}:${field}`]);
  const saturation = consumer.balancer?.saturation;

  return h(
    "section",
    { class: "card" },
    h(
      "div",
      { class: "batt-head" },
      h("h3", null, batteryName(consumer.consumer_id, consumer.device_type)),
      h("span", { style: "flex:1" }),
      chip(status),
    ),
    h("div", { class: "mono" }, macLabel(consumer.consumer_id) ?? ""),
    h(
      "div",
      { class: "batt-figure" },
      h(
        "span",
        { class: `n ${charging ? "charge" : "discharge"}` },
        signedWatts(power) ?? "—",
      ),
      h(
        "span",
        { class: "rail-label" },
        power == null ? "no report" : charging ? "charging" : "discharging",
      ),
    ),
    h(
      "dl",
      { class: "kv" },
      ...row("Phase", phaseLabel(consumer.phase)),
      ...row("Target", signedWatts(consumer.balancer?.last_target_w)),
      ...row("Last seen", ago(consumer.last_seen_age_s)),
      ...row("Polls every", seconds(consumer.poll_interval_s)),
      ...row("Distribution weight", fmtWeight(consumer.distribution_weight)),
    ),
    saturation == null
      ? null
      : h(
          "div",
          { style: "margin-top:10px" },
          h(
            "div",
            { class: "rail-scale" },
            h("span", null, "Saturation"),
            h("span", null, percent(saturation) ?? ""),
          ),
          h(
            "div",
            { class: "meter" },
            h("span", {
              style: `transform:scaleX(${Math.min(saturation, 1).toFixed(3)})`,
            }),
          ),
        ),
    writable
      ? h(
          "div",
          { style: "margin-top:12px;display:flex;gap:8px;flex-wrap:wrap" },
          h(
            "button",
            {
              class: "btn sm",
              disabled: busy("active"),
              onclick: () =>
                actions.setConsumer(
                  deviceId,
                  consumerId,
                  "active",
                  !(consumer.active ?? true),
                ),
            },
            consumer.active === false ? "Enable" : "Disable",
          ),
          consumer.manual_enabled
            ? h(
                "button",
                {
                  class: "btn sm",
                  disabled: busy("auto_target"),
                  onclick: () =>
                    actions.setConsumer(deviceId, consumerId, "auto_target", true),
                },
                "Return to automatic",
              )
            : null,
        )
      : null,
  );
}

function fmtWeight(value: number | undefined): string | null {
  if (value == null) return null;
  return value === 1 ? "1.0 (neutral)" : value.toFixed(2);
}

// ── sources ─────────────────────────────────────────────────────────

function sources(state: AppState, offline: boolean): VChild[] {
  const meters = state.snapshot?.powermeters || [];
  if (!meters.length) {
    return [
      card(
        null,
        h(
          "div",
          { class: "empty" },
          h("strong", null, "No power source configured"),
          "AstraMeter needs a meter to read grid power from.",
        ),
      ),
    ];
  }
  return [h("div", { class: "grid" }, ...meters.map((m) => meterCard(m, offline)))];
}

// ── diagnostics ─────────────────────────────────────────────────────

function diagnostics(state: AppState): VChild[] {
  const snapshot = state.snapshot;
  if (!snapshot) return [coldStart()];
  const service = snapshot.service || {};
  const cards: VChild[] = [
    card(
      "Service",
      h(
        "dl",
        { class: "kv" },
        ...row("Version", service.version),
        ...row("Runtime", service.runtime),
        ...row("Uptime", duration(snapshot.uptime_s)),
        ...row("Config file", service.config_path),
        ...row("Config mode", snapshot.capabilities.config_mode),
        ...row("Log level", service.log_level),
        ...row("Commit", service.git_commit?.slice(0, 12)),
      ),
    ),
  ];

  for (const device of ctDevices(snapshot)) {
    const b = device.balancer;
    if (!b) continue;
    cards.push(
      card(
        `Balancer · ${device.device_id || device.ct_type || "device"}`,
        h(
          "dl",
          { class: "kv" },
          ...row("Predicted grid", signedWatts(b.predictor?.grid_estimate_w)),
          ...row("Prediction trust", percent(b.predictor?.trust)),
          ...row("Pool output", signedWatts(b.predictor?.pool_output_w)),
          ...row("Import trim", b.import_trim?.engaged ? "engaged" : "idle"),
          ...row("Demand average", signedWatts(b.efficiency?.demand_ema_w)),
          ...row(
            "Efficiency rotation",
            b.efficiency_rotation_enabled ? "enabled" : "disabled",
          ),
          ...row(
            "Last rotation",
            ago(b.efficiency?.last_rotation_age_s as number | undefined),
          ),
          ...row("Probing", b.probe?.candidate_id ?? null),
        ),
      ),
    );
  }

  const integrations = snapshot.integrations || {};
  const mqtt = integrations.mqtt_insights;
  if (mqtt) {
    cards.push(
      card(
        "MQTT Insights",
        h("div", { class: "batt-head" }, chip(
          mqtt.connected
            ? { severity: "ok", glyph: "●", label: "Connected" }
            : { severity: "err", glyph: "✕", label: "Disconnected" },
        )),
        h(
          "dl",
          { class: "kv" },
          ...row("Broker", mqtt.broker ? `${mqtt.broker}:${mqtt.port ?? ""}` : null),
          ...row("Base topic", mqtt.base_topic),
          ...row("Discovery", mqtt.ha_discovery ? "enabled" : "disabled"),
          ...row("Queue depth", mqtt.queue_depth?.toString()),
        ),
      ),
    );
  }

  return [h("div", { class: "grid" }, ...cards)];
}

// ── shell ───────────────────────────────────────────────────────────

export function view(
  state: AppState,
  actions: Actions,
  config: ConfigState,
): VNode[] {
  const offline = state.connection === "offline";
  const status = overallHealth(state);
  const snapshot = state.snapshot;

  const body =
    state.tab === "overview"
      ? overview(state, offline)
      : state.tab === "batteries"
        ? batteries(state, actions)
        : state.tab === "sources"
          ? sources(state, offline)
          : state.tab === "config"
            ? configView(state, config, actions)
            : diagnostics(state);

  return [
    h(
      "header",
      { class: "bar" },
      h("h1", null, "AstraMeter"),
      chip(status),
      h("span", { class: "spacer" }),
      snapshot?.service?.version
        ? h("span", { class: "ver" }, `v${snapshot.service.version}`)
        : null,
      h(
        "button",
        {
          class: "btn sm",
          onclick: () => actions.toggleTheme(),
          title: "Switch between light, dark and automatic",
        },
        "Theme",
      ),
    ),
    h(
      "nav",
      { class: "tabs" },
      ...TABS.map((tab) =>
        h(
          "button",
          {
            class: "tab",
            "aria-current": state.tab === tab.id ? "page" : false,
            onclick: () => actions.selectTab(tab.id),
          },
          tab.label,
        ),
      ),
    ),
    h(
      "main",
      null,
      offline ? offlineBanner(state) : null,
      state.error ? h("div", { class: "banner err" }, state.error) : null,
      state.notice ? h("div", { class: "banner info" }, state.notice) : null,
      ...body,
    ),
    // Only state transitions are announced, never the numbers — a live
    // region on a 1 Hz value would make a screen reader unusable.
    h("div", { class: "sr-only", role: "status", "aria-live": "polite" },
      status.label),
  ];
}

function offlineBanner(state: AppState): VNode {
  const at = clockTime(state.snapshot?.generated_at);
  return h(
    "div",
    { class: "banner err" },
    h("span", { "aria-hidden": "true" }, "✕"),
    at
      ? `Lost contact with AstraMeter. Showing the last reading from ${at}.`
      : "Lost contact with AstraMeter. Retrying…",
  );
}

export function pageTitle(snapshot: StatusSnapshot | null): string {
  const grid = gridTotal(snapshot);
  const value = signedWatts(grid);
  return value ? `${value} · AstraMeter` : "AstraMeter";
}
