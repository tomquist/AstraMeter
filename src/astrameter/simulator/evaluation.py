"""Steering-quality evaluation harness for the active-control loop.

Wires the full closed loop — :class:`~astrameter.ct002.ct002.CT002` (active
control) → :class:`~astrameter.simulator.battery.BatterySimulator` (real
firmware steering laws) → :class:`~astrameter.simulator.load_model.LoadModel`
→ :class:`~astrameter.simulator.powermeter_sim.PowermeterSimulator` — under a
mock clock, so hours of simulated household activity (load spikes, solar,
single / multiple / mixed batteries) run in seconds of wall time.

Each scenario produces metrics answering three questions (issue #458):

* **Reaction** — how fast does the loop settle after a load/solar step?
* **Oscillation** — how much does it overshoot and hunt around the null?
* **Energy** — how many Wh leak to/from the grid that a battery with
  headroom could have covered?

Each scenario is run over several seeds (``--seeds``, default 5) **in parallel
across CPU cores** and every metric is reported as the mean over those seeds,
so the figures reflect the seed-averaged signal rather than one noisy draw.
Process isolation (not asyncio) is what parallelizes the CPU-bound sim and, as
a bonus, gives each seed its own global ``random`` state so runs stay
deterministic. Per-scenario rows are then rolled up into a single cross-scenario
``AGGREGATE`` so an across-the-board win or regression is visible at a glance.

Run the suite (from the repo root, with dev deps)::

    uv run python -m astrameter.simulator.evaluation
    uv run python -m astrameter.simulator.evaluation --scenario two_venus/fair \\
        --set balance_deadband=25 --seeds 10 --json head.json
    uv run python -m astrameter.simulator.evaluation --compare base.json \\
        --input head.json

``--compare`` renders a Markdown before/after table — leading with the
seed-averaged ``AGGREGATE`` roll-up — and CI runs the suite on the PR base and
head and posts that comparison as a sticky PR comment (see
``.github/workflows/ci.yml``, job ``steering-eval``). The interactive
grid-power charts live in the self-contained HTML report CI uploads as the
``steering-eval`` artifact.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
import os
import random
import socket
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field

from astrameter.ct002.balancer import device_capabilities
from astrameter.ct002.ct002 import CT002

from .battery import BatterySimulator
from .load_model import Load, LoadModel
from .powermeter_sim import PowermeterSimulator

# Mock-time epoch all scenarios start from (any fixed value works; using a
# constant keeps runs bit-for-bit reproducible across machines).
_EPOCH = 1_750_000_000.0

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
# Number of points each scenario's grid-power trace is downsampled to for the
# interactive charts in the HTML report. Base and head share this fixed count
# so the two lines align by index regardless of poll cadence.
GRAPH_POINTS = 1800
# Resolution of the half-sine solar day curve (s).  Fine enough that each step
# is a few watts (vs ~50 W per minute at the steepest part of the sine), so the
# controller sees a smooth ramp rather than a minutely staircase of load steps.
SOLAR_CURVE_STEP_S = 2


# ---------------------------------------------------------------------------
# Scenario definition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BatterySpec:
    """Static description of one simulated battery in a scenario."""

    device_type: str = "HMG-50"
    phase: str = "A"
    max_charge_power: int = 2500
    max_discharge_power: int = 2500
    capacity_wh: float = 5120.0
    initial_soc: float = 0.6
    ramp_rate: float = 400.0
    poll_interval: float = 1.0
    startup_delay: float = 2.0
    min_power_threshold: float = 5.0
    max_dc_input: int = 0

    @property
    def ac_chargeable(self) -> bool:
        return device_capabilities(self.device_type).has_ac_input


@dataclass(frozen=True)
class Event:
    """A scheduled world mutation.

    A non-empty *label* marks a step disturbance whose settling/overshoot is
    measured; unlabeled events (e.g. the per-minute solar curve) only mutate
    the world.
    """

    at: float
    apply: Callable[[EvalWorld], None]
    label: str = ""


@dataclass
class Scenario:
    name: str
    description: str
    batteries: list[BatterySpec]
    duration_s: float
    build_events: Callable[[random.Random], list[Event]]
    base_load: list[float] = field(default_factory=lambda: [300.0, 0.0, 0.0])
    base_noise: float = 10.0
    loads: list[Load] = field(default_factory=list)
    ct_kwargs: dict[str, float] = field(default_factory=dict)
    # Real grid meters report with latency; the controller acts on a reading
    # refreshed at this cadence (matching a typical ~1 s powermeter poll /
    # THROTTLE_INTERVAL) while the metrics see the true instantaneous grid.
    meter_interval_s: float = 1.0
    # Transport/measurement delay on top of the refresh interval: the value the
    # controller reads reflects the true grid as it was this many seconds ago
    # (a P1 dongle / HA push sensor measures, then takes time to arrive). Acting
    # on a stale reading is a classic driver of sustained oscillation, so this
    # is what reproduces a loop that hunts instead of settling. 0 = no delay.
    meter_latency_s: float = 0.0


@dataclass
class EvalWorld:
    """Mutable world handle passed to scenario events."""

    load_model: LoadModel
    batteries: list[BatterySimulator]
    # Solar is curve x factor so labeled transients (cloud dips) compose with
    # the unlabeled day curve instead of being overwritten by its next tick.
    solar_curve_w: float = 0.0
    solar_factor: float = 1.0

    def set_load(self, name: str, active: bool) -> None:
        for ld in self.load_model.loads:
            if ld.name == name:
                ld.active = active
                return
        raise KeyError(f"no load named {name!r}")

    def set_solar_curve(self, watts: float) -> None:
        self.solar_curve_w = watts
        self._apply_solar()

    def set_solar_factor(self, factor: float) -> None:
        self.solar_factor = factor
        self._apply_solar()

    def _apply_solar(self) -> None:
        self.load_model.set_solar(self.solar_curve_w * self.solar_factor)

    def set_dc_input(self, battery_index: int, watts: float) -> None:
        self.batteries[battery_index].dc_input_power = watts


class _EvalClock:
    """Monotonic settable mock clock (same shape as the e2e HarnessClock)."""

    def __init__(self, start: float) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def set(self, value: float) -> None:
        if value > self._now:
            self._now = value


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Sample:
    t: float  # seconds since scenario start
    grid: float  # grid total W as seen by the controller (before_send)
    powers: tuple[float, ...]
    socs: tuple[float, ...]


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def run_scenario(
    scenario: Scenario,
    seed: int = 1,
    overrides: dict[str, float] | None = None,
) -> dict:
    """Run *scenario* deterministically and return its metrics dict."""
    # The LoadModel draws noise from the global ``random``; seed it so each
    # run is reproducible.  Event schedules use an independent stream so
    # adding noise samples never shifts the scripted timeline.
    random.seed(seed)
    rng = random.Random(seed + 1)
    events = sorted(scenario.build_events(rng), key=lambda e: e.at)

    clock = _EvalClock(_EPOCH)
    load_model = LoadModel(
        base_load=list(scenario.base_load),
        base_noise=scenario.base_noise,
        loads=[Load(ld.name, ld.power, ld.phase) for ld in scenario.loads],
    )
    ct_mac = "112233445566"
    ct_port = _free_udp_port()
    batteries = [
        BatterySimulator(
            mac=f"02B250{i + 1:06X}",
            phase=spec.phase,
            ct_mac=ct_mac,
            ct_host="127.0.0.1",
            ct_port=ct_port,
            meter_dev_type=spec.device_type,
            max_charge_power=spec.max_charge_power,
            max_discharge_power=spec.max_discharge_power,
            capacity_wh=spec.capacity_wh,
            initial_soc=spec.initial_soc,
            ramp_rate=spec.ramp_rate,
            poll_interval=spec.poll_interval,
            min_power_threshold=spec.min_power_threshold,
            startup_delay=spec.startup_delay,
            max_dc_input=spec.max_dc_input,
        )
        for i, spec in enumerate(scenario.batteries)
    ]
    powermeter = PowermeterSimulator(batteries=batteries, load_model=load_model, port=0)
    world = EvalWorld(load_model=load_model, batteries=batteries)

    ct_kwargs: dict[str, float] = dict(scenario.ct_kwargs)
    ct_kwargs.update(overrides or {})
    ct002 = CT002(
        udp_port=ct_port,
        ct_mac=ct_mac,
        active_control=True,
        clock=clock,
        consumer_ttl=10_000_000,  # mock time spans hours; never evict
        dedupe_time_window=0.0,
        **ct_kwargs,
    )

    samples: list[_Sample] = []
    # The controller reads the meter at the meter's own cadence (stale in
    # between, like a real powermeter poll); metrics record the true grid.
    # ``grid_history`` keeps recent true readings so a refresh can serve the
    # value as it was ``meter_latency_s`` ago (transport/measurement delay).
    meter_cache: dict[str, float] = {}
    meter_read_at = [-math.inf]
    grid_history: list[tuple[float, dict[str, float]]] = []

    async def before_send(_addr, _fields=None, _consumer_id=None):
        now = clock() - _EPOCH
        true_grid = powermeter.compute_grid()
        grid_history.append((now, true_grid))
        # Drop history older than what the delayed read can still need.
        horizon = now - scenario.meter_latency_s - scenario.meter_interval_s - 1.0
        while len(grid_history) > 1 and grid_history[0][0] < horizon:
            grid_history.pop(0)
        if now - meter_read_at[0] >= scenario.meter_interval_s:
            # Serve the reading as it was meter_latency_s ago (zero-order hold
            # on the history: the most recent sample at or before target_t).
            target_t = now - scenario.meter_latency_s
            delayed = grid_history[0][1]
            for ht, hg in grid_history:
                if ht <= target_t:
                    delayed = hg
                else:
                    break
            meter_cache.clear()
            meter_cache.update(delayed)
            meter_read_at[0] = now
        samples.append(
            _Sample(
                t=now,
                grid=true_grid["phase_a"] + true_grid["phase_b"] + true_grid["phase_c"],
                powers=tuple(b.current_power for b in batteries),
                socs=tuple(b.soc for b in batteries),
            )
        )
        return [
            meter_cache["phase_a"],
            meter_cache["phase_b"],
            meter_cache["phase_c"],
        ]

    ct002.before_send = before_send
    await ct002.start()
    try:
        # Event-driven schedule: each battery polls on its own cadence
        # (staggered starts), scripted events fire in between.
        next_poll = [0.5 + i * 0.131 for i in range(len(batteries))]
        marks: list[tuple[float, str]] = []
        event_idx = 0
        while True:
            i = min(range(len(batteries)), key=lambda k: next_poll[k])
            t_next = next_poll[i]
            if t_next > scenario.duration_s:
                break
            while event_idx < len(events) and events[event_idx].at <= t_next:
                ev = events[event_idx]
                clock.set(_EPOCH + ev.at)
                ev.apply(world)
                if ev.label:
                    marks.append((ev.at, ev.label))
                event_idx += 1
            clock.set(_EPOCH + t_next)
            await batteries[i].step(batteries[i].poll_interval)
            next_poll[i] = t_next + batteries[i].poll_interval
    finally:
        await ct002.stop()

    return _compute_metrics(scenario, seed, samples, marks)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


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


def _battery_pick(i: int) -> Callable[[_Sample], float]:
    """Return a picker for battery *i*'s output (a typed closure, so the
    per-battery downsampling avoids an inline lambda mypy can't infer)."""
    return lambda s: s.powers[i]


