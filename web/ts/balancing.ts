// balancing.ts — the interactive "how AstraMeter steers your batteries" toy on
// how-balancing-works.html.  A deliberately simplified, homeowner-friendly
// visualisation: a household load wanders and occasionally steps, and TWO
// controllers race side-by-side on the *same* load —
//
//   • a "plain meter-follower" that just reacts to the (delayed) meter, and
//   • AstraMeter, which compensates for the meter's lag, eases the battery in,
//     and damps any hunting.
//
// Driving both at once makes the point without any "dead" toggle: the plain
// follower hunts wildly while AstraMeter sits on zero.  The model is a cartoon
// of the real loop in src/astrameter/ct002/balancer.py — NOT the
// firmware-accurate plant the steering-evaluation suite uses; it only has to
// make the intuition land.  The smart controller folds in, in spirit, the
// adaptive grid-state predictor (_predict_control_grid), ramp pacing
// (_pace_reading) and oscillation-gated damping (_damp_oscillation).
//
// The pure step functions are exported and exercised by balancing.test.ts;
// everything DOM/canvas is guarded so importing under Node never touches
// `document`.

// ── Pure simulation model (exported for tests) ──────────────────────────────

export interface SimToggles {
  /** Act on a latency-compensated estimate of the grid instead of the raw,
   *  delayed meter reading (the grid-state predictor). */
  predictor: boolean;
  /** Limit how fast the battery may change its output per tick (ramp pacing). */
  pacing: boolean;
  /** Bleed loop gain while the correction keeps reversing sign (anti-hunting). */
  damping: boolean;
}

/** All of AstraMeter's smart-steering tricks engaged — the demo's "AstraMeter"
 *  controller. The toggles remain so the test suite can isolate each trick. */
export const SMART: SimToggles = { predictor: true, pacing: true, damping: true };

export interface SimState {
  /** Net battery output in watts (positive = discharging to serve the house). */
  battOut: number;
  /** Ring buffer of recent true grid values, used to model meter latency. */
  meterHistory: number[];
  /** Battery output as it was when each delayed meter sample was taken, so the
   *  predictor can add back the corrections the meter has not yet "seen". */
  battHistory: number[];
  /** Sign of the last correction and the running "is it hunting?" score. */
  lastSign: number;
  oscScore: number;
  /** Most recent true grid value (positive = importing/paying). */
  grid: number;
}

/** Steps the meter lags reality by — the root cause of overshoot/hunting. */
export const METER_LATENCY = 7;
/** Fraction of the grid error folded into battery output each tick. Below 1 so
 *  the approach is a smooth ramp rather than a single deadbeat jump. */
const CONTROL_GAIN = 0.6;
/** Input deadband (watts), like the real battery firmware: errors smaller than
 *  this are left alone, so the controller doesn't chase meter noise and buzz
 *  around zero. Keeps the balanced baseline visibly flat. */
const DEADBAND = 18;
/** Max output change per tick when ramp pacing is on (watts). */
const PACE_STEP = 55;
/** Plausible ceiling on a battery's discharge, so the un-compensated loop's
 *  oscillation stays bounded (and roughly on-screen) instead of running away. */
const BATT_MAX = 1900;
/** Loop gain of the plain meter-follower. High enough that, combined with the
 *  meter delay, it limit-cycles — the classic dead-time instability. */
const NAIVE_GAIN = 0.5;
/** Strongest damping applied to a fully-hunting loop (fraction of gain removed). */
const DAMP_MAX = 0.95;
const DAMP_ALPHA = 0.6;
const DAMP_DECAY = 0.05;

export function makeSim(initialLoad = 550): SimState {
  return {
    battOut: initialLoad,
    meterHistory: new Array<number>(METER_LATENCY).fill(0),
    battHistory: new Array<number>(METER_LATENCY).fill(initialLoad),
    lastSign: 0,
    oscScore: 0,
    grid: 0,
  };
}

/**
 * Advance the smart controller one tick against a household `load` (watts).
 *
 * Returns the new true grid power (positive = importing from the grid, the
 * thing we want at zero).  Mutates `state` in place.  Deterministic, so the
 * test can script a load profile and assert on the outcome.  `cfg` selects
 * which tricks are active; the demo always passes {@link SMART}.
 */
