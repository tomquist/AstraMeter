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
  meterClass,
  percent,
  phaseLabel,
  seconds,
  signedWatts,
  watts,
} from "./format.js";
import {
  batteryHealth,
  contribution,
  ctDevices,
  gridTotal,
  hasReported,
  health,
  meterHealth,
  overallHealth,
  pendingOr,
  railScale,
  reportingConsumers,
  shellyBatteries,
  shellyDevices,
  type AppState,
  type Health,
  type Severity,
  type Tab,
} from "./model.js";
import type {
  ConsumerStatus,
  DeviceStatus,
  ShellyBatteryStatus,
  StatusSnapshot,
} from "./types.js";
import { configView, type ConfigActions, type ConfigState } from "./config-view.js";
import {
  batterySeries,
  meterSeries,
  seriesOf,
  sparkGeometry,
  type SeriesHistory,
} from "./history.js";

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

/**
 * Whether this backend has a configuration to edit at all.
 *
 * `config_mode` names which surface to show, so a backend that omits it has
 * none: an ESPHome device's settings are compiled into its firmware, and a
 * tab offering to change them would be a promise nothing can keep. Kept while
 * the first snapshot is still in flight so the tab does not appear late on
 * the backends that do have one.
 */
function hasConfigSurface(snapshot: StatusSnapshot | null): boolean {
  return !snapshot || Boolean(snapshot.capabilities?.config_mode);
}

function chip(status: Health): VNode {
  return h(
    "span",
    { class: `chip ${status.severity}` },
    h("span", { class: "glyph", "aria-hidden": "true" }, status.glyph),
    status.label,
  );
}

// A sparkline's viewBox. Fixed units, scaled by CSS: the drawing has no idea
// how wide the card it lands in is.
const SPARK_W = 100;
const SPARK_H = 26;

/**
 * A trend line for one series, or nothing while there is too little to say.
 *
 * Drawn from what the page has already polled (see history.ts), so it starts
 * empty on a fresh load and fills in over the next few reads. Labelled with
 * its own range rather than an axis: at this size a scale would be unreadable,
 * but "how far did it swing" is the question the line is answering.
 */