def _compute_metrics(
    scenario: Scenario,
    seed: int,
    samples: list[_Sample],
    marks: list[tuple[float, str]],
) -> dict:
    duration_h = scenario.duration_s / 3600.0
    specs = scenario.batteries

    # --- per-event settling & overshoot ---
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

    # --- oscillation: hysteresis band crossings ---
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

    # --- steady-state RMS (outside post-event transients) ---
    def in_transient(t: float) -> bool:
        return any(t0 <= t < t0 + STEADY_EXCLUDE_S for t0, _ in marks)

    steady = [s.grid for s in samples if not in_transient(s.t)]
    steady_rms = math.sqrt(sum(g * g for g in steady) / len(steady)) if steady else 0.0
    mean_abs = sum(abs(s.grid) for s in samples) / len(samples) if samples else 0.0

    # --- sustained oscillation amplitude ---
    # The robust peak-to-peak grid swing (p95 - p5) over the whole run. Unlike
    # the step-response metrics (settle/overshoot, which only fire on labelled
    # load steps and read 0 for a continuously hunting loop), this is non-zero
    # for *any* sustained oscillation and grades it directly: a loop that holds
    # zero scores ~0, one that constantly swings ±X scores ~2X. Percentiles (not
    # min/max) keep a single brief transient from dominating.
    all_grid = [s.grid for s in samples]
    grid_p2p = _percentile(all_grid, 0.95) - _percentile(all_grid, 0.05)

    # --- energy & battery travel ---
    import_wh = export_wh = avoid_import_wh = avoid_export_wh = 0.0
    travel_w = 0.0
    for prev, cur in itertools.pairwise(samples):
        dt = min(cur.t - prev.t, 5.0)
        if dt <= 0:
            continue
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

    return {
        "scenario": scenario.name,
        "seed": seed,
        "duration_h": round(duration_h, 3),
        "samples": len(samples),
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
        "steady_rms_w": round(steady_rms, 1),
        "mean_abs_grid_w": round(mean_abs, 1),
        "import_wh": round(import_wh, 1),
        "export_wh": round(export_wh, 1),
        "avoidable_import_wh": round(avoid_import_wh, 1),
        "avoidable_export_wh": round(avoid_export_wh, 1),
        "battery_travel_w_per_h": round(travel_w / duration_h, 0),
        "grid_trace": _downsample_series(
            samples, scenario.duration_s, lambda s: s.grid
        ),
        # Net house consumption at the meter coupling = grid + Σ(battery AC
        # output) by energy balance.  It's the same scripted load in base and
        # head, so one trace is enough; the HTML grid chart overlays it as
        # context (grid = consumption minus battery output).
        "consumption_trace": _downsample_series(
            samples, scenario.duration_s, lambda s: s.grid + sum(s.powers)
        ),
        # Per-battery output traces (one downsampled series each) and labels,
        # for the per-scenario battery-output chart in the HTML report.
        "battery_labels": [
            f"B{i + 1} {specs[i].device_type}" for i in range(len(specs))
        ],
        "battery_traces": [
            _downsample_series(samples, scenario.duration_s, _battery_pick(i))
            for i in range(len(specs))
        ],
    }


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

