---
name: steering-eval
description: A change to the active-control loop needs a before/after simulation to show it did not make steering worse — CI runs one on every such PR and posts the comparison, so a regression surfaces publicly either way. Read before changing balancer behavior in src/astrameter/ct002/ or src/astrameter/simulator/: how to capture a baseline, and how to read cost_regret_ct and the guardrail flags.
---

# Steering-quality evaluation

```bash
uv run python -m astrameter.simulator.evaluation
```

Runs the scenario catalogue against the firmware-accurate battery plant,
simulating hours of realistic household activity. Each scenario runs over
several seeds (`--seeds`, default 5; `--seed N` sets the first) in parallel
across cores, and every metric is the mean over those seeds — so the figures are
a seed-averaged signal, not one noisy draw. `--seeds 1` for a quick look.

`evaluation.py` is the CLI; the work lives in `eval_spec.py` (types),
`eval_scenarios.py` (the catalogue), `eval_harness.py` (running one scenario),
`eval_metrics.py` (every metric) and `eval_compare.py` / `eval_report.py` (the
comparison and the HTML report). The names CI and the tests import stay
re-exported from `evaluation`.

## Comparing a change

Capture a baseline on the unchanged code, re-run after the change, compare:

```bash
uv run python -m astrameter.simulator.evaluation --json base.json     # before
uv run python -m astrameter.simulator.evaluation --json head.json     # after
uv run python -m astrameter.simulator.evaluation --input head.json --compare base.json
```

CI runs the same suite on PR base + head (job `steering-eval`) and posts a
sticky comparison. It costs a runner per scenario twice over, so
`steering-eval-gate` keeps it off pushes entirely, and off any PR whose diff
touches neither `src/astrameter/ct002/` nor `src/astrameter/simulator/`
(`*_test.py` under those doesn't count) nor `.github/workflows/ci.yml` — label a
PR `steering-eval` to force a run when a change steers from somewhere else.

## Reading the comparison

It leads with an **aggregate roll-up** — the per-metric mean across all
scenarios plus a one-line verdict — so an across-the-board move is visible
without reading every table. Below it sits a **priority verdict**: a
value-weighted score (`_METRIC_WEIGHTS` in `eval_compare.py`) that hard-flags
any do-no-harm guardrail (`_GUARDRAIL_METRICS`) regressing past 5%, or appearing
from a zero base. Read the flat mean for "did most numbers move down?" and the
priority verdict for "did it improve where it matters, and did it break a
guardrail?".

The headline metric is **`cost_regret_ct`**: the controller's electricity bill
(eurocents, asymmetric tariff — import at `RETAIL_CT_PER_KWH`, export at
`FEEDIN_CT_PER_KWH`) minus what a perfect-foresight optimal battery would have
paid on the same load (`_oracle_cost_ct`, a lossless greedy aggregate battery,
provably optimal under a flat tariff). It is the one ungameable "money left on
the table" number: 0 means it matched the optimum, and the irreducible cost when
the pack saturates is subtracted out, so regret is purely controllable loss.

**`grid_rms_w`** is the whole-run L2 tracking error — control quality,
transients included — paired with `battery_travel_w_per_h` as the effort term.
The two LQR terms are kept separate rather than fused with an arbitrary weight.
