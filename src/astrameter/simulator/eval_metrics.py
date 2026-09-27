"""Per-scenario metrics computed from the samples a run records: reaction
(settle time), oscillation (overshoot, hunting), energy (grid exchange the
pack could have covered) and the cost regret against a perfect-foresight
battery, plus the downsampled chart traces stored alongside them.

Every tracking metric measures the grid against the *policy target*: 0 W for
self-consumption, and under peak shaving the grid the threshold policy asks
for (see :func:`_policy_target`), so a battery deliberately left idle below
the threshold is not scored as a tracking error."""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Iterator, Sequence
from typing import NamedTuple

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

# Capacity tariffs bill the highest mean grid import over any quarter-hour.
PEAK_WINDOW_S = 900.0

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


def _policy_target(threshold: float, net_w: float) -> float:
    """The grid (W) the controller is asked to hold for a net house demand.

    0 for self-consumption.  Under peak shaving (*threshold* > 0) the batteries
    only cover demand above the threshold and charge from surplus, so the grid
    should carry the demand itself up to the threshold, and nothing on surplus.
    """
    if threshold <= 0:
        return 0.0
    return min(max(net_w, 0.0), threshold)


Error = Callable[[_Sample], float]


def _tracking_error(scenario: Scenario) -> Error:
    """Per-sample distance of the grid from its policy target (W)."""
    threshold = scenario.peakshaving_threshold
    if threshold <= 0:
        return lambda s: s.grid
    return lambda s: s.grid - _policy_target(threshold, s.consumption)


def _settle_time(
    samples: list[_Sample], start: float, end: float, err: Error
) -> float | None:
    """Seconds from *start* until the error stays inside SETTLE_BAND_W for
    SETTLE_HOLD_S, or ``None`` if it never settles inside the window."""
    window = [s for s in samples if start <= s.t <= end]
    candidate: float | None = None
    for s in window:
        if abs(err(s)) < SETTLE_BAND_W:
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
    net AC output each step to bring the grid to its policy target when it can
    (zero, or under peak shaving the threshold policy), otherwise to store or
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
    threshold = scenario.peakshaving_threshold
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
        # Reach the target if feasible, else store/shed.
        p = min(max(net - _policy_target(threshold, net), lo), hi)
        energy_wh = max(0.0, min(cap_wh, energy_wh + (dc - p) * h))
        grid = net - p
        if grid > 0:
            import_wh += grid * h
        else:
            export_wh += -grid * h
    return _grid_cost_ct(import_wh, export_wh)


class _EventResponse(NamedTuple):
    """How the loop answered the labeled disturbances in a run."""

    settle_times: list[float]
    overshoots: list[float]
    unsettled: int
    measured: int


def _event_response(
    scenario: Scenario,
    samples: list[_Sample],
    marks: list[tuple[float, str]],
    err: Error,
) -> _EventResponse:
    """Settling time and overshoot per labeled event.

    Each event owns the window up to ``EVENT_WINDOW_S`` later, truncated by the
    next event.  An event whose initial error is already inside the settling
    band is skipped: there is no disturbance to measure a response against.
    """
    settle_times: list[float] = []
    overshoots: list[float] = []
    unsettled = 0
    measured = 0
    for idx, (t0, _label) in enumerate(marks):
        t_end = min(
            scenario.duration_s,
            t0 + EVENT_WINDOW_S,
            marks[idx + 1][0] if idx + 1 < len(marks) else float("inf"),
        )
        window = [s for s in samples if t0 <= s.t <= t_end]
        if not window:
            continue
        e0 = err(window[0])
        if abs(e0) < SETTLE_BAND_W:
            continue
        measured += 1
        sign = 1.0 if e0 > 0 else -1.0
        settle = _settle_time(samples, t0, t_end, err)
        if settle is None:
            unsettled += 1
            settle_times.append(t_end - t0)
        else:
            settle_times.append(settle)
        overshoots.append(max(0.0, max(-sign * err(s) for s in window)))
    return _EventResponse(settle_times, overshoots, unsettled, measured)


def _band_crossings(samples: list[_Sample], err: Error) -> int:
    """Times the grid swung clean through the deadband from one side to the other.

    The band is the hysteresis: brushing it does not count, only reaching the
    far side after having reached the near one.
    """
    crossings = 0
    state = 0
    for s in samples:
        e = err(s)
        if e > OSC_BAND_W:
            if state == -1:
                crossings += 1
            state = 1
        elif e < -OSC_BAND_W:
            if state == 1:
                crossings += 1
            state = -1
    return crossings