_VENUS = BatterySpec()  # HMG-50 (V2-class), 1 s poll
_VENUS_V3 = BatterySpec(device_type="VNSE3-0", poll_interval=0.45)
_VENUS_V2_SLOW = BatterySpec(poll_interval=3.1)
# Venus D (VNSD-0): AC-coupled like the rest, but runs the integer
# proportional-integrator self-consumption loop instead of the float ramp — no
# spike filter, a tighter ±11 W deadband, and a unity-by-default ctrl_ratio gain.
_VENUS_D = BatterySpec(device_type="VNSD-0")
_B2500 = BatterySpec(
    device_type="HMA-1",
    max_charge_power=0,
    max_discharge_power=800,
    capacity_wh=2240.0,
    max_dc_input=1000,
    initial_soc=0.5,
)

# Efficiency-optimization mode knobs (mirrors a typical multi-battery setup).
_EFF_MODE: dict[str, float] = {
    "min_efficient_power": 150.0,
    "efficiency_rotation_interval": 900.0,
}


# Typed closure factories for event actions (a plain lambda with extra
# defaulted parameters doesn't type-check against ``Callable[[EvalWorld], None]``).


def _set_load(name: str, active: bool) -> Callable[[EvalWorld], None]:
    def apply(w: EvalWorld) -> None:
        w.set_load(name, active)

    return apply


def _set_solar_curve(watts: float) -> Callable[[EvalWorld], None]:
    def apply(w: EvalWorld) -> None:
        w.set_solar_curve(watts)

    return apply


def _set_solar_factor(factor: float) -> Callable[[EvalWorld], None]:
    def apply(w: EvalWorld) -> None:
        w.set_solar_factor(factor)

    return apply


def _set_dc_input(battery_index: int, watts: float) -> Callable[[EvalWorld], None]:
    def apply(w: EvalWorld) -> None:
        w.set_dc_input(battery_index, watts)

    return apply


def _household_steps(rng: random.Random, duration: float) -> list[Event]:
    """Scripted appliance schedule: kettle spikes, oven cycling, dishwasher.

    Times get a deterministic per-seed jitter so different seeds exercise
    different alignments against the poll cadence.
    """

    def jitter(t: float, spread: float = 20.0) -> float:
        return max(1.0, t + rng.uniform(-spread, spread))

    events: list[Event] = []

    def load_event(t: float, name: str, active: bool) -> None:
        state = "on" if active else "off"
        events.append(
            Event(at=jitter(t), label=f"{name}_{state}", apply=_set_load(name, active))
        )

    # Kettle: two 2 kW bursts of ~3 minutes.
    for burst, t0 in enumerate((600.0, duration * 0.7)):
        load_event(t0, "kettle", True)
        load_event(t0 + 180.0, "kettle", False)
        del burst
    # Oven: thermostat cycling between 30% and 60% of the run.  Cycles are
    # emitted as on/off pairs so the oven never stays on past its block
    # (an unpaired trailing "on" would stack loads beyond the battery's
    # ceiling for the rest of the run).
    t = duration * 0.3
    while t + 240.0 < duration * 0.6:
        load_event(t, "oven", True)
        load_event(t + 240.0, "oven", False)
        t += 240.0 + 180.0
    # Dishwasher: one long block in the second half.
    load_event(duration * 0.8, "dishwasher", True)
    load_event(duration * 0.8 + 600.0, "dishwasher", False)
    return events


_HOUSEHOLD_LOADS = [
    Load("kettle", 2000.0, "A"),
    Load("oven", 1500.0, "A"),
    Load("dishwasher", 1100.0, "A"),
]

# Washing-machine drum motor: a single ~120 W load the main-wash tumble runs,
# briefly pauses, and restarts.  Sized (with the scenario's ~1 s meter latency)
# to reproduce the field report in issue #473 — a steady ~500 W house whose
# grid never holds zero, hunting on the order of the log's ±100-180 W swings.
# Fidelity notes: the simulated battery plant limit-cycles somewhat harder and
# faster than the real (better-damped) firmware, so the sustained swing here is
# larger/quicker than that one log; and the modelled pause drops the full
# running load, so the export dip is about as deep as the import spike, whereas
# the field trace was asymmetric (shallower dip, larger spike) due to motor
# restart inrush this single on/off load does not model.
_WASHER_LOADS = [Load("washer_motor", 120.0, "A")]


