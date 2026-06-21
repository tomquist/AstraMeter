// balancing.ts — the interactive "how AstraMeter steers your batteries" toy on
// how-balancing-works.html.  A deliberately simplified, homeowner-friendly
// visualisation: a household load wanders and spikes, one (or a few) batteries
// chase the grid back to zero, and three toggles let you switch off the tricks
// that keep the chase smooth so you can *see* why each exists.
//
// The model here is a cartoon of the real control loop in
// src/astrameter/ct002/balancer.py — it is NOT the firmware-accurate plant the
// steering-evaluation suite uses.  It only has to make the intuition land:
//   • Latency compensation  ↔  the adaptive grid-state predictor
//                              (LoadBalancer._predict_control_grid)
//   • Smooth ramping         ↔  ramp pacing (LoadBalancer._pace_reading)
//   • Anti-oscillation       ↔  oscillation-gated damping
//                              (LoadBalancer._damp_oscillation)
//
// The pure step function (`stepSim`) is exported and exercised by
// balancing.test.ts; everything DOM/canvas is guarded so importing the module
// under Node (the test runner) never touches `document`.

// ── Pure simulation model (exported for tests) ──────────────────────────────

export interface SimToggles {
  /** Act on a latency-compensated estimate of the grid instead of the raw,
   *  delayed meter reading (the grid-state predictor). */
  predictor: boolean;
  /** Limit how fast each battery may change its output per tick (ramp pacing). */
  pacing: boolean;
  /** Bleed loop gain while the correction keeps reversing sign (anti-hunting). */
  damping: boolean;
}

export interface SimState {
  /** Net battery output in watts (positive = discharging to serve the house). */
  battOut: number;
  /** Ring buffer of recent true grid values, used to model meter latency. */
  meterHistory: number[];
  /** Predictor estimate of the *current* grid (see _predict_control_grid). */
  pred: number;
  /** Sign of the last correction and the running "is it hunting?" score. */
  lastSign: number;
  oscScore: number;
  /** Most recent true grid value (positive = importing/paying). */
  grid: number;
}

/** Steps the meter lags reality by — the root cause of overshoot/hunting. */
export const METER_LATENCY = 7;
/** How hard the predictor pulls its estimate toward a fresh meter reading. */
const PREDICTOR_TRUST = 0.3;
/** Max output change per tick when ramp pacing is on (watts). */
const PACE_STEP = 55;
/** Strongest damping applied to a fully-hunting loop (fraction of gain removed). */
const DAMP_MAX = 0.85;
const DAMP_ALPHA = 0.35;
const DAMP_DECAY = 0.12;

export function makeSim(initialLoad = 250): SimState {
  return {
    battOut: initialLoad,
    meterHistory: new Array<number>(METER_LATENCY + 1).fill(0),
    pred: 0,
    lastSign: 0,
    oscScore: 0,
    grid: 0,
  };
}

/**
 * Advance the simulation one tick against a given household `load` (watts).
 *
 * Returns the new true grid power (positive = importing from the grid, the
 * thing we want at zero).  Mutates `state` in place.  Deterministic: no RNG in
 * here, so the test can script a load profile and assert on the outcome.
 */
export function stepSim(state: SimState, load: number, cfg: SimToggles): number {
  // True grid right now, before we react: what the house draws minus what the
  // battery currently delivers.  This is the physical truth; the controller
  // does not get to see it directly.
  const trueGrid = load - state.battOut;

  // The meter only reports this after a delay.  Push the truth in, read the
  // value from METER_LATENCY ticks ago back out.
  state.meterHistory.push(trueGrid);
  const meter = state.meterHistory.shift() ?? trueGrid;

  // Advance the predictor by the pool's own last output change (grid moves
  // opposite to battery output) and nudge it toward the delayed meter.  This
  // reconstructs the grid the meter has not caught up to yet — without it the
  // controller keeps re-issuing a correction already in flight.
  state.pred += PREDICTOR_TRUST * (meter - state.pred);
  const controlGrid = cfg.predictor ? state.pred : meter;

  // The correction we want to fold into battery output: if we are importing
  // (grid > 0) we need to discharge more, so raise output by the grid error.
  let correction = controlGrid;

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
  // see "the battery covers the house"); clamp so the picture stays simple.
  state.battOut = Math.max(0, desired);

  // Account for the output we just changed inside the predictor's estimate so
  // next tick's crediting starts from the right place.
  state.pred -= state.battOut - (load - trueGrid);

  state.grid = load - state.battOut;
  return state.grid;
}

