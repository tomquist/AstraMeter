"""Per-scenario metrics computed from the samples a run records: reaction
(settle time), oscillation (overshoot, hunting), energy (grid exchange the
pack could have covered) and the cost regret against a perfect-foresight
battery, plus the downsampled chart traces stored alongside them."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterator, Sequence

from .eval_spec import Scenario, _Sample

# |grid| below this counts as "settled" (just above the battery's own
# ±20 W deadband, matching the main e2e convergence assertion).
SETTLE_BAND_W = 25.0
# The grid must stay inside SETTLE_BAND_W for this long to count as settled.
SETTLE_HOLD_S = 10.0
# Settling/overshoot are measured in a window after each labeled event,
# truncated by the next labeled event.
EVENT_WINDOW_S = 600.0
# Oscillation counting uses the battery's deadband as hysteresis band.
OSC_BAND_W = 20.0
# Samples within this long after a labeled event are excluded from the
# steady-state RMS (they're legitimate transients, not hunting).
STEADY_EXCLUDE_S = 120.0
# Headroom margin when deciding whether grid exchange was "avoidable".
HEADROOM_MARGIN_W = 5.0
SOC_EMPTY = 0.02
SOC_FULL = 0.98
# Longest interval one sample is allowed to stand for when integrating over
# time: a wider gap is a stalled poll, not elapsed household time.
_MAX_SAMPLE_GAP_S = 5.0

# Flat EU-typical tariffs pricing the residual grid exchange in eurocents:
# import paid at retail, export earning the much lower feed-in rate. The
# asymmetry is what makes a controller that silently exports stored energy show
# up as money lost. Cost keys are rounded to 2 dp everywhere (see
# ``_metric_ndp``) because regret clusters at fractions of a cent.
RETAIL_CT_PER_KWH = 30.0
FEEDIN_CT_PER_KWH = 8.0

# Points each trace is downsampled to for the charts. Base and head share the
# count so the two lines align by index regardless of poll cadence.
GRAPH_POINTS = 1800


def _percentile(values: Sequence[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    k = (len(ordered) - 1) * pct
    lo = math.floor(k)
    hi = math.ceil(k)
    if lo == hi:
        return ordered[lo]
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _settle_time(samples: list[_Sample], start: float, end: float) -> float | None:
    """Seconds from *start* until |grid| stays inside SETTLE_BAND_W for
    SETTLE_HOLD_S, or ``None`` if it never settles inside the window."""
    window = [s for s in samples if start <= s.t <= end]
    candidate: float | None = None
    for s in window:
        if abs(s.grid) < SETTLE_BAND_W:
            if candidate is None:
                candidate = s.t
            if s.t - candidate >= SETTLE_HOLD_S:
                return candidate - start
        else:
            candidate = None
    # A quiet tail shorter than the hold still counts when the window ends.
    if candidate is not None and window and window[-1].t - candidate >= SETTLE_HOLD_S:
        return candidate - start
    return None


def _intervals(samples: list[_Sample]) -> Iterator[tuple[_Sample, _Sample, float]]:
    """Consecutive sample pairs with the seconds between them, capped at
    ``_MAX_SAMPLE_GAP_S``; a non-positive gap is skipped."""
    for prev, cur in itertools.pairwise(samples):
        dt = min(cur.t - prev.t, _MAX_SAMPLE_GAP_S)
        if dt <= 0:
            continue
        yield prev, cur, dt


def _grid_cost_ct(import_wh: float, export_wh: float) -> float:
    """Electricity bill (eurocents) for a residual grid exchange; negative when
    the feed-in credit exceeds the import cost."""
    return (RETAIL_CT_PER_KWH * import_wh - FEEDIN_CT_PER_KWH * export_wh) / 1000.0


def _oracle_cost_ct(scenario: Scenario, samples: list[_Sample]) -> float:
    """Cost (eurocents) of a perfect-foresight dispatch, the floor the
    controller is benchmarked against.

    One lossless aggregate battery (summed capacity and power limits) picks its
    net AC output each step to zero the grid when it can, otherwise to store or
    shed the residual. DC-input solar enters the cells directly, offsetting the
    grid first and passing through to export once the pack is full. Under a
    flat tariff with ``retail >= feed-in`` this greedy dispatch is optimal, so
    what it leaves on the grid is physically irreducible and
    ``actual - oracle`` is the controller's own loss. Per-phase routing is
    ignored (one battery for the fleet), so in the three-phase scenario the
    oracle is mildly optimistic — still a valid lower bound.
    """
    specs = scenario.batteries
    cap_wh = sum(s.capacity_wh for s in specs)
    max_charge = sum(s.max_charge_power for s in specs)
    max_discharge = sum(s.max_discharge_power for s in specs)
    energy_wh = sum(s.initial_soc * s.capacity_wh for s in specs)
    import_wh = export_wh = 0.0
    for prev, _cur, dt in _intervals(samples):
        h = dt / 3600.0
        net = prev.consumption  # >0 deficit (would import), <0 surplus (export)
        dc = prev.dc_input  # free DC-side solar entering the pack this step
        # Feasible net AC output p (positive = to house/grid): the cells change
        # by (dc - p), bounded by stored energy and room; p by the AC limits.
        hi = min(max_discharge, dc + energy_wh / h)
        lo = max(-max_charge, dc - (cap_wh - energy_wh) / h)
        if lo > hi:  # DC inflow exceeds what a full pack can shed via the inverter
            lo = hi  # → curtail the excess (output at the cap)
        p = min(max(net, lo), hi)  # zero the grid if feasible, else store/shed
        energy_wh = max(0.0, min(cap_wh, energy_wh + (dc - p) * h))
        grid = net - p
        if grid > 0:
            import_wh += grid * h
        else:
            export_wh += -grid * h
    return _grid_cost_ct(import_wh, export_wh)


def _compute_metrics(
    scenario: Scenario,
    seed: int,
    samples: list[_Sample],
    marks: list[tuple[float, str]],
) -> dict:
    duration_h = scenario.duration_s / 3600.0
    specs = scenario.batteries

    # Per-event settling & overshoot.
    settle_times: list[float] = []
    overshoots: list[float] = []
    unsettled = 0
    events_measured = 0
    for idx, (t0, _label) in enumerate(marks):
        t_end = min(
            scenario.duration_s,
            t0 + EVENT_WINDOW_S,
            marks[idx + 1][0] if idx + 1 < len(marks) else float("inf"),
        )
        window = [s for s in samples if t0 <= s.t <= t_end]
        if not window:
            continue
        e0 = window[0].grid
        if abs(e0) < SETTLE_BAND_W:
            continue  # disturbance too small to measure against the band
        events_measured += 1
        sign = 1.0 if e0 > 0 else -1.0
        settle = _settle_time(samples, t0, t_end)
        if settle is None:
            unsettled += 1
            settle_times.append(t_end - t0)
        else:
            settle_times.append(settle)
        overshoots.append(max(0.0, max(-sign * s.grid for s in window)))

    # Oscillation: hysteresis band crossings.
    crossings = 0
    state = 0
    for s in samples:
        if s.grid > OSC_BAND_W:
            if state == -1:
                crossings += 1
            state = 1
        elif s.grid < -OSC_BAND_W:
            if state == 1:
                crossings += 1
            state = -1

    # Steady-state RMS, outside post-event transients.
    def in_transient(t: float) -> bool:
        return any(t0 <= t < t0 + STEADY_EXCLUDE_S for t0, _ in marks)

    steady = [s.grid for s in samples if not in_transient(s.t)]
    steady_rms = math.sqrt(sum(g * g for g in steady) / len(steady)) if steady else 0.0

    # Sustained oscillation amplitude: the robust peak-to-peak swing (p95 - p5)
    # over the whole run. Non-zero for any continuous hunting, which the
    # step-response metrics (only fired by labeled steps) read as 0; percentiles
    # keep a single brief transient from dominating.
    all_grid = [s.grid for s in samples]
    grid_p2p = _percentile(all_grid, 0.95) - _percentile(all_grid, 0.05)

    # Time-weighted integrals (true time averages, not sample averages skewed by
    # staggered polls): grid_rms is the whole-run L2 tracking error, transients
    # included, whose effort partner is battery_travel; share_imbalance is the
    # watts misallocated within each phase group of >=2 batteries (sum of
    # |power_i - fair share|), 0 by construction with one battery per phase.
    phase_groups: dict[str, list[int]] = {}
    for i, sp in enumerate(specs):
        phase_groups.setdefault((sp.phase or "A").upper(), []).append(i)
    balance_groups = [grp for grp in phase_groups.values() if len(grp) >= 2]

    import_wh = export_wh = avoid_import_wh = avoid_export_wh = 0.0
    travel_w = 0.0
    grid_sq_dt = abs_grid_dt = total_dt = 0.0
    imbalance_dt = 0.0
    for prev, cur, dt in _intervals(samples):
        grid_sq_dt += prev.grid * prev.grid * dt
        abs_grid_dt += abs(prev.grid) * dt
        total_dt += dt
        for grp in balance_groups:
            fair = sum(prev.powers[i] for i in grp) / len(grp)
            imbalance_dt += sum(abs(prev.powers[i] - fair) for i in grp) * dt
        wh = prev.grid * dt / 3600.0
        if wh > 0:
            import_wh += wh
            # Import is avoidable while any battery still has discharge
            # headroom and charge in the pack.
            if any(
                prev.socs[i] > SOC_EMPTY
                and prev.powers[i] < specs[i].max_discharge_power - HEADROOM_MARGIN_W
                for i in range(len(specs))
            ):
                avoid_import_wh += wh
        else:
            export_wh += -wh
            # Export is avoidable while any AC-chargeable battery has charge
            # headroom and room in the pack.
            if any(
                specs[i].ac_chargeable
                and prev.socs[i] < SOC_FULL
                and prev.powers[i] > -specs[i].max_charge_power + HEADROOM_MARGIN_W
                for i in range(len(specs))
            ):
                avoid_export_wh += -wh
        travel_w += sum(abs(cur.powers[i] - prev.powers[i]) for i in range(len(specs)))

    grid_rms = math.sqrt(grid_sq_dt / total_dt) if total_dt > 0 else 0.0
    mean_abs = abs_grid_dt / total_dt if total_dt > 0 else 0.0
    share_imbalance = imbalance_dt / total_dt if total_dt > 0 else 0.0

    # Money: the bill for the residual grid minus the perfect-foresight bill.
    # The oracle is a true lower bound, so the clamp only absorbs the
    # per-phase-routing slack of the three-phase scenario.
    grid_cost = _grid_cost_ct(import_wh, export_wh)
    oracle_cost = _oracle_cost_ct(scenario, samples)
    cost_regret = max(0.0, grid_cost - oracle_cost)

    # SoC extremes let a scenario verify it drove the pack into saturation
    # (not in the metric tables; for tests and context).
    all_socs = [soc for s in samples for soc in s.socs]
    soc_min = round(min(all_socs), 3) if all_socs else 0.0
    soc_max = round(max(all_socs), 3) if all_socs else 0.0

    return {
        "scenario": scenario.name,
        "seed": seed,
        "duration_h": round(duration_h, 3),
        "samples": len(samples),
        "soc_min": soc_min,
        "soc_max": soc_max,
        "events_measured": events_measured,
        "unsettled_events": unsettled,
        "settle_mean_s": round(sum(settle_times) / len(settle_times), 1)
        if settle_times
        else 0.0,
        "settle_p95_s": round(_percentile(settle_times, 0.95), 1),
        "overshoot_mean_w": round(sum(overshoots) / len(overshoots), 1)
        if overshoots
        else 0.0,
        "overshoot_max_w": round(max(overshoots), 1) if overshoots else 0.0,
        "band_crossings_per_h": round(crossings / duration_h, 2),
        "grid_p2p_w": round(grid_p2p, 1),
        "grid_rms_w": round(grid_rms, 1),
        "steady_rms_w": round(steady_rms, 1),
        "mean_abs_grid_w": round(mean_abs, 1),
        "share_imbalance_w": round(share_imbalance, 1),
        "import_wh": round(import_wh, 1),
        "export_wh": round(export_wh, 1),
        "avoidable_import_wh": round(avoid_import_wh, 1),
        "avoidable_export_wh": round(avoid_export_wh, 1),
        "grid_cost_ct": round(grid_cost, 2),
        "oracle_cost_ct": round(oracle_cost, 2),
        "cost_regret_ct": round(cost_regret, 2),
        "battery_travel_w_per_h": round(travel_w / duration_h, 0),
    }


def _downsample_series(
    samples: list[_Sample],
    duration_s: float,
    pick: Callable[[_Sample], float],
    n: int = GRAPH_POINTS,
) -> list[float]:
    """Bucket a per-sample value into *n* evenly spaced means over the run.

    *pick* selects the value from each sample (grid, a battery's power, ...).
    Empty buckets carry the previous value forward so the chart has no gaps;
    the fixed length lets traces from different runs overlay by index.
    """
    if not samples or duration_s <= 0 or n <= 0:
        return []
    buckets: list[list[float]] = [[] for _ in range(n)]
    for s in samples:
        idx = min(int(s.t / duration_s * n), n - 1)
        buckets[idx].append(pick(s))
    out: list[float] = []
    last = 0.0
    for bucket in buckets:
        if bucket:
            last = sum(bucket) / len(bucket)
        out.append(round(last, 1))
    return out


def _battery_power(i: int) -> Callable[[_Sample], float]:
    """Picker for battery *i*'s output (a typed closure, so the per-battery
    downsampling avoids an inline lambda mypy can't infer)."""
    return lambda s: s.powers[i]


def _chart_traces(scenario: Scenario, samples: list[_Sample]) -> dict:
    """The downsampled series the charts draw, stored alongside the metrics.

    Consumption comes straight from the load model, so it cannot carry
    control-loop oscillation; it is the same scripted load in base and head,
    so one trace is enough and the grid chart overlays it as context."""
    specs = scenario.batteries
    return {
        "grid_trace": _downsample_series(
            samples, scenario.duration_s, lambda s: s.grid
        ),
        "consumption_trace": _downsample_series(
            samples, scenario.duration_s, lambda s: s.consumption
        ),
        "battery_labels": [
            f"B{i + 1} {specs[i].device_type}" for i in range(len(specs))
        ],
        "battery_traces": [
            _downsample_series(samples, scenario.duration_s, _battery_power(i))
            for i in range(len(specs))
        ],
    }