def _washer_cycle(rng: random.Random, duration: float) -> list[Event]:
    """Main-wash drum tumble: the motor runs, briefly pauses, and restarts.

    This reproduces the field-reported washing-machine signature (issue #473).
    A real drum tumbles in one direction, briefly pauses, then reverses, so a
    short pause drops the load (a brief **export dip** as the battery is still
    discharging) and the restart re-applies it (an **import spike**), repeating
    every ~16 s. Paired with the scenario's ~1 s meter latency, the loop acts on
    stale readings and never fully settles between pauses, so the grid hunts
    continuously rather than holding zero — matching the log, which never showed
    a steady-at-zero phase.

    The events are unlabelled on purpose: a continuously hunting loop never
    holds the ±25 W band for the settle hold time, so this scenario is scored on
    the sustained-oscillation aggregates (``grid_p2p_w``, ``band_crossings_per_h``,
    ``steady_rms_w``, ``mean_abs_grid_w``, ``battery_travel_w_per_h``) rather than
    per-step settling — the step-response metrics read 0 for this failure mode.
    A balancer that damps the hunt drives those down.
    """
    events: list[Event] = []
    period = 16.0
    pause = 3.0
    start = duration * 0.15
    end = duration * 0.85
    # Motor on for the whole wash block; the rhythm is the brief pauses.
    events.append(Event(at=start, apply=_set_load("washer_motor", True)))
    t = start + (period - pause)
    while t + pause < end:
        # Small deterministic per-pause jitter so the rhythm doesn't phase-lock
        # to the 1 s meter cadence (and different seeds probe different
        # alignments), without ever reordering the pause/restart pair.
        j = rng.uniform(-0.5, 0.5)
        events.append(Event(at=max(1.0, t + j), apply=_set_load("washer_motor", False)))
        events.append(
            Event(at=max(1.0, t + pause + j), apply=_set_load("washer_motor", True))
        )
        t += period
    # Always leave the program with the motor off.
    events.append(Event(at=end, apply=_set_load("washer_motor", False)))
    return events


def _solar_curve(duration: float, peak: float) -> list[Event]:
    """Unlabeled half-sine solar day curve.

    Updated every ``SOLAR_CURVE_STEP_S`` so the curve is smooth: a coarse
    per-minute step jumps ~50 W per tick where the sine is steepest (sunrise /
    sunset), which the controller would chase as a series of small load steps.
    """
    events: list[Event] = []
    for t in range(0, int(duration), SOLAR_CURVE_STEP_S):
        watts = peak * math.sin(math.pi * t / duration)
        events.append(Event(at=float(t), apply=_set_solar_curve(watts)))
    return events


def _cloud_dips(rng: random.Random, duration: float) -> list[Event]:
    """Labeled cloud transients: solar collapses to 20% for ~2 minutes.

    Implemented as a multiplicative factor so the day curve keeps ticking
    underneath without cancelling the dip.
    """
    events: list[Event] = []
    for frac in (0.4, 0.55):
        t0 = duration * frac + rng.uniform(-60.0, 60.0)
        events.append(Event(at=t0, label="cloud_on", apply=_set_solar_factor(0.2)))
        events.append(
            Event(at=t0 + 120.0, label="cloud_off", apply=_set_solar_factor(1.0))
        )
    return events


def _dc_solar_curve(duration: float, peak: float, battery_index: int) -> list[Event]:
    """Unlabeled DC-input solar curve for a B2500-style battery (smooth — see
    :func:`_solar_curve`)."""
    events: list[Event] = []
    for t in range(0, int(duration), SOLAR_CURVE_STEP_S):
        watts = peak * math.sin(math.pi * t / duration)
        events.append(Event(at=float(t), apply=_set_dc_input(battery_index, watts)))
    return events


def _household_and_solar(
    rng: random.Random, duration: float, solar_peak: float
) -> list[Event]:
    """Household appliance steps over an AC solar day curve with cloud dips.

    Combines the discharge-side step schedule with a half-sine PV curve big
    enough to push the pool into export/charge territory around midday, so the
    scenario exercises the full bidirectional loop (charge distribution, the
    AC-charge clamp, zero-crossings) on top of the step responses — not just
    discharge.
    """
    return (
        _household_steps(rng, duration)
        + _solar_curve(duration, solar_peak)
        + _cloud_dips(rng, duration)
    )