export function stepSim(state: SimState, load: number, cfg: SimToggles): number {
  // True grid right now, before we react: what the house draws minus what the
  // battery currently delivers.  The controller does not see this directly.
  const trueGrid = load - state.battOut;

  // The meter only reports this after a delay.  Push the truth (and the output
  // active at this instant) in, read the values from METER_LATENCY ticks ago.
  state.meterHistory.push(trueGrid);
  state.battHistory.push(state.battOut);
  const meter = state.meterHistory.shift() ?? trueGrid;
  const battWhenMeasured = state.battHistory.shift() ?? state.battOut;

  // Latency compensation: the meter reflects the grid as it was before our
  // recent corrections.  Add back the output we have committed since then, so
  // the estimate tracks where the grid really is *now* — and the controller
  // stops re-issuing a correction already on its way (the cause of the
  // overshoot/hunting a plain follower suffers).
  const estimated = meter - (state.battOut - battWhenMeasured);
  const controlGrid = cfg.predictor ? estimated : meter;

  // The correction we want to fold into battery output: if we are importing
  // (grid > 0) we need to discharge more, so raise output by the grid error.
  // Errors inside the deadband are ignored so a balanced pool sits still.
  let correction = Math.abs(controlGrid) < DEADBAND ? 0 : CONTROL_GAIN * controlGrid;

  if (cfg.damping) {
    const sign = correction > 0 ? 1 : correction < 0 ? -1 : 0;
    if (sign !== 0 && state.lastSign !== 0 && sign !== state.lastSign) {
      state.oscScore = Math.min(1, state.oscScore + DAMP_ALPHA);
    } else {
      state.oscScore *= 1 - DAMP_DECAY;
    }
    if (sign !== 0) state.lastSign = sign;
    correction *= 1 - DAMP_MAX * state.oscScore;
  }

  let desired = state.battOut + correction;
  if (cfg.pacing) {
    const delta = Math.max(-PACE_STEP, Math.min(PACE_STEP, desired - state.battOut));
    desired = state.battOut + delta;
  }
  // The battery never charges below zero output in this toy (homeowners just
  // see "the battery covers the house"); clamp to a sane discharge range.
  state.battOut = Math.max(0, Math.min(BATT_MAX, desired));

  state.grid = load - state.battOut;
  return state.grid;
}

export interface NaiveState {
  battOut: number;
  meterHistory: number[];
  grid: number;
}

export function makeNaive(initialLoad = 550): NaiveState {
  return {
    battOut: initialLoad,
    meterHistory: new Array<number>(METER_LATENCY).fill(0),
    grid: 0,
  };
}

/**
 * Advance the "plain meter-follower" one tick: just steer the battery by the
 * latest *delayed* meter reading, full stop.  With the meter lag this is the
 * textbook dead-time instability — it overshoots and limit-cycles.  This is the
 * "without AstraMeter" baseline the demo races against {@link stepSim}.
 */
export function stepNaive(state: NaiveState, load: number): number {
  const trueGrid = load - state.battOut;
  state.meterHistory.push(trueGrid);
  const meter = state.meterHistory.shift() ?? trueGrid;
  state.battOut = Math.max(0, Math.min(BATT_MAX, state.battOut + NAIVE_GAIN * meter));
  state.grid = load - state.battOut;
  return state.grid;
}

/** Sample count the meter lags by in the "why it overshoots" explainer. */
export const LAG_SAMPLES = 18;

/**
 * A scripted, repeating disturbance (0..1) for the meter-lag explainer: a
 * smooth trapezoid "bump" — think the kettle switching on, holding, then off.
 * Plotting this against a copy delayed by {@link LAG_SAMPLES} makes the meter's
 * lag visible as a horizontal offset between the two lines.
 */
export function lagSignal(phase: number): number {
  const cycle = 260;
  const p = ((phase % cycle) + cycle) % cycle;
  const rampUpEnd = 80;
  const plateauEnd = 150;
  const rampDownEnd = 195;
  if (p < 60) return 0; // calm before
  if (p < rampUpEnd) return 0.5 - 0.5 * Math.cos((Math.PI * (p - 60)) / (rampUpEnd - 60));
  if (p < plateauEnd) return 1;
  if (p < rampDownEnd)
    return 0.5 + 0.5 * Math.cos((Math.PI * (p - plateauEnd)) / (rampDownEnd - plateauEnd));
  return 0; // calm after
}

// ── DOM / canvas demo (browser only) ────────────────────────────────────────

interface LoadGen {
  /** Quiet baseline household draw (watts). */
  base: number;
  /** "calm" = sitting at base; "event" = a load step is active. */
  phase: "calm" | "event";
  /** Ticks left in the current phase before it flips. */
  ticks: number;
  /** Watts added by the active event (kettle/oven up, cloud over solar down). */
  eventDelta: number;
}