// ── DOM / canvas demo (browser only) ────────────────────────────────────────

interface LoadGen {
  base: number;
  current: number;
  /** Ticks remaining on a transient event (kettle, cloud). */
  eventTicks: number;
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

  const toggleEls = {
    predictor: document.getElementById("t-predictor") as HTMLInputElement | null,
    pacing: document.getElementById("t-pacing") as HTMLInputElement | null,
    damping: document.getElementById("t-damping") as HTMLInputElement | null,
  };
  const readoutLoad = document.getElementById("r-load");
  const readoutBatt = document.getElementById("r-batt");
  const readoutGrid = document.getElementById("r-grid");
  const scoreBar = document.getElementById("sim-score-bar");
  const scoreLabel = document.getElementById("sim-score-label");
  const kettleBtn = document.getElementById("sim-kettle");
  const pauseBtn = document.getElementById("sim-pause");

  const cfg = (): SimToggles => ({
    predictor: toggleEls.predictor?.checked ?? true,
    pacing: toggleEls.pacing?.checked ?? true,
    damping: toggleEls.damping?.checked ?? true,
  });

  const sim = makeSim(250);
  const gen: LoadGen = { base: 250, current: 250, eventTicks: 0, eventDelta: 0 };
  // Plotted history of [grid, load, battery]; trimmed to the canvas width.
  const history: { grid: number; load: number; batt: number }[] = [];
  const errWindow: number[] = [];
  const MAX_W = 1400; // vertical scale (±watts)
  let running = true;
  let lastFrame = 0;

  // Step the synthetic household: gentle drift + occasional spikes/dips so the
  // controller always has something to chase.
  function nextLoad(): number {
    if (gen.eventTicks > 0) {
      gen.eventTicks--;
    } else if (Math.random() < 0.012) {
      // Kettle/oven spike (up) or a cloud passing over solar (acts like a dip).
      gen.eventDelta = (Math.random() < 0.5 ? 1 : -1) * (250 + Math.random() * 550);
      gen.eventTicks = 40 + Math.floor(Math.random() * 80);
    } else {
      gen.eventDelta = 0;
    }
    gen.base += (Math.random() - 0.5) * 14;
    gen.base = Math.max(120, Math.min(600, gen.base));
    const noise = (Math.random() - 0.5) * 30;
    gen.current = Math.max(0, gen.base + gen.eventDelta + noise);
    return gen.current;
  }