def build_scenarios() -> dict[str, Scenario]:
    """All evaluation scenarios, keyed by name.

    Multi-battery scenarios come in two balancer modes: plain fair-share
    (``…/fair``) and efficiency optimization (``…/eff``, exercising
    deprioritization, rotation, saturation swaps and probe handoffs).
    """
    scenarios: dict[str, Scenario] = {}

    def add(s: Scenario) -> None:
        scenarios[s.name] = s

    dur_steps = 3600.0
    add(
        Scenario(
            name="single_venus_steps",
            description="One Venus, stepped house load (kettle/oven/dishwasher)",
            batteries=[_VENUS],
            duration_s=dur_steps,
            loads=list(_HOUSEHOLD_LOADS),
            build_events=lambda rng: _household_steps(rng, dur_steps),
        )
    )

    dur_washer = 1800.0
    add(
        Scenario(
            name="single_venus_washer",
            description=(
                "One Venus, washing-machine drum tumble (~120 W motor "
                "pausing/restarting every ~16 s) over a meter with ~1 s "
                "latency — sustained-oscillation stress (issue #473)"
            ),
            batteries=[_VENUS],
            duration_s=dur_washer,
            base_load=[450.0, 0.0, 0.0],
            loads=list(_WASHER_LOADS),
            build_events=lambda rng: _washer_cycle(rng, dur_washer),
            # The field setup read an HA push sensor with measurement+transport
            # delay; that latency is what turns each drum disturbance into a
            # loop that hunts continuously instead of settling between pauses
            # (issue #473). Without it the loop settles into ~10 s calm windows
            # the real trace never showed.
            meter_latency_s=1.0,
        )
    )

    dur_solar = 5400.0
    solar_peak = 1800.0
    add(
        Scenario(
            name="single_venus_solar",
            description="One Venus, solar day curve crossing into export + clouds",
            batteries=[BatterySpec(initial_soc=0.4)],
            duration_s=dur_solar,
            base_load=[400.0, 0.0, 0.0],
            build_events=lambda rng: (
                _solar_curve(dur_solar, solar_peak) + _cloud_dips(rng, dur_solar)
            ),
        )
    )

    add(
        Scenario(
            name="single_venus_d_steps",
            description="One Venus D (VNSD-0 integer loop), stepped house load",
            batteries=[_VENUS_D],
            duration_s=dur_steps,
            loads=list(_HOUSEHOLD_LOADS),
            build_events=lambda rng: _household_steps(rng, dur_steps),
        )
    )

    add(
        Scenario(
            name="single_venus_d_washer",
            description=(
                "One Venus D, washing-machine drum tumble over a ~1 s-latency "
                "meter — sustained-oscillation stress for the integer loop "
                "(±11 W deadband, no spike filter), cf. single_venus_washer"
            ),
            batteries=[_VENUS_D],
            duration_s=dur_washer,
            base_load=[450.0, 0.0, 0.0],
            loads=list(_WASHER_LOADS),
            build_events=lambda rng: _washer_cycle(rng, dur_washer),
            meter_latency_s=1.0,
        )
    )

    add(
        Scenario(
            name="single_venus_d_solar",
            description="One Venus D, solar day curve crossing into export + clouds",
            batteries=[BatterySpec(device_type="VNSD-0", initial_soc=0.4)],
            duration_s=dur_solar,
            base_load=[400.0, 0.0, 0.0],
            build_events=lambda rng: (
                _solar_curve(dur_solar, solar_peak) + _cloud_dips(rng, dur_solar)
            ),
        )
    )

    # Heterogeneous phase: a Venus D (integer integrator) sharing one phase with
    # a Venus C (HMG float ramp), so the two different control laws coexist
    # under the same balancer.
    for mode, kwargs in (("fair", {}), ("eff", _EFF_MODE)):
        add(
            Scenario(
                name=f"venus_d_plus_c/{mode}",
                description="One Venus D + one Venus C sharing one phase",
                batteries=[_VENUS_D, _VENUS],
                duration_s=dur_steps,
                loads=list(_HOUSEHOLD_LOADS),
                build_events=lambda rng: _household_steps(rng, dur_steps),
                ct_kwargs=dict(kwargs),
            )
        )

    for mode, kwargs in (("fair", {}), ("eff", _EFF_MODE)):
        add(
            Scenario(
                name=f"two_venus/{mode}",
                description="Two identical Venus sharing one phase",
                batteries=[_VENUS, _VENUS],
                duration_s=dur_steps,
                loads=list(_HOUSEHOLD_LOADS),
                build_events=lambda rng: _household_steps(rng, dur_steps),
                ct_kwargs=dict(kwargs),
            )
        )

    # Solar peak (W) for the multi-Venus solar scenarios: above the base load
    # plus typical appliance draw, so midday PV pushes the pool into charging /
    # export for stretches.
    solar_peak_house = 3000.0
    for mode, kwargs in (("fair", {}), ("eff", _EFF_MODE)):
        add(
            Scenario(
                name=f"two_venus_solar/{mode}",
                description="Two Venus, household load + solar day curve + clouds",
                batteries=[_VENUS, _VENUS],
                duration_s=dur_solar,
                base_load=[400.0, 0.0, 0.0],
                loads=list(_HOUSEHOLD_LOADS),
                build_events=lambda rng: _household_and_solar(
                    rng, dur_solar, solar_peak_house
                ),
                ct_kwargs=dict(kwargs),
            )
        )

    dur_mixed = 5400.0
    for mode, kwargs in (("fair", {}), ("eff", _EFF_MODE)):
        add(
            Scenario(
                name=f"mixed_venus_b2500/{mode}",
                description="Two Venus + one DC-only B2500 with PV input",
                batteries=[_VENUS, _VENUS, _B2500],
                duration_s=dur_mixed,
                loads=list(_HOUSEHOLD_LOADS),
                build_events=lambda rng: (
                    _household_steps(rng, dur_mixed)
                    + _dc_solar_curve(dur_mixed, 700.0, battery_index=2)
                ),
                ct_kwargs=dict(kwargs),
            )
        )

    for mode, kwargs in (("fair", {}), ("eff", _EFF_MODE)):
        add(
            Scenario(
                name=f"mixed_cadence/{mode}",
                description="Slow-polling V2 (3.1 s) + fast V3 (0.45 s)",
                batteries=[_VENUS_V2_SLOW, _VENUS_V3],
                duration_s=dur_steps,
                loads=list(_HOUSEHOLD_LOADS),
                build_events=lambda rng: _household_steps(rng, dur_steps),
                ct_kwargs=dict(kwargs),
            )
        )

    for mode, kwargs in (("fair", {}), ("eff", _EFF_MODE)):
        add(
            Scenario(
                name=f"mixed_cadence_solar/{mode}",
                description="Slow V2 + fast V3, household load + solar + clouds",
                batteries=[_VENUS_V2_SLOW, _VENUS_V3],
                duration_s=dur_solar,
                base_load=[400.0, 0.0, 0.0],
                loads=list(_HOUSEHOLD_LOADS),
                build_events=lambda rng: _household_and_solar(
                    rng, dur_solar, solar_peak_house
                ),
                ct_kwargs=dict(kwargs),
            )
        )

    return scenarios


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

# Metrics shown in tables, in order, with "lower is better" direction (all
# current metrics improve downward).
_REPORT_METRICS = [
    "settle_mean_s",
    "settle_p95_s",
    "unsettled_events",
    "overshoot_mean_w",
    "overshoot_max_w",
    "band_crossings_per_h",
    "grid_p2p_w",
    "steady_rms_w",
    "mean_abs_grid_w",
    "avoidable_import_wh",
    "avoidable_export_wh",
    "battery_travel_w_per_h",
]

