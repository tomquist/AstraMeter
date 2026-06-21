// Behavioural checks for the balancing-simulation model (balancing.ts). The toy
// is only a cartoon of the real controller, but the claims the page makes must
// hold: AstraMeter's smart steering tracks far better than a plain
// meter-follower, latency compensation is what does the heavy lifting, and the
// smart controller settles instead of hunting. Run with:
//   tsx ts/balancing.test.ts
import {
  makeSim,
  stepSim,
  makeNaive,
  stepNaive,
  lagSignal,
  LAG_SAMPLES,
  METER_LATENCY,
  SMART,
  type SimToggles,
} from "./balancing.js";

let failures = 0;
function ok(cond: boolean, msg: string): void {
  if (!cond) {
    failures++;
    console.error("✗ " + msg);
  } else {
    console.log("✓ " + msg);
  }
}

const ALL_OFF: SimToggles = { predictor: false, pacing: false, damping: false };

// A scripted household: settle, then a sharp step held long enough to expose
// any overshoot/hunting in the recovery.
function loadAt(tick: number): number {
  return tick < 40 ? 550 : 1050;
}

/** Root-mean-square grid error over the recovery window after the step. */
function recoveryRms(cfg: SimToggles): number {
  const sim = makeSim(550);
  let sumSq = 0;
  let count = 0;
  for (let t = 0; t < 220; t++) {
    const grid = stepSim(sim, loadAt(t), cfg);
    if (t >= 40) {
      sumSq += grid * grid;
      count++;
    }
  }
  return Math.sqrt(sumSq / count);
}

const rmsRaw = recoveryRms(ALL_OFF);
const rmsPredict = recoveryRms({ ...ALL_OFF, predictor: true });
const rmsPace = recoveryRms({ ...ALL_OFF, pacing: true });

ok(
  rmsPredict < rmsRaw,
  `latency compensation reduces tracking error (raw ${rmsRaw.toFixed(0)} → predicted ${rmsPredict.toFixed(0)})`,
);
ok(
  rmsPace < rmsRaw,
  `smooth ramping reduces tracking error (raw ${rmsRaw.toFixed(0)} → paced ${rmsPace.toFixed(0)})`,
);

// The headline of the page: AstraMeter (all tricks on) vastly out-tracks a plain
// meter-follower over the same load profile.
function rms(values: number[]): number {
  return Math.sqrt(values.reduce((a, b) => a + b * b, 0) / values.length);
}
const smart = makeSim(550);
const naive = makeNaive(550);
const smartGrid: number[] = [];
const naiveGrid: number[] = [];
for (let t = 0; t < 320; t++) {
  const sg = stepSim(smart, loadAt(t), SMART);
  const ng = stepNaive(naive, loadAt(t));
  if (t >= 60) {
    smartGrid.push(sg);
    naiveGrid.push(ng);
  }
}
const smartRms = rms(smartGrid);
const naiveRms = rms(naiveGrid);
ok(
  naiveRms > 4 * smartRms,
  `AstraMeter out-tracks a plain meter-follower (AstraMeter RMS ${smartRms.toFixed(0)} vs plain ${naiveRms.toFixed(0)})`,
);

// With latency compensation on, steady state should sit essentially on zero.
const settled = makeSim(250);
let lastGrid = 0;
for (let t = 0; t < 200; t++) lastGrid = stepSim(settled, 250, SMART);
ok(Math.abs(lastGrid) < 5, `steady-state grid converges to ~0 (got ${lastGrid.toFixed(1)} W)`);

// Regression guard: from a mismatched start the smart controller must *converge
// and hold* against a constant load — not limit-cycle around zero, which is
// exactly the bug that made the demo "oscillate wildly". Battery starts at
// 250 W, load is a constant 700 W, so it has to ramp up and settle.
const hold = makeSim(250);
let tailSumSq = 0;
let tailCount = 0;
let flips = 0;
let prevSign = 0;
for (let t = 0; t < 300; t++) {
  const grid = stepSim(hold, 700, SMART);
  if (t >= 200) {
    tailSumSq += grid * grid;
    tailCount++;
    const s = grid > 0 ? 1 : grid < 0 ? -1 : 0;
    if (s !== 0 && prevSign !== 0 && s !== prevSign) flips++;
    if (s !== 0) prevSign = s;
  }
}
const tailRms = Math.sqrt(tailSumSq / tailCount);
ok(tailRms < 25, `smart controller holds a constant load steady (tail RMS ${tailRms.toFixed(1)} W < 25)`);
ok(flips <= 2, `smart controller does not hunt at steady state (${flips} sign reversals over 100 ticks)`);

// The plain follower, by contrast, must visibly hunt against the same constant
// load (this is the failure the demo exists to show).
const naiveHold = makeNaive(250);
let nFlips = 0;
let nPrev = 0;
for (let t = 0; t < 300; t++) {
  const grid = stepNaive(naiveHold, 700);
  if (t >= 200) {
    const s = grid > 0 ? 1 : grid < 0 ? -1 : 0;
    if (s !== 0 && nPrev !== 0 && s !== nPrev) nFlips++;
    if (s !== 0) nPrev = s;
  }
}
ok(nFlips >= 5, `plain meter-follower hunts at steady state (${nFlips} sign reversals over 100 ticks)`);

ok(METER_LATENCY > 0, "meter latency is a positive lag");

// The lag explainer's scripted bump: bounded to 0..1, periodic, and the delayed
// copy actually trails reality on the rising edge (so the offset is visible).
let lagMin = Infinity;
let lagMax = -Infinity;
for (let p = 0; p < 260; p++) {
  const v = lagSignal(p);
  lagMin = Math.min(lagMin, v);
  lagMax = Math.max(lagMax, v);
}
ok(lagMin >= 0 && lagMax <= 1 && lagMax > 0.99, `lag signal is a 0..1 bump (min ${lagMin}, max ${lagMax})`);
ok(lagSignal(5) === lagSignal(5 + 260), "lag signal repeats each cycle");
// On the rising edge, the delayed meter copy is still below reality.
const edge = 95;
ok(
  lagSignal(edge) > lagSignal(edge - LAG_SAMPLES),
  `delayed meter trails reality on the rising edge (${lagSignal(edge).toFixed(2)} > ${lagSignal(edge - LAG_SAMPLES).toFixed(2)})`,
);

console.log("\n" + (failures ? `${failures} FAILED` : "ALL PASSED"));
process.exit(failures ? 1 : 0);
