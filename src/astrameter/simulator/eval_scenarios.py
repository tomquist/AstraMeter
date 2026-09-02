"""The scenario catalogue: battery presets, scripted household activity and
recorded traces, assembled by :func:`build_scenarios`."""

from __future__ import annotations

import functools
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Concatenate, ParamSpec, TypeVar

from .eval_spec import BatterySpec, EvalWorld, Event, Scenario
from .load_model import Load, load_net_trace, load_power_trace

# Resolution of the half-sine solar day curve (s): fine enough that each step
# is a few watts, so the controller sees a ramp rather than a staircase of
# ~50 W load steps at the steepest part of the sine.
SOLAR_CURVE_STEP_S = 2
# Where in the run the two clouds pass, as fractions of its length: both close
# to the solar peak, where a dip costs the most.
_CLOUD_DIP_FRACTIONS = (0.4, 0.55)

_VENUS = BatterySpec()  # HMG-50 (V2-class), 1 s poll
# Venus E (VNSE3-0), the fast-polling V3-class unit: integer integrator behind
# its input gate (see venus_integer_steering), so it slews far harder than an
# HMG-50 on a big step.
_VENUS_V3 = BatterySpec(device_type="VNSE3-0", poll_interval=0.45)
_VENUS_V2_SLOW = BatterySpec(poll_interval=3.1)
# Venus D (VNSD-0): a ±10 W input deadband with a one-shot spike filter, a
# ±11 W setpoint park, and a unity-by-default ctrl_ratio gain.
_VENUS_D = BatterySpec(device_type="VNSD-0")
_B2500 = BatterySpec(
    device_type="HMA-1",
    max_charge_power=0,
    max_discharge_power=800,
    capacity_wh=2240.0,
    max_dc_input=1000,
    initial_soc=0.5,
)
# A B2500 in a same-phase pair, sized to serve a household on its own charge for
# the run: no DC input, so only the balancer's command decides its output
# (the pair in issue #600: two HMJ-2 on phase A).
_B2500_PAIR = BatterySpec(
    device_type="HMJ-2",
    phase="A",
    max_charge_power=0,
    max_discharge_power=800,
    capacity_wh=5120.0,
    initial_soc=0.8,
)

# Efficiency-optimization mode knobs (mirrors a typical multi-battery setup).
_EFF_MODE: dict[str, float] = {
    "min_efficient_power": 150.0,
    "efficiency_rotation_interval": 900.0,
}
# The concentration cut-in sits near total load = 2 x min_efficient_power, so
# the real-trace pair needs a higher floor than 150 W for it to land inside the
# recorded load's range: at 500 W the second Venus idles on the calm overnight
# base and cuts in on cooking peaks; at the default both stay active all run
# and /eff is an exact copy of /fair.
_TRACE_EFF_MODE: dict[str, float] = {**_EFF_MODE, "min_efficient_power": 500.0}

_HOUSEHOLD_LOADS = [
    Load("kettle", 2000.0, "A"),
    Load("oven", 1500.0, "A"),
    Load("dishwasher", 1100.0, "A"),
]
# The same appliances spread one-per-phase for the three-phase scenario; names
# match so the same scripted schedule drives them.
_PHASE_IMBALANCE_LOADS = [
    Load("kettle", 2000.0, "A"),
    Load("oven", 1500.0, "B"),
    Load("dishwasher", 1100.0, "C"),
]
# Washing-machine drum motor: a ~120 W load the main-wash tumble runs, briefly
# pauses and restarts. Sized (with ~1 s meter latency) to reproduce the field
# report in issue #473, a steady ~500 W house whose grid never held zero.
_WASHER_LOADS = [Load("washer_motor", 120.0, "A")]

# "Pretty noisy" house baseline: many small switching loads and no dominant
# appliance, so the ±_NOISY_BASE_NOISE W jitter on every meter read is the only
# disturbance and the scenario is scored on the sustained-oscillation
# aggregates. A balancer that ignores the noise holds grid swing and battery
# travel down instead of chasing every wiggle.
_NOISY_BASE_LOAD = [400.0, 0.0, 0.0]
_NOISY_BASE_NOISE = 150.0