# Short, human-readable description for each metric in `_REPORT_METRICS`,
# rendered as a collapsible glossary in the CI PR comment. Keep in sync with
# `_REPORT_METRICS` and the metric computation in `_score()`.
_METRIC_GLOSSARY = [
    (
        "settle_mean_s",
        f"Mean seconds after a load/PV step for grid power to return inside the "
        f"±{SETTLE_BAND_W:g} W settle band and hold for {SETTLE_HOLD_S:g} s "
        f"(reaction speed).",
    ),
    (
        "settle_p95_s",
        "95th-percentile settle time — the slow tail of reactions.",
    ),
    (
        "unsettled_events",
        f"Number of disturbance events that never settled within the "
        f"{EVENT_WINDOW_S / 60:g}-minute measurement window.",
    ),
    (
        "overshoot_mean_w",
        "Mean overshoot (W): how far grid power swings past zero to the "
        "opposite sign after an event.",
    ),
    (
        "overshoot_max_w",
        "Worst-case overshoot (W) across all events.",
    ),
    (
        "band_crossings_per_h",
        f"Sign flips per hour across the ±{OSC_BAND_W:g} W hysteresis band — "
        f"oscillation / hunting frequency.",
    ),
    (
        "grid_p2p_w",
        "Sustained peak-to-peak grid swing (95th - 5th percentile) over the "
        "whole run — oscillation amplitude. Non-zero whenever the loop keeps "
        "hunting, including continuous oscillation the step-response metrics "
        "(settle/overshoot) miss.",
    ),
    (
        "steady_rms_w",
        f"RMS grid power (W) during steady state (excluding the "
        f"{STEADY_EXCLUDE_S:g} s after each event) — residual jitter when "
        f"nothing is changing.",
    ),
    (
        "mean_abs_grid_w",
        "Mean absolute grid power (W) over the whole run — overall tracking accuracy.",
    ),
    (
        "avoidable_import_wh",
        "Energy imported from the grid (Wh) the battery could have supplied "
        "(it had charge and discharge headroom) — missed self-consumption.",
    ),
    (
        "avoidable_export_wh",
        "Energy exported to the grid (Wh) an AC-chargeable battery could have "
        "absorbed (it had room and charge headroom) — missed charging.",
    ),
    (
        "battery_travel_w_per_h",
        "Total absolute change in battery setpoints per hour (W/h) — control "
        "effort / actuator wear; lower is smoother.",
    ),
]


def _mean_value(values: list):
    """Average a homogeneous list of result values across seeds.

    Recurses into nested lists (a battery's trace, the list of per-battery
    traces) so traces average element-by-element; string lists (battery
    labels, identical across seeds) pass through as the first value."""
    v0 = values[0]
    if isinstance(v0, bool):
        return v0
    if isinstance(v0, (int, float)):
        return round(sum(float(v) for v in values) / len(values), 1)
    if isinstance(v0, list):
        if v0 and isinstance(v0[0], str):
            return v0  # labels are identical across seeds
        return [_mean_value([v[i] for v in values]) for i in range(len(v0))]
    return v0  # strings / anything else: identical across seeds


def _merge_seeds(per_seed: list[dict]) -> dict:
    """Collapse one scenario's per-seed results into a single averaged row.

    Every numeric metric (and every trace, element-wise) becomes the mean over
    the seeds, so the reported figure is the seed-averaged signal rather than
    one noisy draw. A lone seed is returned unchanged (no averaging, keeps the
    original ``seed`` field). The merged row carries ``seeds`` / ``n_seeds``
    instead of a single ``seed``."""
    if len(per_seed) == 1:
        return per_seed[0]
    merged: dict = {
        "scenario": per_seed[0]["scenario"],
        "seeds": [r["seed"] for r in per_seed],
        "n_seeds": len(per_seed),
    }
    for key in per_seed[0]:
        if key in ("scenario", "seed"):
            continue
        merged[key] = _mean_value([r[key] for r in per_seed])
    return merged


def _aggregate(results: list[dict]) -> dict:
    """Collapse a result list into one synthetic "AGGREGATE" row.

    Each reported metric becomes its **unweighted mean across the scenarios**
    that carry it (a base produced before a metric existed simply doesn't
    contribute that key — it's omitted from the aggregate, mirroring the
    per-scenario renderers' ``—`` handling). One row that answers "did this
    change help *overall*?" at a glance, on top of the per-scenario tables.

    Means are scale-mixing on purpose: they're a rough headline, while the
    per-metric relative deltas (and :func:`_overall_summary`) are the
    unit-independent read on direction.
    """
    agg: dict = {"scenario": "AGGREGATE", "n_scenarios": len(results)}
    for key in _REPORT_METRICS:
        vals = [float(r[key]) for r in results if key in r]
        if vals:
            agg[key] = round(sum(vals) / len(vals), 1)
    return agg


def _compare_aggregates(
    base: list[dict] | None, head: list[dict]
) -> tuple[dict | None, dict]:
    """Aggregate base and head for a base-vs-head comparison.

    With a baseline, both sides are aggregated over the **scenarios they share**
    so the Overall verdict and aggregate table compare like for like even when
    the two runs cover different scenario sets (e.g. a PR adds or drops a
    scenario). Without a baseline, head is aggregated over all its scenarios."""
    if not base:
        return None, _aggregate(head)
    shared = {r["scenario"] for r in base} & {r["scenario"] for r in head}
    base_agg = _aggregate([r for r in base if r["scenario"] in shared])
    head_agg = _aggregate([r for r in head if r["scenario"] in shared])
    return base_agg, head_agg


def _overall_change(
    base_agg: dict, head_agg: dict
) -> tuple[int, int, int, float | None]:
    """Count improved / regressed / unchanged aggregate metrics and the mean
    relative change across them (``None`` when nothing is comparable).

    Every reported metric is lower-is-better, so a negative mean is an overall
    improvement. The mean is over *relative* deltas (head vs base, per metric),
    making it unit-independent; metrics whose base aggregate is 0 contribute to
    the up/down counts but not the percentage (no defined relative change)."""
    improved = regressed = unchanged = 0
    deltas: list[float] = []
    for key in _REPORT_METRICS:
        if key not in base_agg or key not in head_agg:
            continue
        bv, hv = float(base_agg[key]), float(head_agg[key])
        if hv < bv:
            improved += 1
        elif hv > bv:
            regressed += 1
        else:
            unchanged += 1
        if bv != 0:
            deltas.append((hv - bv) / abs(bv))
    mean_pct = sum(deltas) / len(deltas) * 100.0 if deltas else None
    return improved, regressed, unchanged, mean_pct