function initBalancingDemo(): void {
  const canvasEl = document.getElementById("sim-canvas") as HTMLCanvasElement | null;
  if (!canvasEl) return;
  const context = canvasEl.getContext("2d");
  if (!context) return;
  // Capture as non-null locals so the narrowing survives into the nested
  // render/resize closures below (TS drops outer-scope narrowing there).
  const canvas: HTMLCanvasElement = canvasEl;
  const ctx: CanvasRenderingContext2D = context;

  const showNaiveEl = document.getElementById("t-naive") as HTMLInputElement | null;
  const readoutLoad = document.getElementById("r-load");
  const readoutSmart = document.getElementById("r-smart");
  const readoutNaive = document.getElementById("r-naive");
  const naiveReadoutWrap = document.getElementById("r-naive-wrap");
  const kettleBtn = document.getElementById("sim-kettle");
  const pauseBtn = document.getElementById("sim-pause");

  const showNaive = (): boolean => showNaiveEl?.checked ?? true;

  const sim = makeSim(550);
  const naive = makeNaive(550);
  const gen: LoadGen = { base: 550, phase: "calm", ticks: 90, eventDelta: 0 };
  // Plotted history; trimmed to the canvas width.
  const history: { load: number; smart: number; naive: number }[] = [];
  const MAX_W = 1600; // vertical scale (±watts)
  let running = true;
  let lastFrame = 0;

  // Step the synthetic household.  Most of the time it sits quietly at the
  // baseline; every so often a discrete event — kettle/oven on (up) or a cloud
  // over solar (down) — steps the load so you can watch each controller react.
  function nextLoad(): number {
    if (gen.ticks > 0) {
      gen.ticks--;
    } else if (gen.phase === "calm") {
      gen.phase = "event";
      gen.eventDelta = (Math.random() < 0.5 ? 1 : -1) * (150 + Math.random() * 200);
      gen.ticks = 130 + Math.floor(Math.random() * 120);
    } else {
      gen.phase = "calm";
      gen.eventDelta = 0;
      gen.ticks = 150 + Math.floor(Math.random() * 150);
    }
    const noise = (Math.random() - 0.5) * 10;
    return Math.max(0, gen.base + gen.eventDelta + noise);
  }

  function resize(): void {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cssW = canvas.clientWidth || 640;
    const cssH = canvas.clientHeight || 320;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function yOf(w: number, h: number): number {
    // 0 W at the vertical centre; +import above, −export below.
    return h / 2 - (w / MAX_W) * (h / 2 - 14);
  }

  function plotLine(
    key: "load" | "smart" | "naive",
    color: string,
    width: number,
    h: number,
    dx: number,
  ): void {
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.beginPath();
    history.forEach((p, i) => {
      const y = yOf(p[key], h);
      if (i === 0) ctx.moveTo(0, y);
      else ctx.lineTo(i * dx, y);
    });
    ctx.stroke();
  }

  function draw(): void {
    const w = canvas.clientWidth || 640;
    const h = canvas.clientHeight || 320;
    ctx.clearRect(0, 0, w, h);

    const zeroY = yOf(0, h);
    // Import (above zero) / export (below zero) tint so the centre line reads
    // as "balanced" at a glance.
    ctx.fillStyle = "rgba(244, 63, 94, 0.05)";
    ctx.fillRect(0, 0, w, zeroY);
    ctx.fillStyle = "rgba(16, 185, 129, 0.05)";
    ctx.fillRect(0, zeroY, w, h - zeroY);

    // Zero / target line.
    ctx.strokeStyle = "rgba(226, 232, 240, 0.55)";
    ctx.setLineDash([5, 5]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, zeroY);
    ctx.lineTo(w, zeroY);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = "rgba(148, 163, 184, 0.9)";
    ctx.font = "11px -apple-system, system-ui, sans-serif";
    ctx.fillText("0 W · perfectly balanced", 8, zeroY - 6);

    if (history.length < 2) return;
    const dx = w / (history.length - 1);

    // Plain follower first (behind), only when shown.
    if (showNaive()) plotLine("naive", "#f43f5e", 2, h, dx);
    // AstraMeter on top — the line that hugs zero.
    plotLine("smart", "#10b981", 2.5, h, dx);
  }

  function updateReadouts(): void {
    const last = history[history.length - 1];
    if (!last) return;
    const fmt = (v: number) => {
      const r = Math.round(v);
      return r > 0 ? `+${r} W` : `${r} W`;
    };
    if (readoutLoad) readoutLoad.textContent = `${Math.round(last.load)} W`;
    if (readoutSmart) readoutSmart.textContent = fmt(last.smart);
    if (readoutNaive) readoutNaive.textContent = fmt(last.naive);
    if (naiveReadoutWrap) naiveReadoutWrap.style.display = showNaive() ? "" : "none";
  }

  function tick(load: number): void {
    const smart = stepSim(sim, load, SMART);
    const nv = stepNaive(naive, load);
    history.push({ load, smart, naive: nv });
    const maxPts = Math.max(120, Math.floor(canvas.clientWidth || 640));
    while (history.length > maxPts) history.shift();
  }

  function frame(now: number): void {
    if (running) {
      // One model tick per ~38 ms (regardless of display refresh rate): slow
      // enough that the eye can follow the meter delay and recovery, brisk
      // enough to feel live.
      if (now - lastFrame > 38) {
        lastFrame = now;
        tick(nextLoad());
        updateReadouts();
      }
      draw();
    }
    requestAnimationFrame(frame);
  }

  kettleBtn?.addEventListener("click", () => {
    // Force a big load step right now so both controllers must react on demand.
    gen.phase = "event";
    gen.eventDelta = 700;
    gen.ticks = 120;
  });
  pauseBtn?.addEventListener("click", () => {
    running = !running;
    pauseBtn.textContent = running ? "⏸ Pause" : "▶ Play";
    pauseBtn.setAttribute("aria-pressed", running ? "false" : "true");
  });

  const ro = new ResizeObserver(() => resize());
  ro.observe(canvas);
  resize();
  // Warm up so the first painted frame already shows a settled trace.
  for (let i = 0; i < METER_LATENCY + 6; i++) tick(nextLoad());
  requestAnimationFrame(frame);
}

// ── Meter-lag explainer (the "how it works" mechanism viz) ──────────────────

function initLagDemo(): void {
  const canvasEl = document.getElementById("lag-canvas") as HTMLCanvasElement | null;
  if (!canvasEl) return;
  const context = canvasEl.getContext("2d");
  if (!context) return;
  const canvas: HTMLCanvasElement = canvasEl;
  const ctx: CanvasRenderingContext2D = context;

  let phase = 0;
  let lastFrame = 0;

  function resize(): void {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cssW = canvas.clientWidth || 640;
    const cssH = canvas.clientHeight || 200;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function plot(
    sampleAt: (i: number) => number,
    color: string,
    width: number,
    dash: number[],
    w: number,
    h: number,
    n: number,
  ): void {
    const dx = w / (n - 1);
    const top = 24;
    const bot = h - 22;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.setLineDash(dash);
    ctx.beginPath();
    for (let i = 0; i < n; i++) {
      const y = bot - sampleAt(i) * (bot - top);
      if (i === 0) ctx.moveTo(0, y);
      else ctx.lineTo(i * dx, y);
    }
    ctx.stroke();
    ctx.setLineDash([]);
  }

  function draw(): void {
    const w = canvas.clientWidth || 640;
    const h = canvas.clientHeight || 200;
    ctx.clearRect(0, 0, w, h);
    const n = Math.max(120, Math.floor(w));
    // Oldest sample on the left, newest on the right (scrolling left).
    const realAt = (i: number) => lagSignal(phase - (n - 1 - i));
    const meterAt = (i: number) => lagSignal(phase - (n - 1 - i) - LAG_SAMPLES);

    // What the meter reports — a moment late (grey, dashed, behind).
    plot(meterAt, "rgba(148,163,184,0.9)", 2, [5, 4], w, h, n);
    // Reality, and what AstraMeter acts on (emerald, on time).
    plot(realAt, "#10b981", 2.5, [], w, h, n);

    // Call out the horizontal gap between the two rising edges near the middle.
    ctx.fillStyle = "rgba(148,163,184,0.9)";
    ctx.font = "11px -apple-system, system-ui, sans-serif";
    const dx = w / (n - 1);
    ctx.fillText("→ the meter is a moment behind", LAG_SAMPLES * dx + w * 0.18, 16);
  }

  function frame(now: number): void {
    if (now - lastFrame > 38) {
      lastFrame = now;
      phase += 1;
    }
    draw();
    requestAnimationFrame(frame);
  }

  const ro = new ResizeObserver(() => resize());
  ro.observe(canvas);
  resize();
  requestAnimationFrame(frame);
}

if (typeof document !== "undefined") {
  const boot = () => {
    initLagDemo();
    initBalancingDemo();
  };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}