# Recorded traces (CC BY, see traces/README.md): a real home carries correlated
# drift and persistent appliance switching that IID noise lacks, so it rewards
# a balancer that tracks genuine load changes over one tuned to reject white
# noise. Loaded on first use, so importing the module stays side-effect-free
# and a missing trace fails with a scenario-specific error.
_TRACE_DIR = Path(__file__).parent / "traces"
# A real CT adds a few watts of jitter on top of the recorded house power.
_TRACE_METER_NOISE = 10.0
# The Cyprus prosumer site scaled down to balcony size (keeps PV under the load
# model's 2 kW solar clamp while preserving the cloud-dip shape).
_PV_SCALE = 0.45

_P = ParamSpec("_P")
_Row = TypeVar("_Row", bound=tuple)


def _bind(
    method: Callable[Concatenate[EvalWorld, _P], None],
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> Callable[[EvalWorld], None]:
    """An event action: *method* of the world, called with these arguments."""

    def apply(world: EvalWorld) -> None:
        method(world, *args, **kwargs)

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
            Event(
                at=jitter(t),
                label=f"{name}_{state}",
                apply=_bind(EvalWorld.set_load, name, active),
            )
        )

    # Kettle: two 2 kW bursts of ~3 minutes.
    for t0 in (600.0, duration * 0.7):
        load_event(t0, "kettle", True)
        load_event(t0 + 180.0, "kettle", False)
    # Oven: thermostat cycling between 30% and 60% of the run, emitted as
    # on/off pairs so an unpaired trailing "on" can never stack loads beyond
    # the battery's ceiling for the rest of the run.
    t = duration * 0.3
    while t + 240.0 < duration * 0.6:
        load_event(t, "oven", True)
        load_event(t + 240.0, "oven", False)
        t += 240.0 + 180.0
    # Dishwasher: one long block in the second half.
    load_event(duration * 0.8, "dishwasher", True)
    load_event(duration * 0.8 + 600.0, "dishwasher", False)
    return events


def _washer_cycle(rng: random.Random, duration: float) -> list[Event]:
    """Main-wash drum tumble: the motor runs, pauses for ~3 s and restarts
    every ~16 s (issue #473). Each pause is a brief export dip, each restart an
    import spike; with the scenario's meter latency the loop never settles
    between them. The events are unlabelled on purpose: a continuously hunting
    loop never holds the settle band, so the scenario is scored on the
    sustained-oscillation aggregates, which a balancer that damps the hunt
    drives down."""
    events: list[Event] = []
    period = 16.0
    pause = 3.0
    start = duration * 0.15
    end = duration * 0.85
    # Motor on for the whole wash block; the rhythm is the brief pauses.
    events.append(
        Event(at=start, apply=_bind(EvalWorld.set_load, "washer_motor", True))
    )
    t = start + (period - pause)
    while t + pause < end:
        # Small per-pause jitter so the rhythm doesn't phase-lock to the 1 s
        # meter cadence, without ever reordering the pause/restart pair.
        j = rng.uniform(-0.5, 0.5)
        events.append(
            Event(
                at=max(1.0, t + j),
                apply=_bind(EvalWorld.set_load, "washer_motor", False),
            )
        )
        events.append(
            Event(
                at=max(1.0, t + pause + j),
                apply=_bind(EvalWorld.set_load, "washer_motor", True),
            )
        )
        t += period
    # Always leave the program with the motor off.
    events.append(Event(at=end, apply=_bind(EvalWorld.set_load, "washer_motor", False)))
    return events


def _unscripted(_rng: random.Random) -> list[Event]:
    """No scripted events: base load, its noise and any solar drive the loop."""
    return []


def _missing_trace(path: Path, purpose: str) -> RuntimeError:
    return RuntimeError(
        f"{purpose} needs the trace at {path}, which isn't available (dev/eval-"
        "only data, not packaged) — run from a source checkout. See "
        "traces/README.md."
    )


@functools.cache
def _household_trace() -> list[tuple[float, float]]:
    """Real whole-house power trace (RAE House 1, 1 Hz)."""
    path = _TRACE_DIR / "rae_household.csv"
    try:
        return load_power_trace(path)
    except OSError as exc:
        raise _missing_trace(path, "Real-trace scenario") from exc


@functools.cache
def _net_trace() -> list[tuple[float, float, float]]:
    """Real prosumer net-load trace (Cyprus, 30 s): PV and load from one site
    on a partly-cloudy day, so the cloud transients are genuine."""
    path = _TRACE_DIR / "cyprus_netload.csv"
    try:
        return load_net_trace(path)
    except OSError as exc:
        raise _missing_trace(path, "PV-net scenario") from exc