def _overall_summary(base_agg: dict, head_agg: dict) -> str:
    """One-line verdict for the aggregate: how many metrics moved each way and
    the mean relative change (lower is better)."""
    improved, regressed, unchanged, mean_pct = _overall_change(base_agg, head_agg)
    if mean_pct is None:
        trend = "no comparable metrics"
    elif mean_pct < 0:
        trend = f"mean {mean_pct:.1f}% (better)"
    elif mean_pct > 0:
        trend = f"mean +{mean_pct:.1f}% (worse)"
    else:
        trend = "mean 0% (unchanged)"
    return (
        f"{improved} improved, {regressed} regressed, {unchanged} unchanged "
        f"across {improved + regressed + unchanged} metrics — {trend}"
    )


def render_text(results: list[dict]) -> str:
    lines = []
    # A single aggregate row up top makes an overall read possible without
    # eyeballing every scenario (skipped for a lone scenario — it'd just echo
    # that scenario's own numbers).
    if len(results) > 1:
        agg = _aggregate(results)
        lines.append(f"== AGGREGATE (mean across {agg['n_scenarios']} scenarios)")
        for key in _REPORT_METRICS:
            if key in agg:
                lines.append(f"  {key:<24} {agg[key]}")
        lines.append("")
    for res in results:
        lines.append(
            f"== {res['scenario']} ({_seed_label(res)}, "
            f"{res['duration_h']}h, {res['events_measured']} events)"
        )
        for key in _REPORT_METRICS:
            lines.append(f"  {key:<24} {res[key]}")
    return "\n".join(lines)


def _seed_label(res: dict) -> str:
    """How a result's seed provenance reads in a header: a single ``seed N`` or
    ``mean of N seeds`` once per-seed results have been merged."""
    if res.get("n_seeds"):
        return f"mean of {res['n_seeds']} seeds"
    return f"seed {res.get('seed', '?')}"


def _fmt_delta(base: float, head: float) -> str:
    if base == head:
        return "="
    if base == 0:
        return f"{head - base:+g}"
    return f"{(head - base) / abs(base) * 100.0:+.0f}%"


def _md_metric_rows(base: dict | None, head: dict) -> list[str]:
    """A `| Metric | Base | Head | Δ |` table body for one result pair.

    Shared by the aggregate roll-up and the per-scenario tables. A base
    produced before a metric existed (newly added metric on the PR head) has
    no value to compare against, so both Base and Δ degrade to ``—``."""
    rows = ["| Metric | Base | Head | Δ |", "|---|---:|---:|---:|"]
    for key in _REPORT_METRICS:
        hv: object = head.get(key, "—")
        if base is not None and key in base and key in head:
            bv: object = base[key]
            delta = _fmt_delta(float(base[key]), float(head[key]))
        else:
            bv = "—"
            delta = "—"
        rows.append(f"| {key} | {bv} | {hv} | {delta} |")
    return rows


def _seeds_phrase(results: list[dict]) -> str:
    """`mean of N seeds` / `single seed` describing how a result set was run."""
    n = max((int(r.get("n_seeds", 1)) for r in results), default=1)
    return f"mean of {n} seeds" if n > 1 else "single seed"


def _seeds_caption(base: list[dict] | None, head: list[dict]) -> str:
    """One sentence on the seed count behind each side, or ``""`` when both ran a
    single seed (so the note only appears once figures are seed-averaged)."""
    hp = _seeds_phrase(head)
    bp = _seeds_phrase(base) if base else None
    if hp == "single seed" and (bp is None or bp == "single seed"):
        return ""
    if bp is None or bp == hp:
        return f"Metrics are the per-scenario {hp}."
    return f"Metrics are the per-scenario mean over seeds (base: {bp}, head: {hp})."


def _seeds_note(base: list[dict], head: list[dict]) -> list[str]:
    """The :func:`_seeds_caption` sentence as italic Markdown lines (empty when
    there's nothing to note)."""
    caption = _seeds_caption(base, head)
    return [f"_{caption}_", ""] if caption else []


def render_markdown_compare(
    base: list[dict], head: list[dict], *, report_available: bool = False
) -> str:
    """Markdown before/after tables for the CI PR comment.

    The comment carries the metrics tables for an at-a-glance read; the
    interactive grid-power charts live in the self-contained HTML report
    (:func:`astrameter.simulator.eval_report.render_html_report`) that CI
    uploads as the ``steering-eval`` artifact, since GitHub can't render an
    interactive chart inline in a comment.

    Set *report_available* when an HTML report is being produced (and CI will
    append a link to it); only then is the "see the link below" pointer
    included, so a plain ``--compare`` run doesn't promise a report that
    doesn't exist.
    """
    base_by = {r["scenario"]: r for r in base}
    base_agg, head_agg = _compare_aggregates(base, head)
    out = [
        "### Steering evaluation (base vs head)",
        "",
    ]
    # Lead with the aggregate so a reviewer sees the overall direction before
    # any per-scenario table — the whole point of the roll-up.
    if base_agg is not None:
        out.append(f"**Overall: {_overall_summary(base_agg, head_agg)}.**")
        out.append("")
    out += [
        "Lower is better for every metric. See "
        "`src/astrameter/simulator/evaluation.py` for definitions.",
        "",
    ]
    out += _seeds_note(base, head)
    out += [
        f"#### Aggregate — mean across {head_agg['n_scenarios']} scenarios",
        "",
    ]
    out += _md_metric_rows(base_agg, head_agg)
    out.append("")
    if report_available:
        out += [
            "📊 **Interactive grid-power charts** (zoom / hover / toggle series) "
            "are in the self-contained `steering-eval-report.html` report — see "
            "the link below (it opens directly in the browser).",
            "",
        ]
    out += [
        "<details><summary><b>What do these metrics mean?</b></summary>",
        "",
        "| Metric | Meaning |",
        "|---|---|",
    ]
    out.extend(f"| `{key}` | {desc} |" for key, desc in _METRIC_GLOSSARY)
    out.append("")
    out.append("</details>")
    out.append("")
    for res in head:
        b = base_by.get(res["scenario"])
        out.append(
            f"<details><summary><b>{res['scenario']}</b> — "
            f"{_summary_line(b, res)}</summary>"
        )
        out.append("")
        out.extend(_md_metric_rows(b, res))
        out.append("")
        out.append("</details>")
    missing = [
        r["scenario"]
        for r in base
        if r["scenario"] not in {h["scenario"] for h in head}
    ]
    if missing:
        out.append("")
        out.append(f"_Scenarios only in base: {', '.join(missing)}_")
    return "\n".join(out)


