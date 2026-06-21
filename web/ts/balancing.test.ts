// Behavioural checks for the balancing-simulation model (balancing.ts). The toy
// is only a cartoon of the real controller, but the toggles must still *do* what
// the page claims — turning latency compensation on should visibly steady the
// grid — so we assert on tracking error over a scripted load profile. Run with:
//   tsx ts/balancing.test.ts
import { makeSim, stepSim, METER_LATENCY, type SimToggles } from "./balancing.js";

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

// A scripted household: settle at 250 W, then a sharp step to 850 W held long
// enough to expose any overshoot/hunting in the recovery.
function loadAt(tick: number): number {
  return tick < 40 ? 250 : 850;
}

/** Root-mean-square grid error over the recovery window after the step. */
function recoveryRms(cfg: SimToggles): number {
  const sim = makeSim(250);
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

// With latency compensation on, steady state should sit essentially on zero.
const settled = makeSim(250);
let lastGrid = 0;
for (let t = 0; t < 200; t++) lastGrid = stepSim(settled, 250, { ...ALL_OFF, predictor: true });
ok(Math.abs(lastGrid) < 5, `steady-state grid converges to ~0 (got ${lastGrid.toFixed(1)} W)`);

// The model must not crash for short runs and the latency constant must be the
// positive lag that creates the problem in the first place.
ok(METER_LATENCY > 0, "meter latency is a positive lag");

console.log("\n" + (failures ? `${failures} FAILED` : "ALL PASSED"));
process.exit(failures ? 1 : 0);