def _replay_trace(
    rng: random.Random,
    duration: float,
    trace: Sequence[_Row],
    action: Callable[[_Row], Callable[[EvalWorld], None]],
) -> list[Event]:
    """Replay a per-seed window of a recorded trace (rows ``(seconds, ...)``)
    as unlabelled events, *action* turning each row into the world mutation.

    Each seed starts at a different offset into the recording, so seeds see
    genuinely different regimes rather than re-rolled noise, and the run opens
    on the sample active at that offset instead of the scenario's placeholder
    base load. Unlabelled because real load changes aren't clean steps: these
    scenarios are scored on the sustained-oscillation and energy aggregates.
    """
    t0 = trace[0][0]
    span = trace[-1][0] - t0
    max_off = max(0.0, span - duration)
    start = rng.uniform(0.0, max_off)
    events: list[Event] = []
    for row in trace:
        rel = (row[0] - t0) - start
        if rel < 0.0:
            continue
        if rel > duration:
            break
        events.append(Event(at=rel, apply=action(row)))
    if not events or events[0].at > 0.0:
        first = trace[0]
        for row in trace:
            if (row[0] - t0) <= start:
                first = row
            else:
                break
        events.insert(0, Event(at=0.0, apply=action(first)))
    return events


def _household_trace_events(rng: random.Random, duration: float) -> list[Event]:
    return _replay_trace(
        rng,
        duration,
        _household_trace(),
        lambda row: _bind(EvalWorld.set_house_load, row[1]),
    )


def _pv_net_events(rng: random.Random, duration: float) -> list[Event]:
    return _replay_trace(
        rng,
        duration,
        _net_trace(),
        lambda row: _bind(
            EvalWorld.set_net_load, row[1] * _PV_SCALE, row[2] * _PV_SCALE
        ),
    )


def _solar_day(
    duration: float, peak: float, *, dc_battery: int | None = None
) -> list[Event]:
    """Unlabeled half-sine solar day curve, on the house's AC side or wired
    into battery *dc_battery*'s DC input, stepped every SOLAR_CURVE_STEP_S."""
    events: list[Event] = []
    for t in range(0, int(duration), SOLAR_CURVE_STEP_S):
        watts = peak * math.sin(math.pi * t / duration)
        apply = (
            _bind(EvalWorld.set_solar_curve, watts)
            if dc_battery is None
            else _bind(EvalWorld.set_dc_input, dc_battery, watts)
        )
        events.append(Event(at=float(t), apply=apply))
    return events


def _cloud_dips(rng: random.Random, duration: float) -> list[Event]:
    """Labeled cloud transients: solar collapses to 20% for ~2 minutes, as a
    multiplicative factor so the day curve keeps ticking underneath."""
    events: list[Event] = []
    for frac in _CLOUD_DIP_FRACTIONS:
        t0 = duration * frac + rng.uniform(-60.0, 60.0)
        events.append(
            Event(at=t0, label="cloud_on", apply=_bind(EvalWorld.set_solar_factor, 0.2))
        )
        events.append(
            Event(
                at=t0 + 120.0,
                label="cloud_off",
                apply=_bind(EvalWorld.set_solar_factor, 1.0),
            )
        )
    return events


def _household_and_solar(
    rng: random.Random, duration: float, solar_peak: float
) -> list[Event]:
    """Appliance steps over an AC solar day big enough to push the pool into
    export/charge around midday, so the full bidirectional loop (charge
    distribution, the AC-charge clamp, zero-crossings) is exercised."""
    return (
        _household_steps(rng, duration)
        + _solar_day(duration, solar_peak)
        + _cloud_dips(rng, duration)
    )


_SLOW_METER = "over a slow meter (fresh reading only every 10 s + ~1 s delay)"
_TRACE_PAIR = (
    "Two Venus sharing one phase, real recorded household load (RAE House 1, "
    "1 Hz trace, CC BY) over a ~0.8 s-latency meter — "
)