  function resize(): void {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const cssW = canvas.clientWidth || 640;
    const cssH = canvas.clientHeight || 320;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    ctx!.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  function yOf(w: number, h: number): number {
    // 0 W at the vertical centre; +import above, −export below.
    return h / 2 - (w / MAX_W) * (h / 2 - 14);
  }

  function draw(): void {
    const w = canvas.clientWidth || 640;
    const h = canvas.clientHeight || 320;
    ctx!.clearRect(0, 0, w, h);

    const zeroY = yOf(0, h);
    // Import (above zero) / export (below zero) tint so the goal — the centre
    // line — reads as "balanced" at a glance.
    ctx!.fillStyle = "rgba(244, 63, 94, 0.05)";
    ctx!.fillRect(0, 0, w, zeroY);
    ctx!.fillStyle = "rgba(16, 185, 129, 0.05)";
    ctx!.fillRect(0, zeroY, w, h - zeroY);

    // Zero / target line.
    ctx!.strokeStyle = "rgba(226, 232, 240, 0.55)";
    ctx!.setLineDash([5, 5]);
    ctx!.lineWidth = 1;
    ctx!.beginPath();
    ctx!.moveTo(0, zeroY);
    ctx!.lineTo(w, zeroY);
    ctx!.stroke();
    ctx!.setLineDash([]);
    ctx!.fillStyle = "rgba(148, 163, 184, 0.9)";
    ctx!.font = "11px -apple-system, system-ui, sans-serif";
    ctx!.fillText("0 W · perfectly balanced", 8, zeroY - 6);

    if (history.length < 2) return;
    const n = history.length;
    const dx = w / (n - 1);

    // Faint context lines: household load and battery output.
    const faint = (key: "load" | "batt", color: string) => {
      ctx!.strokeStyle = color;
      ctx!.lineWidth = 1;
      ctx!.beginPath();
      history.forEach((p, i) => {
        const y = yOf(p[key], h);
        i === 0 ? ctx!.moveTo(0, y) : ctx!.lineTo(i * dx, y);
      });
      ctx!.stroke();
    };
    faint("load", "rgba(34, 211, 238, 0.35)");
    faint("batt", "rgba(139, 92, 246, 0.45)");

    // The star of the show: the grid line.  Coloured by how far off zero it is.
    ctx!.lineWidth = 2.5;
    ctx!.beginPath();
    history.forEach((p, i) => {
      const y = yOf(p.grid, h);
      i === 0 ? ctx!.moveTo(0, y) : ctx!.lineTo(i * dx, y);
    });
    const avgErr = errWindow.length
      ? errWindow.reduce((a, b) => a + Math.abs(b), 0) / errWindow.length
      : 0;
    ctx!.strokeStyle =
      avgErr < 60 ? "#10b981" : avgErr < 180 ? "#f59e0b" : "#f43f5e";
    ctx!.stroke();
  }

  function updateReadouts(): void {
    const last = history[history.length - 1];
    if (!last) return;
    if (readoutLoad) readoutLoad.textContent = `${Math.round(last.load)} W`;
    if (readoutBatt) readoutBatt.textContent = `${Math.round(last.batt)} W`;
    if (readoutGrid) {
      const g = Math.round(last.grid);
      readoutGrid.textContent = g > 0 ? `+${g} W` : `${g} W`;
    }
    const avgErr = errWindow.length
      ? errWindow.reduce((a, b) => a + Math.abs(b), 0) / errWindow.length
      : 0;
    // Map 0..300 W average error → 100..0 "steadiness".
    const score = Math.max(0, Math.min(100, 100 - (avgErr / 300) * 100));
    if (scoreBar) {
      scoreBar.style.width = `${score}%`;
      scoreBar.style.background =
        avgErr < 60 ? "var(--ok)" : avgErr < 180 ? "var(--warn)" : "var(--bad)";
    }
    if (scoreLabel) {
      scoreLabel.textContent =
        avgErr < 60 ? "Rock steady" : avgErr < 180 ? "A bit jumpy" : "Hunting hard";
    }
  }

  function tick(load: number): void {
    const grid = stepSim(sim, load, cfg());
    history.push({ grid, load, batt: sim.battOut });
    const maxPts = Math.max(120, Math.floor(canvas.clientWidth || 640));
    while (history.length > maxPts) history.shift();
    errWindow.push(grid);
    while (errWindow.length > 90) errWindow.shift();
  }

  function frame(now: number): void {
    if (running) {
      // Run a few model ticks per animation frame so the trace scrolls at a
      // lively, readable pace regardless of display refresh rate.
      if (now - lastFrame > 28) {
        lastFrame = now;
        for (let i = 0; i < 2; i++) tick(nextLoad());
        updateReadouts();
      }
      draw();
    }
    requestAnimationFrame(frame);
  }

  kettleBtn?.addEventListener("click", () => {
    gen.eventDelta = 750;
    gen.eventTicks = 90;
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
  for (let i = 0; i < METER_LATENCY + 4; i++) tick(nextLoad());
  requestAnimationFrame(frame);
}

if (typeof document !== "undefined") {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initBalancingDemo);
  } else {
    initBalancingDemo();
  }
}