function sparkline(values: number[], label: string): VChild {
  const geometry = sparkGeometry(values, SPARK_W, SPARK_H);
  if (!geometry) return null;
  const range = `${signedWatts(geometry.min) ?? ""} to ${signedWatts(geometry.max) ?? ""}`;
  return h(
    "div",
    { class: "spark-wrap" },
    h(
      "svg",
      {
        class: "spark",
        viewBox: `0 0 ${SPARK_W} ${SPARK_H}`,
        preserveAspectRatio: "none",
        role: "img",
        "aria-label": `${label} over the last ${values.length} readings, ${range}`,
      },
      geometry.zeroY == null
        ? null
        : h("line", {
            class: "spark-zero",
            x1: 0,
            y1: geometry.zeroY,
            x2: SPARK_W,
            y2: geometry.zeroY,
          }),
      h("polyline", { class: "spark-line", points: geometry.points }),
    ),
    h("span", { class: "spark-range" }, range),
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
  const consumers = reportingConsumers(snapshot).filter((c) => !c.expired);
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

function overview(state: AppState, offline: boolean, actions: Actions): VChild[] {
  const snapshot = state.snapshot;
  if (!snapshot) return [coldStart()];
  const devices = ctDevices(snapshot);
  const consumers = reportingConsumers(snapshot);

  const shelly = shellyDevices(snapshot);
  const polling = shellyBatteries(snapshot);

  return [
    balanceRail(state, offline),
    h(
      "div",
      { class: "grid" },
      ...devices.map((d) =>
        deviceCard(d, offline, Boolean(snapshot.capabilities?.controls), state, actions),
      ),
      ...shelly.map((d) => shellyCard(d, offline)),
      ...(snapshot.powermeters || []).map((m) => meterCard(m, offline, state.history)),
    ),
    consumers.length === 0 && polling.length === 0 ? noBatteries(snapshot) : null,
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

/**
 * The empty state names the meter the user is actually emulating. Telling a
 * Shelly user to pick "the AstraMeter CT" sends them looking for a device
 * this install does not pretend to be.
 */
function noBatteries(snapshot: StatusSnapshot | null): VNode {
  const shelly = shellyDevices(snapshot).length > 0;
  return card(
    null,
    h(
      "div",
      { class: "empty" },
      h("strong", null, "No batteries have reported yet"),
      shelly
        ? "AstraMeter is listening. In the Marstek app, set the battery's " +
            "mode to Automatic and select this Shelly meter."
        : "AstraMeter is listening. In the Marstek app, set the battery's " +
            "mode to Automatic and select the AstraMeter CT as its meter.",
    ),
  );
}

function deviceCard(
  device: DeviceStatus,
  offline: boolean,
  writable: boolean,
  state: AppState,
  actions: Actions,
): VNode {
  const grid = device.grid;
  const reporting = (device.consumers || []).filter(
    (c) => hasReported(c) && !c.expired,
  ).length;
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
    writable ? deviceControls(device, state, actions) : null,
  );
}

/**
 * The device-wide controls the MQTT integration already exposes: the Active
 * Control switch and the Force Rotation button.
 *
 * Rotation is only offered when the balancer says rotation is enabled —
 * pressing it otherwise does nothing, exactly as the MQTT button would.
 */
function deviceControls(
  device: DeviceStatus,
  state: AppState,
  actions: Actions,
): VNode {
  const deviceId = device.device_id || "";
  const active = pendingOr(
    state,
    `${deviceId}:active_control`,
    device.control?.active_control ?? false,
  );
  const rotationOn = Boolean(device.balancer?.efficiency_rotation_enabled);
  return h(
    "div",
    { class: "controls" },
    h(
      "label",
      { class: "row" },
      h("input", {
        type: "checkbox",
        checked: active,
        disabled: Boolean(state.busy[`${deviceId}:active_control`]),
        "aria-label": "Active control",
        onchange: (e: Event) =>
          actions.setDevice(
            deviceId,
            "active_control",
            (e.target as HTMLInputElement).checked,
          ),
      }),
      h("span", null, "Active control"),
    ),
    h(
      "button",
      {
        class: "btn sm",
        disabled: !rotationOn || Boolean(state.busy[`${deviceId}:force_rotation`]),
        title: rotationOn
          ? "Swap which batteries take the load now"
          : "Efficiency rotation is off — set a minimum efficient power to enable it",
        onclick: () => actions.setDevice(deviceId, "force_rotation", true),
      },
      "Force rotation",
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
  history: SeriesHistory,
): VNode {
  const status = meterHealth(meter);
  return card(
    meter.name ? `Power source · ${meter.name}` : "Power source",
    h("div", { class: "batt-head" }, chip(status)),
    sparkline(seriesOf(history, meterSeries(meter.name)), "Power source total"),
    h(
      "dl",
      { class: "kv" },
      ...row("Reads via", meterClass(meter.kind)),
      ...row("Total", signedWatts(meter.last_total_w)),
      ...row(
        offline ? "Last read at" : "Last read",
        offline ? null : ago(meter.last_read_age_s),
      ),
      ...row(
        "Filters",
        (meter.pipeline || [])
          .map(meterClass)
          .filter(Boolean)
          .join(" → ") || null,
      ),
    ),
  );
}

// ── batteries ───────────────────────────────────────────────────────

function batteries(state: AppState, actions: Actions): VChild[] {
  const snapshot = state.snapshot;
  const devices = ctDevices(snapshot);
  const shelly = shellyDevices(snapshot);
  const anyCt = devices.some((d) => (d.consumers || []).length > 0);
  const anyShelly = shelly.some((d) => (d.batteries || []).length > 0);
  if (!anyCt && !anyShelly) return [noBatteries(snapshot)];
  const writable = Boolean(snapshot?.capabilities?.controls);
  return [
    ...devices.map((device) =>
      h(
        "div",
        { class: "grid" },
        ...(device.consumers || []).map((c) =>
          batteryCard(device, c, writable, state, actions),
        ),
      ),
    ),
    // A Shelly emulator knows only that a battery polled it — no power, no
    // target, nothing to steer — so these are liveness rows, not the control
    // cards a CT battery gets.
    ...shelly.map((device) =>
      h("div", { class: "grid" }, ...(device.batteries || []).map(shellyBatteryCard)),
    ),
  ];
}

/**
 * A Shelly emulator: what it is, and who is polling it.
 *
 * There is no grid triple, no balancer and no controls here — this device
 * serves the meter reading and takes no part in steering, and the card says
 * so rather than leaving CT-shaped holes.
 */
function shellyCard(device: DeviceStatus, offline: boolean): VNode {
  const polling = (device.batteries || []).filter((b) => b.active !== false).length;
  return card(
    device.device_type ? `${device.device_type} emulator` : "Meter emulator",
    h(
      "div",
      { class: "batt-head" },
      chip(
        device.running === false
          ? health("err", "Stopped")
          : polling > 0
            ? health("ok", "Serving readings")
            : health("warn", "No batteries polling"),
      ),
      h("span", { style: "flex:1" }),
      h("span", { class: "mono" }, `UDP ${device.udp_port ?? "—"}`),
    ),
    h(
      "dl",
      { class: "kv" },
      ...row("Batteries polling", `${polling}`),
      ...row("Drops after", seconds(device.inactive_timeout_s)),
      ...row(
        offline ? "Started at" : "Up for",
        offline ? clockTime(device.started_at) : duration(secondsSince(device.started_at)),
      ),
    ),
  );
}

/** One battery polling a Shelly emulator: address and liveness only. */
function shellyBatteryCard(battery: ShellyBatteryStatus): VNode {
  return h(
    "section",
    { class: "card" },
    h(
      "div",
      { class: "batt-head" },
      h("h3", null, battery.ip || "Unknown address"),
      h("span", { style: "flex:1" }),
      chip(
        battery.active === false
          ? health("err", "Not reporting")
          : health("ok", "Polling"),
      ),
    ),
    h(
      "dl",
      { class: "kv" },
      ...row("Last seen", ago(battery.last_seen_age_s)),
      ...row("Polls every", seconds(battery.poll_interval_s)),
    ),
  );
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
  const saturation = consumer.balancer?.saturation;
  // Nothing has reported under this address, so every figure below would be
  // a default dressed up as a measurement — 0 W "discharging" on phase A.
  // Say what it actually is instead, and keep the controls so the setting
  // can be cleared from here.
  const reported = hasReported(consumer);

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
    reported
      ? h(
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
        )
      : h(
          "p",
          { class: "hint" },
          "No battery has ever reported at this address. A saved setting is " +
            "holding the slot — from a retained MQTT command, or a control " +
            "changed while the battery was away. It is not steered, and it " +
            "goes away once the setting is cleared.",
        ),
    reported
      ? sparkline(seriesOf(state.history, batterySeries(consumer)), "Battery power")
      : null,
    reported
      ? h(
          "dl",
          { class: "kv" },
          ...row("Phase", phaseLabel(consumer.phase)),
          ...row("Target", signedWatts(consumer.balancer?.last_target_w)),
          ...row("Last seen", ago(consumer.last_seen_age_s)),
          ...row("Polls every", seconds(consumer.poll_interval_s)),
          // Redundant while the two agree, which is the normal case — show it
          // only when a dedupe window is holding replies back.
          ...(consumer.answer_interval_s != null &&
          consumer.poll_interval_s != null &&
          consumer.answer_interval_s > consumer.poll_interval_s
            ? row("Answered every", seconds(consumer.answer_interval_s))
            : []),
        )
      : null,
    saturation == null || !reported
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
    writable ? consumerControls(device, consumer, state, actions) : null,
  );
}

/**
 * Every per-battery control the MQTT integration exposes, with the same
 * ranges and the same conditions on when each one applies.
 *
 * Folded into a disclosure: the common case is reading, and four numeric
 * controls open by default would bury the reading they exist to change.
 */
function consumerControls(
  device: DeviceStatus,
  consumer: ConsumerStatus,
  state: AppState,
  actions: Actions,
): VNode {
  const deviceId = device.device_id || "";
  const consumerId = consumer.consumer_id || "";
  const busy = (field: string) => Boolean(state.busy[`${consumerId}:${field}`]);
  const set = (field: string, value: unknown) =>
    actions.setConsumer(deviceId, consumerId, field, value);
  const pend = <T,>(field: string, actual: T): T =>
    pendingOr(state, `${consumerId}:${field}`, actual);
  // auto_target is the inverse of manual_enabled on the wire.
  const autoPending = pend<boolean | undefined>("auto_target", undefined);
  const manual =
    autoPending === undefined ? Boolean(consumer.manual_enabled) : !autoPending;

  return h(
    "details",
    { class: "controls-fold" },
    h("summary", null, "Controls"),

    h(
      "label",
      { class: "row" },
      h("input", {
        type: "checkbox",
        checked: pend("active", consumer.active ?? true),
        disabled: busy("active"),
        "aria-label": "Active",
        onchange: (e: Event) =>
          set("active", (e.target as HTMLInputElement).checked),
      }),
      h("span", null, "Active"),
    ),

    // "Auto target" on means the balancer owns this battery; off hands it to
    // the manual setpoint. Same semantics as the MQTT switch.
    h(
      "label",
      { class: "row" },
      h("input", {
        type: "checkbox",
        checked: !manual,
        disabled: busy("auto_target"),
        "aria-label": "Auto target",
        onchange: (e: Event) =>
          set("auto_target", (e.target as HTMLInputElement).checked),
      }),
      h("span", null, "Automatic target"),
    ),

    manual
      ? numberControl({
          label: "Manual target",
          unit: "W",
          value: pend("manual_target", consumer.manual_target_w),
          min: -10000,
          max: 10000,
          step: 10,
          busy: busy("manual_target"),
          onCommit: (v) => set("manual_target", v),
        })
      : null,

    sliderControl({
      label: "Distribution weight",
      value: pend("distribution_weight", consumer.distribution_weight ?? 1),
      min: 0,
      max: 10,
      step: 0.1,
      digits: 1,
      busy: busy("distribution_weight"),
      onCommit: (v) => set("distribution_weight", v),
    }),

    // Only meaningful while efficiency rotation is running, exactly as the
    // MQTT entity is only published then.
    device.balancer?.efficiency_rotation_enabled
      ? sliderControl({
          label: "Efficiency window",
          unit: "%",
          value: pend(
            "efficiency_window_weight",
            consumer.efficiency_window_weight_pct ?? 100,
          ),
          min: 0,
          max: 100,
          step: 5,
          digits: 0,
          busy: busy("efficiency_window_weight"),
          onCommit: (v) => set("efficiency_window_weight", v),
        })
      : null,

    // DC-coupled batteries only — the MQTT entity is conditional the same way.
    consumer.min_dc_output_applicable
      ? numberControl({
          label: "Min DC output",
          unit: "W",
          value: pend("min_dc_output", consumer.min_dc_output_w),
          min: 0,
          max: 1000,
          step: 1,
          busy: busy("min_dc_output"),
          onCommit: (v) => set("min_dc_output", v),
        })
      : null,
  );
}

interface ControlSpec {
  label: string;
  unit?: string;
  value: number | undefined;
  min: number;
  max: number;
  step: number;
  busy: boolean;
  onCommit(value: number): void;
}

/**
 * A number box that commits on `change`, not on every keystroke.
 *
 * Committing per keystroke would send "1", "10", "100" on the way to 1000 —
 * each one a real command to a real battery.
 */
function numberControl(spec: ControlSpec): VNode {
  return h(
    "label",
    { class: "field control" },
    h(
      "span",
      { class: "name" },
      spec.unit ? `${spec.label} (${spec.unit})` : spec.label,
    ),
    h("input", {
      type: "number",
      value: spec.value == null ? "" : String(spec.value),
      min: spec.min,
      max: spec.max,
      step: spec.step,
      disabled: spec.busy,
      "aria-label": spec.label,
      onchange: (e: Event) => {
        // Number("") is 0, so clearing the box and tabbing away would commit
        // a real 0 W to the battery. An empty field means "no change".
        //
        // Deliberately NOT clamped to min/max: the server enforces the same
        // range and says why it refused, and silently rewriting what someone
        // typed is worse feedback than telling them it was out of range.
        const raw = (e.target as HTMLInputElement).value.trim();
        if (raw === "") return;
        const parsed = Number(raw);
        if (Number.isFinite(parsed)) spec.onCommit(parsed);
      },
    }),
  );
}

/** A slider that shows its value and commits on release, for the same reason. */
function sliderControl(spec: ControlSpec & { digits: number }): VNode {
  const shown = spec.value == null ? "—" : spec.value.toFixed(spec.digits);
  return h(
    "div",
    { class: "control slider" },
    h(
      "div",
      { class: "slider-head" },
      h("span", { class: "name" }, spec.label),
      h("span", { class: "slider-val" }, spec.unit ? `${shown}${spec.unit}` : shown),
    ),
    h("input", {
      type: "range",
      value: spec.value == null ? spec.min : String(spec.value),
      min: spec.min,
      max: spec.max,
      step: spec.step,
      disabled: spec.busy,
      "aria-label": spec.label,
      onchange: (e: Event) => {
        const parsed = Number((e.target as HTMLInputElement).value);
        if (Number.isFinite(parsed)) spec.onCommit(parsed);
      },
    }),
  );
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
  return [
    h("div", { class: "grid" }, ...meters.map((m) => meterCard(m, offline, state.history))),
  ];
}

// ── diagnostics ─────────────────────────────────────────────────────

/**
 * The balancer's own verdict on the loop, said in one line.
 *
 * Everything else on this card describes a mechanism; this is the answer the
 * mechanisms exist to produce, so it leads the card and carries the severity.
 * The wording states what was measured and stops there — "off target" does not
 * guess whether the loop is hunting or lagging, because the two have opposite
 * fixes and nothing here can tell them apart. "limited" is deliberately not an
 * error: a full or empty pack is the house's state, not a fault.
 */
const CONTROL_QUALITY_BLURB: Record<string, string> = {
  idle: "Nothing is being steered right now.",
  warmup: "Watching the grid — not enough samples yet.",
  stable: "The grid is being held close to zero.",
  off_target: "The grid is not being held at zero, and the batteries still have room.",
  limited: "No headroom left — the batteries are full, empty or clamped.",
};

const CONTROL_QUALITY_SEVERITY: Record<string, Severity> = {
  stable: "ok",
  off_target: "warn",
  limited: "idle",
  warmup: "idle",
  idle: "idle",
};

const CONTROL_QUALITY_LABEL: Record<string, string> = {
  stable: "Stable",
  off_target: "Off target",
  limited: "Limited",
  warmup: "Warming up",
  idle: "Idle",
};

/** A verdict this bundle predates is shown as-is rather than mislabelled. */
function controlQualityHealth(verdict: string): Health {
  return health(
    CONTROL_QUALITY_SEVERITY[verdict] ?? "idle",
    CONTROL_QUALITY_LABEL[verdict] ?? verdict,
  );
}

/** The score crosses the wire as 0–100; `percent` wants a fraction. */
function scoreFraction(value: number | undefined): number | null {
  return value == null ? null : value / 100;
}

function perMinute(value: number | undefined): string | null {
  if (value == null || !Number.isFinite(value)) return null;
  return `${value.toFixed(value < 10 ? 1 : 0)} / min`;
}

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
        ...row("Config mode", snapshot.capabilities?.config_mode),
        ...row("Log level", service.log_level),
        ...row("Commit", service.git_commit?.slice(0, 12)),
      ),
    ),
  ];

  for (const device of ctDevices(snapshot)) {
    const b = device.balancer;
    if (!b) continue;
    const quality = b.control_quality;
    cards.push(
      card(
        `Balancer · ${device.device_id || device.ct_type || "device"}`,
        quality?.verdict
          ? h("div", { class: "batt-head" }, chip(controlQualityHealth(quality.verdict)))
          : null,
        quality?.verdict && CONTROL_QUALITY_BLURB[quality.verdict]
          ? h("p", { class: "hint" }, CONTROL_QUALITY_BLURB[quality.verdict])
          : null,
        h(
          "dl",
          { class: "kv" },
          ...row("Quality score", percent(scoreFraction(quality?.score_pct))),
          ...row("Mean grid error", watts(quality?.error_w)),
          ...row("Time inside band", percent(quality?.in_band_fraction)),
          ...row("Settling band", watts(quality?.band_w)),
          // Evidence, not a verdict: a high rate alongside "off target"
          // points at a loop overshooting past zero rather than lagging
          // behind — but a noisy meter produces the same reading, so the
          // page shows the number and lets the reader judge.
          ...row("Zero crossings", perMinute(quality?.crossings_per_min)),
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
  const tabs = TABS.filter((tab) => tab.id !== "config" || hasConfigSurface(snapshot));
  // A deep link into a tab this backend does not have would otherwise render
  // an empty shell nothing can ever fill.
  const active = tabs.some((tab) => tab.id === state.tab) ? state.tab : "overview";

  const body =
    active === "overview"
      ? overview(state, offline, actions)
      : active === "batteries"
        ? batteries(state, actions)
        : active === "sources"
          ? sources(state, offline)
          : active === "config"
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
      ...tabs.map((tab) =>
        h(
          "button",
          {
            class: "tab",
            "aria-current": active === tab.id ? "page" : false,
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