def build_scenarios() -> dict[str, Scenario]:
    """All evaluation scenarios, keyed by name.

    Multi-battery scenarios come in two balancer modes: plain fair-share
    (``…/fair``) and efficiency optimization (``…/eff``, exercising
    deprioritization, rotation, saturation swaps and probe handoffs).
    """
    scenarios: dict[str, Scenario] = {}

    def add(scenario: Scenario) -> None:
        scenarios[scenario.name] = scenario

    def add_modes(
        name: str,
        description: str | dict[str, str],
        *,
        eff: dict[str, float] = _EFF_MODE,
        **fields: Any,
    ) -> None:
        """Register ``name/fair`` and ``name/eff``; *description* may differ
        per mode."""
        for mode, ct_kwargs in (("fair", {}), ("eff", eff)):
            add(
                Scenario(
                    name=f"{name}/{mode}",
                    description=(
                        description
                        if isinstance(description, str)
                        else description[mode]
                    ),
                    ct_kwargs=dict(ct_kwargs),
                    **fields,
                )
            )

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
    add(
        Scenario(
            name="single_venus_steps_slow",
            description=(
                f"One Venus, stepped house load {_SLOW_METER} — coarse-sampling "
                "stress, cf. single_venus_steps"
            ),
            batteries=[_VENUS],
            duration_s=dur_steps,
            loads=list(_HOUSEHOLD_LOADS),
            build_events=lambda rng: _household_steps(rng, dur_steps),
            meter_interval_s=10.0,
            meter_latency_s=1.0,
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
            # The field setup read an HA push sensor; that latency is what
            # turns each drum disturbance into continuous hunting instead of
            # the ~10 s calm windows the real trace never showed.
            meter_latency_s=1.0,
        )
    )
    add(
        Scenario(
            name="single_venus_noisy",
            description=(
                "One Venus, pretty noisy house baseline "
                f"(±{_NOISY_BASE_NOISE:g} W jitter, no discrete appliance steps) "
                "— noise-rejection stress"
            ),
            batteries=[_VENUS],
            duration_s=dur_steps,
            base_load=list(_NOISY_BASE_LOAD),
            base_noise=_NOISY_BASE_NOISE,
            build_events=_unscripted,
        )
    )
    add(
        Scenario(
            name="single_venus_trace",
            description=(
                "One Venus, real recorded household load (RAE House 1, 1 Hz "
                "trace, CC BY) over a meter with realistic ~0.8 s latency — "
                "real-world correlated-load stress (cf. the synthetic-noise "
                "single_venus_noisy)"
            ),
            batteries=[_VENUS],
            duration_s=dur_steps,
            base_load=[_household_trace()[0][1], 0.0, 0.0],
            base_noise=_TRACE_METER_NOISE,
            build_events=lambda rng: _household_trace_events(rng, dur_steps),
            # Real load under realistic meter delay is the field condition the
            # synthetic latency-free scenarios never cover.
            meter_latency_s=0.8,
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
                _solar_day(dur_solar, solar_peak) + _cloud_dips(rng, dur_solar)
            ),
        )
    )
    add(
        Scenario(
            name="single_venus_solar_slow",
            description=(
                f"One Venus, solar day + clouds {_SLOW_METER} — coarse sampling "
                "of a moving PV setpoint, cf. single_venus_solar"
            ),
            batteries=[BatterySpec(initial_soc=0.4)],
            duration_s=dur_solar,
            base_load=[400.0, 0.0, 0.0],
            build_events=lambda rng: (
                _solar_day(dur_solar, solar_peak) + _cloud_dips(rng, dur_solar)
            ),
            meter_interval_s=10.0,
            meter_latency_s=1.0,
        )
    )

    # SoC saturation: short runs against a 5 kWh pack barely move the SoC, so
    # these use a single-unit 2.56 kWh pack started near an edge and driven
    # hard enough to hit it.
    dur_drain = 10800.0  # 3 h
    add(
        Scenario(
            name="single_venus_drain",
            description=(
                "One Venus emptying under a sustained real evening load (2.56 kWh "
                "pack, low initial SoC) — empty-saturation handoff to grid import"
            ),
            batteries=[BatterySpec(capacity_wh=2560.0, initial_soc=0.35)],
            duration_s=dur_drain,
            base_load=[_household_trace()[0][1], 0.0, 0.0],
            base_noise=_TRACE_METER_NOISE,
            build_events=lambda rng: _household_trace_events(rng, dur_drain),
            meter_latency_s=0.5,
        )
    )
    add(
        Scenario(
            name="single_venus_fill",
            description=(
                "One Venus filling under a strong solar day (2.56 kWh pack, high "
                "initial SoC) — full-saturation handoff: charge clamp + export"
            ),
            batteries=[BatterySpec(capacity_wh=2560.0, initial_soc=0.8)],
            duration_s=dur_solar,
            base_load=[300.0, 0.0, 0.0],
            build_events=lambda rng: (
                _solar_day(dur_solar, 2200.0) + _cloud_dips(rng, dur_solar)
            ),
            meter_latency_s=0.5,
        )
    )
    add(
        Scenario(
            name="single_venus_pv",
            description=(
                "One Venus, real recorded PV + load net-load (Cyprus prosumer, "
                "30 s, real cloud transients) — bidirectional charge/export "
                "tracking, cf. the synthetic single_venus_solar"
            ),
            batteries=[BatterySpec(initial_soc=0.4)],
            duration_s=dur_solar,
            base_load=[_net_trace()[0][1] * _PV_SCALE, 0.0, 0.0],
            base_noise=_TRACE_METER_NOISE,
            build_events=lambda rng: _pv_net_events(rng, dur_solar),
            meter_latency_s=0.5,
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
                _solar_day(dur_solar, solar_peak) + _cloud_dips(rng, dur_solar)
            ),
        )
    )

    # Two control laws (Venus D integer integrator, Venus C float ramp) under
    # one balancer on one phase.
    add_modes(
        "venus_d_plus_c",
        "One Venus D + one Venus C sharing one phase",
        batteries=[_VENUS_D, _VENUS],
        duration_s=dur_steps,
        loads=list(_HOUSEHOLD_LOADS),
        build_events=lambda rng: _household_steps(rng, dur_steps),
    )
    add_modes(
        "two_venus",
        "Two identical Venus sharing one phase",
        batteries=[_VENUS, _VENUS],
        duration_s=dur_steps,
        loads=list(_HOUSEHOLD_LOADS),
        build_events=lambda rng: _household_steps(rng, dur_steps),
    )
    add(
        Scenario(
            name="two_venus_slow/fair",
            description=(
                f"Two Venus sharing one phase, stepped load {_SLOW_METER} — "
                "coarse sampling with share-splitting, cf. two_venus/fair"
            ),
            batteries=[_VENUS, _VENUS],
            duration_s=dur_steps,
            loads=list(_HOUSEHOLD_LOADS),
            build_events=lambda rng: _household_steps(rng, dur_steps),
            meter_interval_s=10.0,
            meter_latency_s=1.0,
        )
    )
    # The only three-phase scenario: each unit must null its own phase with no
    # cross-phase interference.
    add(
        Scenario(
            name="phase_imbalance",
            description=(
                "Three Venus, one per phase; asymmetric per-phase base + a "
                "different appliance on each phase — per-phase distribution"
            ),
            batteries=[
                BatterySpec(phase="A"),
                BatterySpec(phase="B"),
                BatterySpec(phase="C"),
            ],
            duration_s=dur_steps,
            base_load=[300.0, 200.0, 150.0],
            loads=list(_PHASE_IMBALANCE_LOADS),
            build_events=lambda rng: _household_steps(rng, dur_steps),
        )
    )
    add_modes(
        "two_venus_noisy",
        (
            "Two Venus sharing one phase, pretty noisy house baseline "
            f"(±{_NOISY_BASE_NOISE:g} W jitter, no discrete appliance steps)"
        ),
        batteries=[_VENUS, _VENUS],
        duration_s=dur_steps,
        base_load=list(_NOISY_BASE_LOAD),
        base_noise=_NOISY_BASE_NOISE,
        build_events=_unscripted,
    )
    add_modes(
        "two_venus_trace",
        {
            "fair": _TRACE_PAIR + "fair-share splitting across both units",
            "eff": _TRACE_PAIR
            + "efficiency concentration: the 2nd Venus cuts in only on peaks",
        },
        eff=_TRACE_EFF_MODE,
        batteries=[_VENUS, _VENUS],
        duration_s=dur_steps,
        base_load=[_household_trace()[0][1], 0.0, 0.0],
        base_noise=_TRACE_METER_NOISE,
        build_events=lambda rng: _household_trace_events(rng, dur_steps),
        meter_latency_s=0.8,
    )

    # Above the base load plus typical appliance draw, so midday PV pushes the
    # pool into charging / export for stretches.
    solar_peak_house = 3000.0
    add_modes(
        "two_venus_solar",
        "Two Venus, household load + solar day curve + clouds",
        batteries=[_VENUS, _VENUS],
        duration_s=dur_solar,
        base_load=[400.0, 0.0, 0.0],
        loads=list(_HOUSEHOLD_LOADS),
        build_events=lambda rng: _household_and_solar(rng, dur_solar, solar_peak_house),
    )

    # Issue #522: under a sustained surplus the balance correction pulls the
    # healthy unit's command toward an even split with the idle full unit, and
    # part of that lands inside the firmware's input deadband — unless the full
    # unit is recognised as saturated and drops out of the share math. The
    # deadband only holds a sub-deadband command on a unit already in motion,
    # hence the seeded initial_power.
    add(
        Scenario(
            name="full_battery_low_pace",
            description=(
                "Fair distribution, both active: a full unit on the PV-export "
                "phase + a healthy unit (already charging) on another phase, "
                "sustained surplus; ramp-pace base step below the saturation "
                "min-target (issue #522). The full unit must be detected "
                "saturated so the healthy one keeps absorbing instead of having "
                "part of its command held inside the firmware deadband"
            ),
            batteries=[
                BatterySpec(
                    device_type="VNSE3-0",
                    phase="A",
                    max_charge_power=0,  # full: cannot charge
                    initial_soc=0.95,
                ),
                BatterySpec(
                    device_type="VNSE3-0",
                    phase="B",
                    initial_soc=0.4,
                    initial_power=-200.0,  # already charging when the run begins
                ),
            ],
            duration_s=900.0,
            base_load=[-250.0, 0.0, 0.0],  # ~250 W sustained PV surplus
            build_events=_unscripted,
            ct_kwargs={
                "balance_gain": 0.40,
                "balance_deadband": 30.0,
                "max_correction_per_step": 150.0,
                "pace_base_step": 30.0,
                "pace_max_step": 200.0,
                "min_target_for_saturation": 40.0,
                "saturation_alpha": 0.9,
            },
            meter_latency_s=0.5,
        )
    )

    # MIN_DC_OUTPUT below the ~80 W a channel pair needs to start (issue #600):
    # the balancer parks a starved unit at a command it cannot execute, and
    # whether that counts as saturation decides if the second unit ever gets a
    # real share. Nothing else in the suite sets MIN_DC_OUTPUT.
    dur_floor = 3600.0
    add(
        Scenario(
            name="b2500_pair_dc_floor",
            description="Two DC-only B2500 on one phase, MIN_DC_OUTPUT below "
            "their start floor",
            batteries=[
                # One unit carrying the house, the other at rest — what a
                # restart leaves behind (the reporter's 230 W against 4 W).
                # Starting both from zero would split the first correction
                # evenly and neither would ever be starved.
                replace(_B2500_PAIR, initial_power=250.0),
                _B2500_PAIR,
            ],
            duration_s=dur_floor,
            base_load=[250.0, 0.0, 0.0],
            loads=list(_HOUSEHOLD_LOADS),
            build_events=lambda rng: _household_steps(rng, dur_floor),
            ct_kwargs={"min_dc_output": 20.0},
        )
    )

    dur_mixed = 5400.0
    add_modes(
        "mixed_venus_b2500",
        "Two Venus + one DC-only B2500 with PV input",
        batteries=[_VENUS, _VENUS, _B2500],
        duration_s=dur_mixed,
        loads=list(_HOUSEHOLD_LOADS),
        build_events=lambda rng: (
            _household_steps(rng, dur_mixed)
            + _solar_day(dur_mixed, 700.0, dc_battery=2)
        ),
    )
    add_modes(
        "mixed_cadence",
        "Slow-polling V2 (3.1 s) + fast V3 (0.45 s)",
        batteries=[_VENUS_V2_SLOW, _VENUS_V3],
        duration_s=dur_steps,
        loads=list(_HOUSEHOLD_LOADS),
        build_events=lambda rng: _household_steps(rng, dur_steps),
    )
    add_modes(
        "mixed_cadence_solar",
        "Slow V2 + fast V3, household load + solar + clouds",
        batteries=[_VENUS_V2_SLOW, _VENUS_V3],
        duration_s=dur_solar,
        base_load=[400.0, 0.0, 0.0],
        loads=list(_HOUSEHOLD_LOADS),
        build_events=lambda rng: _household_and_solar(rng, dur_solar, solar_peak_house),
    )

    return scenarios