def _summary_line(base: dict | None, head: dict) -> str:
    parts = [
        f"settle {head['settle_mean_s']}s",
        f"overshoot {head['overshoot_max_w']}W",
        f"RMS {head['steady_rms_w']}W",
    ]
    if base:
        parts = [
            f"settle {base['settle_mean_s']}→{head['settle_mean_s']}s",
            f"overshoot {base['overshoot_max_w']}→{head['overshoot_max_w']}W",
            f"RMS {base['steady_rms_w']}→{head['steady_rms_w']}W",
        ]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_overrides(pairs: list[str]) -> dict[str, float]:
    overrides: dict[str, float] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise SystemExit(f"--set expects KEY=VALUE, got {pair!r}")
        overrides[key.strip()] = float(value)
    return overrides


def _run_one(name: str, seed: int, overrides: dict[str, float]) -> dict:
    """Run one (scenario, seed) to completion in its own process.

    A process-pool worker: it rebuilds the scenario registry from *name*
    (closures in ``Scenario.build_events`` aren't picklable, so we ship the
    name, not the object) and drives the async scenario under a fresh event
    loop. Process isolation also gives each seed its own global ``random``
    state, so concurrent runs stay bit-for-bit deterministic — which a single
    interpreter (asyncio interleaving the shared global RNG) could not."""
    scenarios = build_scenarios()
    return asyncio.run(run_scenario(scenarios[name], seed=seed, overrides=overrides))


def _run_tasks(
    tasks: list[tuple[str, int]], overrides: dict[str, float]
) -> dict[tuple[str, int], dict]:
    """Run every (scenario, seed) task, in parallel across CPU cores.

    The simulation is CPU-bound, so processes (not asyncio) are what actually
    parallelize it; one task runs inline to avoid pool overhead."""
    if len(tasks) <= 1:
        return {t: _run_one(t[0], t[1], overrides) for t in tasks}
    workers = min(len(tasks), os.cpu_count() or 1)
    out: dict[tuple[str, int], dict] = {}
    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_run_one, name, seed, overrides): (name, seed)
            for name, seed in tasks
        }
        for fut in as_completed(futures):
            out[futures[fut]] = fut.result()
    return out


def _run_all(
    names: list[str], seeds: list[int], overrides: dict[str, float]
) -> list[dict]:
    """Run the selected scenarios across all *seeds* concurrently, then collapse
    each scenario's per-seed results into one seed-averaged row."""
    scenarios = build_scenarios()
    unknown = [n for n in names if n not in scenarios]
    if unknown:
        raise SystemExit(
            f"unknown scenario(s): {', '.join(unknown)}; "
            f"available: {', '.join(sorted(scenarios))}"
        )
    selected = names or sorted(scenarios)
    raw = _run_tasks([(name, seed) for name in selected for seed in seeds], overrides)
    results = []
    for name in selected:
        merged = _merge_seeds([raw[(name, seed)] for seed in seeds])
        print(render_text([merged]), file=sys.stderr, flush=True)
        results.append(merged)
    return results


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m astrameter.simulator.evaluation",
        description="Steering-quality evaluation for the active-control loop.",
    )
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        help="run only this scenario (repeatable; default: all)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=1,
        help="first seed (default: 1); with --seeds N, runs seed..seed+N-1",
    )
    parser.add_argument(
        "--seeds",
        type=int,
        default=5,
        metavar="N",
        help="number of seeds to run per scenario and average over, in "
        "parallel across CPU cores (default: 5; 1 disables seed averaging)",
    )
    parser.add_argument(
        "--set",
        dest="overrides",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a CT002/balancer config knob, e.g. balance_deadband=25",
    )
    parser.add_argument("--json", metavar="PATH", help="write results JSON to PATH")
    parser.add_argument(
        "--input",
        metavar="PATH",
        help="load results from PATH instead of running scenarios",
    )
    parser.add_argument(
        "--compare",
        metavar="BASELINE_JSON",
        help="compare results against a baseline JSON",
    )
    parser.add_argument(
        "--html",
        metavar="PATH",
        help="write the self-contained interactive HTML report to PATH "
        "(uses --compare's baseline when given, else head-only)",
    )
    parser.add_argument("--list", action="store_true", help="list scenarios and exit")
    args = parser.parse_args(argv)

    if args.list:
        for name, sc in sorted(build_scenarios().items()):
            print(f"{name:<28} {sc.description}")
        return

    if args.input:
        with open(args.input) as fh:
            results = json.load(fh)
    else:
        if args.seeds < 1:
            raise SystemExit("--seeds must be >= 1")
        overrides = _parse_overrides(args.overrides)
        seeds = list(range(args.seed, args.seed + args.seeds))
        results = _run_all(args.scenario, seeds, overrides)

    if args.json:
        with open(args.json, "w") as fh:
            json.dump(results, fh, indent=2)

    base = None
    if args.compare:
        with open(args.compare) as fh:
            base = json.load(fh)
        print(render_markdown_compare(base, results, report_available=bool(args.html)))
    elif not args.input:
        print(render_text(results))

    if args.html:
        from .eval_report import render_html_report

        base_agg, head_agg = _compare_aggregates(base, results)
        report = render_html_report(
            base,
            results,
            report_metrics=_REPORT_METRICS,
            metric_glossary=_METRIC_GLOSSARY,
            fmt_delta=_fmt_delta,
            aggregate=(base_agg, head_agg),
            aggregate_summary=(
                _overall_summary(base_agg, head_agg) if base_agg is not None else ""
            ),
            note=_seeds_caption(base, results),
        )
        with open(args.html, "w", encoding="utf-8") as fh:
            fh.write(report)


if __name__ == "__main__":
    main()