def _steady_rms(
    samples: list[_Sample], marks: list[tuple[float, str]], err: Error
) -> float:
    """RMS grid error outside the post-event transients — hunting, not reaction."""
    steady = [
        err(s)
        for s in samples
        if not any(t0 <= s.t < t0 + STEADY_EXCLUDE_S for t0, _ in marks)
    ]
    return math.sqrt(sum(g * g for g in steady) / len(steady)) if steady else 0.0


class _Integrals(NamedTuple):
    """True time averages, not sample averages skewed by staggered polls."""

    grid_rms: float
    mean_abs_grid: float
    share_imbalance: float
    battery_travel_w: float
    import_wh: float
    export_wh: float
    avoidable_import_wh: float
    avoidable_export_wh: float


def _time_weighted(
    scenario: Scenario, samples: list[_Sample], err: Error
) -> _Integrals:
    """Integrate tracking error, share imbalance, effort and grid energy over time.

    ``grid_rms`` is the whole-run L2 tracking error, transients included, whose
    effort partner is ``battery_travel_w``.  ``share_imbalance`` is the watts
    misallocated within each phase group of >=2 batteries (the sum of
    ``|power_i - fair share|``), 0 by construction with one battery per phase.
    Grid energy is split into what was exchanged and the part of the error the
    pack still had the headroom and the charge (or the room) to have covered.
    """
    specs = scenario.batteries
    phase_groups: dict[str, list[int]] = {}
    for i, spec in enumerate(specs):
        phase_groups.setdefault((spec.phase or "A").upper(), []).append(i)
    balance_groups = [grp for grp in phase_groups.values() if len(grp) >= 2]

    import_wh = export_wh = avoid_import_wh = avoid_export_wh = 0.0
    travel_w = 0.0
    grid_sq_dt = abs_grid_dt = total_dt = imbalance_dt = 0.0
    for prev, cur, dt in _intervals(samples):
        e = err(prev)
        grid_sq_dt += e * e * dt
        abs_grid_dt += abs(e) * dt
        total_dt += dt
        for grp in balance_groups:
            fair = sum(prev.powers[i] for i in grp) / len(grp)
            imbalance_dt += sum(abs(prev.powers[i] - fair) for i in grp) * dt
        wh = prev.grid * dt / 3600.0
        if wh > 0:
            import_wh += wh
        else:
            export_wh += -wh
        # Grid above its target is avoidable while any battery still has
        # discharge headroom and charge in the pack; below it, while any
        # AC-chargeable battery has charge headroom and room in the pack.
        # With a 0 W target these are plain import and export.
        err_wh = e * dt / 3600.0
        if err_wh > 0 and any(
            prev.socs[i] > SOC_EMPTY
            and prev.powers[i] < specs[i].max_discharge_power - HEADROOM_MARGIN_W
            for i in range(len(specs))
        ):
            avoid_import_wh += err_wh
        elif err_wh < 0 and any(
            specs[i].ac_chargeable
            and prev.socs[i] < SOC_FULL
            and prev.powers[i] > -specs[i].max_charge_power + HEADROOM_MARGIN_W
            for i in range(len(specs))
        ):
            avoid_export_wh += -err_wh
        travel_w += sum(abs(cur.powers[i] - prev.powers[i]) for i in range(len(specs)))

    def per_second(total: float) -> float:
        return total / total_dt if total_dt > 0 else 0.0

    return _Integrals(
        grid_rms=math.sqrt(per_second(grid_sq_dt)),
        mean_abs_grid=per_second(abs_grid_dt),
        share_imbalance=per_second(imbalance_dt),
        battery_travel_w=travel_w,
        import_wh=import_wh,
        export_wh=export_wh,
        avoidable_import_wh=avoid_import_wh,
        avoidable_export_wh=avoid_export_wh,
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _compute_metrics(
    scenario: Scenario,
    seed: int,
    samples: list[_Sample],
    marks: list[tuple[float, str]],
) -> dict:
    duration_h = scenario.duration_s / 3600.0
    err = _tracking_error(scenario)
    events = _event_response(scenario, samples, marks, err)
    integrals = _time_weighted(scenario, samples, err)

    # Sustained oscillation amplitude: the robust peak-to-peak swing (p95 - p5)
    # over the whole run. Non-zero for any continuous hunting, which the
    # step-response metrics (only fired by labeled steps) read as 0; percentiles
    # keep a single brief transient from dominating.
    all_grid = [err(s) for s in samples]
    grid_p2p = _percentile(all_grid, 0.95) - _percentile(all_grid, 0.05)

    # Money: the bill for the residual grid minus the perfect-foresight bill.
    # The oracle is a true lower bound, so the clamp only absorbs the
    # per-phase-routing slack of the three-phase scenario.
    grid_cost = _grid_cost_ct(integrals.import_wh, integrals.export_wh)
    oracle_cost = _oracle_cost_ct(scenario, samples)
    cost_regret = max(0.0, grid_cost - oracle_cost)

    # SoC extremes let a scenario verify it drove the pack into saturation
    # (not in the metric tables; for tests and context).
    all_socs = [soc for s in samples for soc in s.socs]

    return {
        "scenario": scenario.name,
        "seed": seed,
        "duration_h": round(duration_h, 3),
        "samples": len(samples),
        "soc_min": round(min(all_socs), 3) if all_socs else 0.0,
        "soc_max": round(max(all_socs), 3) if all_socs else 0.0,
        "events_measured": events.measured,
        "unsettled_events": events.unsettled,
        "settle_mean_s": round(_mean(events.settle_times), 1),
        "settle_p95_s": round(_percentile(events.settle_times, 0.95), 1),
        "overshoot_mean_w": round(_mean(events.overshoots), 1),
        "overshoot_max_w": round(max(events.overshoots), 1)
        if events.overshoots
        else 0.0,
        "band_crossings_per_h": round(_band_crossings(samples, err) / duration_h, 2),
        "grid_p2p_w": round(grid_p2p, 1),
        "grid_rms_w": round(integrals.grid_rms, 1),
        "steady_rms_w": round(_steady_rms(samples, marks, err), 1),
        "mean_abs_grid_w": round(integrals.mean_abs_grid, 1),
        "share_imbalance_w": round(integrals.share_imbalance, 1),
        "import_wh": round(integrals.import_wh, 1),
        "export_wh": round(integrals.export_wh, 1),
        "avoidable_import_wh": round(integrals.avoidable_import_wh, 1),
        "avoidable_export_wh": round(integrals.avoidable_export_wh, 1),
        "grid_cost_ct": round(grid_cost, 2),
        "oracle_cost_ct": round(oracle_cost, 2),
        "cost_regret_ct": round(cost_regret, 2),
        "battery_travel_w_per_h": round(integrals.battery_travel_w / duration_h, 0),
        **_peak_metrics(scenario, samples),
    }


def _peak_metrics(scenario: Scenario, samples: list[_Sample]) -> dict:
    """What a capacity tariff bills; 0 for a scenario without peak shaving.

    ``peak_qh_over_w`` is how far the highest quarter-hour mean import rose
    above the threshold: the billed peak the batteries were there to prevent.
    ``peak_excess_wh`` is all energy imported above the threshold, which also
    catches brief excursions a quarter-hour mean averages away.  The scenario
    sizes its pack to cover every peak, so both are 0 when steered perfectly.
    """
    threshold = scenario.peakshaving_threshold
    if threshold <= 0:
        return {"peak_qh_over_w": 0.0, "peak_excess_wh": 0.0}
    excess_wh = 0.0
    quarter_import: dict[int, float] = {}
    quarter_dt: dict[int, float] = {}
    for prev, _cur, dt in _intervals(samples):
        excess_wh += max(0.0, prev.grid - threshold) * dt / 3600.0
        k = int(prev.t // PEAK_WINDOW_S)
        quarter_import[k] = quarter_import.get(k, 0.0) + max(0.0, prev.grid) * dt
        quarter_dt[k] = quarter_dt.get(k, 0.0) + dt
    peak = max(
        (quarter_import[k] / quarter_dt[k] for k in quarter_dt if quarter_dt[k] > 0),
        default=0.0,
    )
    return {
        "peak_qh_over_w": round(max(0.0, peak - threshold), 1),
        "peak_excess_wh": round(excess_wh, 1),
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
