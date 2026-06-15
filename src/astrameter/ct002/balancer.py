"""Load balancing with efficiency optimization and saturation detection."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Callable
from typing import Literal, NamedTuple, NewType

from astrameter.config.logger import logger

from .protocol import parse_int

# ---------------------------------------------------------------------------
# Net-output target: the single currency of all control logic
# ---------------------------------------------------------------------------

# An absolute net-output target in watts.  This is the ONE value every control
# policy (steer-to-zero, manual, fair-share, probing) is allowed to declare:
# "this is the net power I want the battery to deliver at the grid coupling
# point", independent of whatever the battery currently reports.
#
# Sign convention, defined here exactly once:
#     +  =  net discharge  (export to grid / serve house load)
#     -  =  net charge      (import from grid)
#
# It is a distinct type (``NewType``) so a net-output *target* can never be
# silently mixed with a grid-meter *reading* -- the relative, sign-loaded delta
# a battery integrates into its own output.  Control authors produce a
# ``NetOutputW``; the conversion to a reading happens in exactly one audited
# place, :func:`to_grid_reading`.
NetOutputW = NewType("NetOutputW", float)


def to_grid_reading(target: NetOutputW, reported: float) -> float:
    """Convert an absolute net-output *target* into the grid-meter reading.

    This is the single boundary between the balancer's control currency
    (:class:`NetOutputW`, an absolute net output) and what each battery
    actually consumes: a *grid-meter reading* it adds to its own output via
    ``new_output = reported + reading``.  Concretely::

        reading = target - reported

    so that ``reported + reading == target`` — the battery lands on the
    absolute target we asked for, regardless of where it started.

    The returned value is a meter reading by convention: **positive = grid
    import** (the battery should raise net output by this much, i.e. discharge
    more or charge less), **negative = grid export** (lower net output).  It is
    the relative/integral/sign-loaded quantity that used to be hand-computed at
    every call site; keeping it in one place means "increase discharge vs
    reduce charge" is no longer something any control author has to reason
    about.

    The caller is responsible for the phase split (see
    :meth:`LoadBalancer._split_by_phase`), which distributes this scalar
    reading across phases A/B/C.
    """
    return float(target) - reported


def _report_weight(report: dict) -> float:
    """Per-battery fair-share weight from a report dict (defaults to 1.0).

    A missing key (or an explicit ``None``) means "neutral" and maps to 1.0;
    an explicit ``0.0`` is preserved (the battery takes no share). The setter
    keeps real weights in ``[0, 10]``.
    """
    weight = report.get("weight", 1.0)
    return 1.0 if weight is None else float(weight)


# Ramp pacing (issue #458): the battery only grows its own ramp gain while
# an error persists, and its spike filter ignores reading jumps over 50 W
# whose own output moved less than 20 W.  We mirror that: the pacing cap
# doubles per reference second, and only when the battery's reported output
# moved at least this far in the commanded direction since the previous
# paced poll (a non-moving battery — startup delay, saturation — keeps the
# cap at the base step, which also keeps successive readings inside the
# spike filter).
#
# The threshold must sit BELOW the battery firmware's guaranteed minimum
# step on a *constant* reading (issue #469 follow-up): the HMG ramp law
# steps ``sqrt(g² - ref²) + 10`` per cycle, and when its internal ``ref``
# happens to capture the reading itself (a coin flip on the sign of the
# last pre-step noise sample) the step collapses to exactly 10 W.  With a
# 20 W threshold that locked the loop at 10 W/poll for an entire step
# response — the cap never grew because the battery "wasn't tracking", and
# the battery never accelerated because the paced reading never rose.  5 W
# still rejects a genuinely stalled battery (startup delay, saturation:
# 0 W movement) while letting the firmware's worst-case crawl bootstrap
# the cap.
PACE_TRACKING_DELTA_W = 5.0
PACE_GROWTH_FACTOR = 2.0
# Reference poll interval the pace caps are defined against: the configured
# ``pace_base_step`` / ``pace_max_step`` are watts per reference second, and
# the per-poll clamp scales with the consumer's observed inter-poll time so
# that fast pollers can't integrate the same per-poll reading into a higher
# W/s slew (a 0.45 s V3 given 200 W per poll moves ~440 W/s against a meter
# that refreshes once a second — the dominant overshoot source in the
# mixed-cadence scenarios).  The scale is clamped at 1.0: slow pollers keep
# the per-poll cap (their staleness grows with their interval, so widening
# the clamp proportionally would re-introduce the stale-feedback overshoot
# pacing exists to bound).
PACE_REFERENCE_DT = 1.0

# Adaptive grid-state predictor (see BalancerConfig.grid_predict_trust and
# LoadBalancer._predict_control_grid).  The meter trust is bounded to
# ``[PRED_TRUST_MIN, PRED_TRUST_MAX]`` and adapted per fresh meter sample whose
# innovation clears ``PRED_INNOVATION_GATE_W`` (above the steady-state
# meter-noise floor, so random sign flips at the null don't adapt it).  The
# raise is an *additive* step so trust only climbs under a *sustained*
# same-sign innovation run — the signature of a genuine lasting disturbance (a
# cloud, a big load step) — while the shrink is a *multiplicative* cut so a
# single sign flip (the signature of latency-driven hunting, or an oscillatory
# external load) collapses it.  This asymmetry is what lets one law both
# track real steps fast and stay rock-steady against a hunting load, with no
# per-meter tuning.  The band is widened toward a near-fully-trusting ceiling
# with a brisk raise (so a real step is caught in a couple of fresh samples,
# recovering the self-consumption energy a slower ramp leaves on the grid),
# paired with a softer shrink that still halves trust on a hunt — the
# steering-evaluation suite tunes the pair against the ramp-pacing/oscillation
# damping defaults below.
PRED_TRUST_MIN = 0.15
PRED_TRUST_MAX = 0.9
PRED_TRUST_RAISE_STEP = 0.2
PRED_TRUST_SHRINK = 0.5
PRED_INNOVATION_GATE_W = 40.0

EFFICIENCY_HYSTERESIS_FACTOR = 1.2
# Seconds to suppress saturation checks after a battery is promoted from
# deprioritized to active.  Covers the physical ramp-up time of the
# inverter; the grace is also cleared early once the battery proves it
# can produce meaningful output.
SATURATION_GRACE_SECONDS = 90
# A battery that still produces effectively nothing after prolonged grace under
# a real target is overwhelmingly likely to be empty/full/limited, not merely
# ramping up. In that case we bypass the remaining grace window and mark it
# saturated immediately so the balancer can rotate to a healthy unit.
SATURATION_STALL_TIMEOUT_SECONDS = 60.0
# Reference poll interval (seconds) at which the configured ``SATURATION_ALPHA``
# and ``SATURATION_DECAY_FACTOR`` apply one full step.  The EMA is time-
# weighted against this reference so that batteries polling at different
# cadences (e.g. V3 at ~0.45 s vs V2 at ~3.1 s) converge to the same
# saturation score under the same physical conditions.  Chosen to match
# the ~1 Hz cadence the previous per-sample defaults were implicitly tuned
# against.
SATURATION_REFERENCE_DT = 1.0
# If more than this many seconds pass between saturation updates (e.g. a
# battery drops off the network), treat the next sample as a fresh start
# rather than dosing the EMA with a huge rise or decay step.
SATURATION_LONG_GAP_SECONDS = 30.0

# ---------------------------------------------------------------------------
# Device capabilities — the single source of truth for every device-type
# decision in the balancer.  A battery is classified from its reported
# device-type string into three independent capabilities; all downstream
# policy (AC-charge eligibility, the MIN_DC_OUTPUT wake floor) is derived
# from these rather than from ad-hoc prefix checks.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DeviceCapabilities:
    """What a battery model can physically do.

    - ``has_builtin_inverter``: the battery produces its own AC output, so it
      never depends on a separate inverter that could sleep at low DC output.
    - ``has_ac_input``: the battery can be charged from AC (Venus lineup).
    - ``has_dc_input``: the battery has a DC (solar) input.
    """

    has_builtin_inverter: bool
    has_ac_input: bool
    has_dc_input: bool


def device_capabilities(device_type: str) -> DeviceCapabilities:
    """Classify *device_type* into its :class:`DeviceCapabilities`.

    Known Marstek families:

    - Venus A/D (``VNSA``/``VNSD``): built-in inverter, AC input, *and* an
      extra DC input.  Checked before the generic ``VNS`` branch because
      ``"VNSA".startswith("VNS")``.
    - Other Venus (``HMG*``, ``VNSE3``, ...): built-in inverter + AC input.
    - Jupiter (``HMN*``/``HMM*``/``JPLS*``): a DC battery, but with its own
      built-in inverter — so it does *not* depend on an external inverter.
    - B2500 family (``HMA*``/``HMJ*``/``HMK*``): DC input feeding a *separate*
      inverter, with no built-in inverter and no AC input.

    Unknown / future / empty device types are assumed to be modern AC-coupled
    batteries (built-in inverter + AC input, no separate DC inverter), so they
    are never floored by MIN_DC_OUTPUT and are treated as AC-chargeable.
    """
    dt = (device_type or "").upper()
    if dt.startswith(("VNSA", "VNSD")):
        return DeviceCapabilities(True, True, True)
    if dt.startswith(("HMG", "VNS")):
        return DeviceCapabilities(True, True, False)
    if dt.startswith(("HMN", "HMM", "JPLS")):
        return DeviceCapabilities(True, False, True)
    if dt.startswith(("HMA", "HMJ", "HMK")):
        return DeviceCapabilities(False, False, True)
    return DeviceCapabilities(True, True, False)


def _is_ac_chargeable(device_type: str) -> bool:
    """True iff *device_type* can be charged from AC (the Venus lineup).

    Unrecognized/empty device types are assumed AC-chargeable (see
    :func:`device_capabilities`).  This is used to exclude DC-only batteries
    (B2500 family) from charge distribution under a grid surplus — see issue
    #338.
    """
    return device_capabilities(device_type).has_ac_input


def _needs_dc_output_floor(device_type: str) -> bool:
    """True iff *device_type* depends on a sleep-prone *external* inverter.

    Such a battery has no built-in inverter and no AC input, so its only way
    to stay awake is to keep discharging through its DC-fed external inverter.
    This is exactly the B2500 family (``HMA*``/``HMJ*``/``HMK*``); Jupiter and
    Venus are excluded because they have a built-in inverter.
    """
    caps = device_capabilities(device_type)
    return not caps.has_ac_input and not caps.has_builtin_inverter


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BalancerConfig:
    """Tuning knobs for :class:`LoadBalancer`."""

    fair_distribution: bool = True
    balance_gain: float = 0.2
    # Share-rebalance deadband.  Kept above the battery firmware's own ±20 W
    # input deadband so the balancer never chases share errors the battery
    # would ignore anyway (issue #458).
    balance_deadband: float = 25
    error_boost_threshold: float = 150
    error_boost_max: float = 0.5
    error_reduce_threshold: float = 20
    max_correction_per_step: float = 80
    max_target_step: float = 0
    min_efficient_power: float = 0
    probe_min_power: float = 80
    efficiency_rotation_interval: float = 900
    efficiency_fade_alpha: float = 0.15
    efficiency_saturation_threshold: float = 0.4
    # Minimum net discharge (W) to hold an external-inverter DC battery at so
    # its inverter doesn't switch off at 0 W and sleep.  0 disables.  Only
    # applied to batteries selected by ``_needs_dc_output_floor`` (B2500
    # family) unless overridden per-device.  See issue #425.
    min_dc_output: float = 0
    # Ramp pacing for the auto path (issue #458).  The battery firmware runs
    # its own gain-scheduled ramp on the reading we send, stepping at most
    # ``min(GAIN[ramp], |reading|)`` per poll, where GAIN accelerates from
    # ~50 W to ~400 W while an error persists.  With one or two polls of
    # feedback lag an uncapped reading lets that accelerated ramp overshoot
    # by hundreds of watts.  Pacing clamps each consumer's sent reading to a
    # per-consumer cap that starts at ``pace_base_step`` (the firmware's
    # first-step gain), grows x2 toward ``pace_max_step`` only while the
    # battery is observed tracking the command, follows the error back down,
    # and resets on direction reversal.  ``pace_base_step = 0`` disables.
    # Defaults (30/100) were tuned with the steering evaluation suite
    # (``python -m astrameter.simulator.evaluation``): the tighter caps trade a
    # little settling speed for a large drop in worst-case overshoot and battery
    # travel across the scenario matrix (the faster grid-state predictor below
    # keeps real-step reaction quick despite the lower cap).  A base step below
    # ~30 W or a max below ~80 W starts re-introducing near-null hunting (the
    # grid-p2p guardrail regresses), so the caps are held just above that.
    pace_base_step: float = 30
    pace_max_step: float = 100
    # Oscillation-gated damping (issue #473).  Under meter latency the gain-1
    # grid-following residual limit-cycles: the controller keeps reacting to a
    # stale reading that doesn't yet reflect its last command, so it overshoots
    # and the grid hunts continuously.  We track an EMA of how often a
    # consumer's residual *reverses sign* (the signature of hunting, not of a
    # genuine load step, which holds one sign) and scale the residual down by up
    # to ``osc_damp_max`` as that score rises.  A clean step keeps full gain
    # (sign constant -> score ~0 -> factor ~1), so step reaction is unchanged;
    # only a hunting loop is damped.  ``osc_damp_max = 0`` disables.
    osc_damp_max: float = 0.95
    osc_damp_alpha: float = 0.3
    osc_damp_decay: float = 0.05
    # Only near-null corrections are damped: a residual above this magnitude is
    # a genuine demand step (kettle, solar ramp), not hunting, so it passes
    # through at full gain and reacts immediately.  Keeps the damper from
    # bleeding a real step response just because the loop was hunting before it.
    osc_damp_threshold: float = 300
    # Adaptive grid-state predictor.  The controller acts on a *predicted* grid
    # rather than the raw meter: every poll the prediction is advanced by the
    # pool's own freshly-reported output change, and on each fresh meter sample
    # it is pulled toward the reading by an *adaptive* trust.  The battery
    # reports are fresher than the grid meter (which is delayed by its poll
    # interval plus transport/measurement latency), so crediting the pool's
    # just-delivered output reconstructs the grid the meter has not yet shown —
    # the controller stops re-issuing a correction already in flight, the
    # dominant source of overshoot and latency-driven limit-cycling.  Because it
    # advances by the *reported* (actual) output, battery non-ideality (ramp,
    # deadband, saturation) is captured for free.  The trust is learned online
    # from the innovation's sign pattern (see ``_predict_control_grid``), so the
    # loop self-tunes to each meter's latency instead of the user configuring a
    # throttle/pacing per powermeter.  ``0`` disables it (act on the raw meter);
    # any positive value only *seeds* the self-adapting trust, so the exact
    # value barely matters — ``0.5`` is a neutral default.
    grid_predict_trust: float = 0.5

    def __post_init__(self) -> None:
        def _clamp(name: str, lo: float, hi: float) -> None:
            v = getattr(self, name)
            clamped = max(lo, min(hi, v))
            if clamped != v:
                object.__setattr__(self, name, clamped)

        _clamp("balance_gain", 0.0, 1.0)
        _clamp("balance_deadband", 0, float("inf"))
        _clamp("error_boost_threshold", 0, float("inf"))
        _clamp("error_boost_max", 0.0, float("inf"))
        _clamp("error_reduce_threshold", 0, float("inf"))
        _clamp("max_correction_per_step", 0, float("inf"))
        _clamp("max_target_step", 0, float("inf"))
        _clamp("min_efficient_power", 0, float("inf"))
        _clamp("probe_min_power", 0, float("inf"))
        _clamp("efficiency_rotation_interval", 1, float("inf"))
        _clamp("efficiency_fade_alpha", 0.01, 1.0)
        _clamp("efficiency_saturation_threshold", 0.0, 1.0)
        _clamp("min_dc_output", 0, float("inf"))
        _clamp("pace_base_step", 0, float("inf"))
        _clamp("pace_max_step", self.pace_base_step, float("inf"))
        _clamp("osc_damp_max", 0.0, 1.0)
        _clamp("osc_damp_alpha", 0.0, 1.0)
        _clamp("osc_damp_decay", 0.0, 1.0)
        _clamp("osc_damp_threshold", 0.0, float("inf"))
        _clamp("grid_predict_trust", 0.0, 1.0)


# ---------------------------------------------------------------------------
# Consumer mode (auto / manual / inactive)
# ---------------------------------------------------------------------------


class ConsumerMode(NamedTuple):
    """Describes a consumer's current control mode."""

    mode: Literal["auto", "manual", "inactive"]
    manual_value: float = 0.0


# ---------------------------------------------------------------------------
# Per-consumer state
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BalancerConsumerState:
    """Bundled per-consumer state owned by LoadBalancer."""

    last_target: float | None = None
    # Absolute net-output target (NetOutputW currency) the control path
    # intended for this consumer, recorded *before* wire pacing.  While
    # ``last_target`` is the (paced) reading actually sent, ``last_intent``
    # preserves the control direction — the cross-talk chrg/dchrg
    # attribution uses it to filter involuntary outputs such as PV
    # passthrough from a full battery (issue #376).
    last_intent: float | None = None
    fade_weight: float = 1.0
    # Ramp-pacing state (see BalancerConfig.pace_base_step): the current
    # cap on the sent reading in W per reference second, the sign of the
    # last paced reading, the battery's reported power at the last pacing
    # step (used to detect whether it is tracking the command before
    # growing the cap), and the wall-clock time of the last paced poll
    # (0.0 = none yet) used to scale the per-poll clamp to the consumer's
    # cadence.
    pace_cap: float = 0.0
    pace_sign: int = 0
    pace_prev_reported: float | None = None
    pace_last_at: float = 0.0
    # Oscillation-gated damping (see BalancerConfig.osc_damp_max): accumulated
    # reversal score (1.0 = sustained hunting, 0.0 = steady) and the sign of the
    # last non-zero residual that fed it.
    osc_score: float = 0.0
    osc_last_sign: int = 0
    saturation_score: float = 0.0
    saturation_grace_until: float = 0.0
    saturation_grace_started_at: float = 0.0
    # Wall-clock timestamp of the most recent saturation EMA step for this
    # consumer. 0.0 is a sentinel meaning "no prior update"; it also flags
    # the first post-grace sample, so the next update re-seeds instead of
    # applying stale dt.
    last_saturation_update: float = 0.0


@dataclasses.dataclass
class ProbeState:
    """Tracks an in-flight efficiency handoff."""

    candidate_id: str
    active_ids: tuple[str, ...]
    backup_ids: tuple[str, ...]
    restore_active_ids: tuple[str, ...]
    deadline: float
    started_at: float
    proof_samples: int = 0
    requested_power_abs: float = 0.0


# ---------------------------------------------------------------------------
# Saturation tracker
# ---------------------------------------------------------------------------


class SaturationTracker:
    """Time-weighted EMA saturation detector with grace periods.

    A saturation score of 1.0 means the actuator cannot follow its target
    (e.g. battery full/empty); 0.0 means it is tracking well.

    The EMA is weighted against :data:`SATURATION_REFERENCE_DT` so that
    batteries polling at different cadences converge to the same score
    under the same physical conditions.  Concretely, for a real
    inter-sample interval ``dt`` the effective per-update weight is
    ``1 - (1 - alpha) ** (dt / dt_ref)`` and the decay is
    ``decay_factor ** (dt / dt_ref)``.  At ``dt == dt_ref`` both reduce
    to the previous per-sample formulas.

    State is stored externally in :class:`BalancerConsumerState` objects;
    this class holds only configuration and algorithm logic.
    """

    def __init__(
        self,
        alpha: float,
        min_target: float,
        decay_factor: float,
        stall_timeout_seconds: float,
        *,
        enabled: bool = True,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._enabled = enabled
        self._alpha = max(0.01, min(1.0, alpha))
        self._min_target = max(1, min_target)
        self._decay_factor = max(0.0, min(1.0, decay_factor))
        self._stall_timeout_seconds = max(0.0, stall_timeout_seconds)

    def update(
        self, state: BalancerConsumerState, last_target: float | None, actual: float
    ) -> None:
        """Update the saturation score for a consumer."""
        if not self._enabled or last_target is None:
            return
        now = self._clock()
        target_abs = abs(last_target)
        # Grace period handling
        if state.saturation_grace_until > 0:
            if now < state.saturation_grace_until:
                if abs(actual) >= self._min_target:
                    state.saturation_grace_until = 0.0
                    state.saturation_grace_started_at = 0.0
                    # Re-seed so the first post-grace update applies one
                    # reference-period step rather than a stale dt dose.
                    state.last_saturation_update = 0.0
                elif (
                    target_abs >= self._min_target
                    and state.saturation_grace_started_at > 0
                    and now - state.saturation_grace_started_at
                    >= self._stall_timeout_seconds
                ):
                    state.saturation_score = 1.0
                    state.saturation_grace_until = 0.0
                    state.saturation_grace_started_at = 0.0
                    state.last_saturation_update = 0.0
                    return
                else:
                    return
            else:
                state.saturation_grace_until = 0.0
                state.saturation_grace_started_at = 0.0
                state.last_saturation_update = 0.0
        # Detect sign reversal: target says one direction, actual is still
        # in the opposite direction.  The battery is healthy but ramping to
        # the new direction — not saturated.  Treat like low-target (decay).
        target_sign = 1 if last_target > 0 else (-1 if last_target < 0 else 0)
        actual_sign = 1 if actual > 0 else (-1 if actual < 0 else 0)
        sign_reversing = (
            target_sign != 0 and actual_sign != 0 and target_sign != actual_sign
        )
        # Compute elapsed time since the previous EMA step with guards.
        # First sample (prev_t == 0) is treated as a full reference-period
        # step so a cold start still responds to the very first poll; this
        # is the "option (b)" seeding described in the class docstring.
        # A backwards clock (NTP correction) is clamped to zero; a long
        # gap (battery offline) is dropped and re-seeded so we never dose
        # the EMA with hundreds of seconds of rise or decay.
        prev_t = state.last_saturation_update
        if prev_t <= 0.0:
            prev_t = now - SATURATION_REFERENCE_DT
        dt = max(0.0, now - prev_t)
        state.last_saturation_update = now
        if dt == 0.0:
            return
        if dt > SATURATION_LONG_GAP_SECONDS:
            return
        ratio = dt / SATURATION_REFERENCE_DT
        if target_abs < self._min_target or sign_reversing:
            prev = state.saturation_score
            if prev > 0:
                decayed = prev * (self._decay_factor**ratio)
                if decayed < 0.001:
                    state.saturation_score = 0.0
                else:
                    state.saturation_score = decayed
            return
        inst_saturation = 1.0 if abs(actual) < self._min_target else 0.0
        alpha_eff = 1.0 - (1.0 - self._alpha) ** ratio
        prev = state.saturation_score
        state.saturation_score = alpha_eff * inst_saturation + (1 - alpha_eff) * prev

    def get(self, state: BalancerConsumerState) -> float:
        return state.saturation_score

    def set_grace(self, state: BalancerConsumerState, deadline: float) -> None:
        state.saturation_grace_until = deadline
        state.saturation_grace_started_at = self._clock()
        # Pause tracking until grace ends; the next real update will
        # re-seed via the prev_t <= 0 path.
        state.last_saturation_update = 0.0

    def clear(self, state: BalancerConsumerState) -> None:
        state.saturation_score = 0.0
        state.saturation_grace_until = 0.0
        state.saturation_grace_started_at = 0.0
        state.last_saturation_update = 0.0


# ---------------------------------------------------------------------------
# Load balancer
# ---------------------------------------------------------------------------


class LoadBalancer:
    """Distributes demand across consumers with efficiency and fairness.

    Owns the full target-allocation pipeline: inactive steering, manual
    override, saturation tracking, efficiency deprioritization with
    priority rotation, EMA fade transitions, fair-share distribution
    with balance correction, and phase-aware splitting.
    """

    def __init__(
        self,
        config: BalancerConfig,
        saturation_alpha: float,
        saturation_min_target: float,
        saturation_decay_factor: float,
        saturation_grace_seconds: float,
        saturation_stall_timeout_seconds: float,
        *,
        saturation_enabled: bool = True,
        clock: Callable[[], float] | None = None,
        reset_fn: Callable[[], None] | None = None,
    ) -> None:
        self._clock = clock or time.time
        self._cfg = config
        self._saturation = SaturationTracker(
            alpha=saturation_alpha,
            enabled=saturation_enabled,
            min_target=saturation_min_target,
            decay_factor=saturation_decay_factor,
            stall_timeout_seconds=saturation_stall_timeout_seconds,
            clock=self._clock,
        )
        self._saturation_grace_seconds = max(0.0, saturation_grace_seconds)
        # Optional: called after every probe commit / rejection so
        # post-handoff state cannot drag in stale pre-probe EMA values.
        # Injected by CT002 at construction.
        self._reset_fn = reset_fn
        self._consumers: dict[str, BalancerConsumerState] = {}
        self._deprioritized: set[str] = set()
        self._priority: list[str] = []
        self._last_rotation: float = self._clock()
        self._cache_sample: tuple | None = None
        self._cache_result: dict[str, float] | None = None
        self._probe_state: ProbeState | None = None
        self._probe_timeout_seconds = max(0.0, saturation_grace_seconds)
        self._probe_success_threshold = max(1.0, float(saturation_min_target))
        self._post_probe_fade_until = 0.0
        self._post_probe_fade_ids: set[str] = set()
        # Latch so the "surplus with no AC-chargeable battery" notice is
        # logged once per transition into that state, not every tick.
        self._all_dc_surplus_warned: bool = False
        # Adaptive grid-state observer (see BalancerConfig.grid_predict_trust).
        # ``_pred_grid`` is the estimate of the *instantaneous* grid the control
        # path acts on; ``_pred_meter`` models what the *latent* meter currently
        # reads (so a fresh reading's innovation isolates genuine disturbances
        # from corrections still in flight); ``_pred_pool_output`` is the pool's
        # last-seen reported output (its per-call delta advances both estimates);
        # ``_pred_sample_id`` flags a genuinely fresh meter reading; and
        # ``_pred_catchup`` is the online estimate of how fast the meter absorbs
        # the pool's output (the learned meter responsiveness — see
        # ``_predict_control_grid``).
        self._pred_grid: float | None = None
        self._pred_pool_output: float = 0.0
        self._pred_sample_id: tuple | None = None
        # Adaptive meter trust and the sign of the last significant innovation
        # that drove it (see ``_predict_control_grid``).
        self._pred_trust: float = 0.0
        self._pred_innov_sign: int = 0

    def _get_consumer(self, consumer_id: str) -> BalancerConsumerState:
        state = self._consumers.get(consumer_id)
        if state is None:
            state = BalancerConsumerState()
            self._consumers[consumer_id] = state
        return state

    def _invalidate_efficiency_cache(self) -> None:
        self._cache_sample = None
        self._cache_result = None

    def _probe_participants(self) -> set[str]:
        if self._probe_state is None:
            return set()
        return set(self._probe_state.active_ids) | set(self._probe_state.backup_ids)

    def _effective_probe_min_power(self) -> float:
        return max(self._probe_success_threshold, self._cfg.probe_min_power)

    def _next_probe_requested_abs(
        self, current_requested_abs: float, ceiling: float
    ) -> float:
        ceiling = max(0.0, ceiling)
        base_step = max(1.0, self._probe_success_threshold * 0.25)
        if current_requested_abs <= 0:
            return min(ceiling, base_step)
        return min(
            ceiling,
            max(current_requested_abs + base_step, current_requested_abs * 1.35),
        )

    def _clear_probe_state(self, reason: str) -> None:
        if self._probe_state is None:
            return
        logger.info("Efficiency: ending probe (%s)", reason)
        self._probe_state = None
        self._invalidate_efficiency_cache()

    def _clear_post_probe_fade(self) -> None:
        self._post_probe_fade_until = 0.0
        self._post_probe_fade_ids.clear()

    def _set_consumer_grace(self, consumer_id: str, deadline: float) -> None:
        self._saturation.set_grace(self._get_consumer(consumer_id), deadline)

    def _clear_consumer_grace(self, consumer_id: str) -> None:
        state = self._get_consumer(consumer_id)
        state.saturation_grace_until = 0.0
        state.saturation_grace_started_at = 0.0

    def _begin_probe(
        self,
        candidate_id: str,
        active_ids: tuple[str, ...],
        backup_ids: tuple[str, ...],
        restore_active_ids: tuple[str, ...],
        now: float,
    ) -> None:
        deadline = now + self._probe_timeout_seconds
        self._probe_state = ProbeState(
            candidate_id=candidate_id,
            active_ids=active_ids,
            backup_ids=backup_ids,
            restore_active_ids=restore_active_ids,
            deadline=deadline,
            started_at=now,
        )
        for cid in set(active_ids) | set(backup_ids):
            self._get_consumer(cid).fade_weight = 1.0
        self._clear_post_probe_fade()
        self._saturation.clear(self._get_consumer(candidate_id))
        self._set_consumer_grace(candidate_id, deadline)
        logger.info(
            "Efficiency: probing consumer %s with backups %s until %.1fs",
            candidate_id[:16],
            [cid[:16] for cid in backup_ids],
            self._probe_timeout_seconds,
        )
        self._invalidate_efficiency_cache()

    def _commit_probe(self, reports: dict, now: float, actual: float) -> None:
        probe = self._probe_state
        if probe is None:
            return
        participants = [
            cid for cid in (*probe.active_ids, *probe.backup_ids) if cid in reports
        ]
        total_actual = sum(
            abs(parse_int(reports.get(cid, {}).get("power", 0))) for cid in participants
        )
        if total_actual > 0:
            for cid in participants:
                actual_share = abs(parse_int(reports.get(cid, {}).get("power", 0)))
                self._get_consumer(cid).fade_weight = actual_share / total_actual
        else:
            active_count = max(1, len(probe.active_ids))
            for cid in probe.active_ids:
                self._get_consumer(cid).fade_weight = 1.0 / active_count
            for cid in probe.backup_ids:
                self._get_consumer(cid).fade_weight = 0.0
        self._post_probe_fade_until = now + min(5.0, self._probe_timeout_seconds)
        self._post_probe_fade_ids = set(participants)
        self._clear_consumer_grace(probe.candidate_id)
        self._probe_state = None
        self._last_rotation = now
        logger.info(
            "Efficiency: probe succeeded for %s at %.0fW",
            probe.candidate_id[:16],
            actual,
        )
        self._invalidate_efficiency_cache()
        # Reset powermeter wrapper state so the post-handoff balance runs
        # against a fresh baseline instead of an EMA that still carries
        # pre-probe state (including the transient zero-crossing that
        # happens while the candidate ramps up and the backup drops out).
        #
        # Timing note: ``_commit_probe`` runs inside
        # ``_resolve_probe_state`` which is called from
        # ``_compute_efficiency_deprioritized`` from
        # ``_compute_auto_target`` — the current ``compute_target`` call
        # has already captured ``grid_total`` as a parameter, so the
        # reset here does NOT affect the current tick's target.  It only
        # affects the NEXT powermeter reading, which is the desired
        # semantics.
        if self._reset_fn is not None:
            self._reset_fn()

    def _reject_probe(self, now: float, reason: str) -> None:
        probe = self._probe_state
        if probe is None:
            return
        candidate_state = self._get_consumer(probe.candidate_id)
        candidate_state.saturation_score = max(candidate_state.saturation_score, 1.0)
        candidate_state.fade_weight = 0.0
        for cid in probe.restore_active_ids:
            self._get_consumer(cid).fade_weight = 1.0
        self._clear_consumer_grace(probe.candidate_id)
        self._clear_post_probe_fade()
        remaining = [
            cid
            for cid in self._priority
            if cid not in probe.restore_active_ids and cid != probe.candidate_id
        ]
        self._priority = (
            list(probe.restore_active_ids) + remaining + [probe.candidate_id]
        )
        self._probe_state = None
        logger.info(
            "Efficiency: probe rejected for %s (%s), restoring backups %s",
            probe.candidate_id[:16],
            reason,
            [cid[:16] for cid in probe.backup_ids],
        )
        self._invalidate_efficiency_cache()
        # See _commit_probe — same rationale: force a fresh baseline
        # after the probe window ends.
        if self._reset_fn is not None:
            self._reset_fn()

    def _resolve_probe_state(
        self, reports: dict, now: float, grid_total: float
    ) -> bool:
        probe = self._probe_state
        if probe is None:
            return False
        participants = set(probe.active_ids) | set(probe.backup_ids)
        missing = [cid for cid in participants if cid not in reports]
        if missing:
            self._clear_probe_state(
                f"participants disappeared: {[cid[:16] for cid in missing]}"
            )
            return True
        actual = parse_int(reports.get(probe.candidate_id, {}).get("power", 0))
        desired_total = (
            sum(parse_int(report.get("power", 0)) for report in reports.values())
            + grid_total
        )
        probe_success_threshold = self._probe_success_threshold
        demand_sign = 1 if desired_total > 0 else -1 if desired_total < 0 else 0
        actual_sign = 1 if actual > 0 else -1 if actual < 0 else 0
        if (
            demand_sign != 0
            and actual_sign == demand_sign
            and abs(actual) >= probe_success_threshold
        ):
            probe.proof_samples += 1
        else:
            probe.proof_samples = 0
        if probe.proof_samples >= 2:
            self._commit_probe(reports, now, actual)
            return True
        if now >= probe.deadline:
            self._reject_probe(now, "timeout before meaningful output")
            return True
        return False

    def _compute_desired_contribution(
        self,
        consumer_id: str,
        reports: dict,
        weights: dict[str, float],
        desired_total: float,
    ) -> float:
        total_weight = sum(weights.get(cid, 0.0) for cid in reports)
        if total_weight > 0:
            fair_share = desired_total * weights.get(consumer_id, 0.0) / total_weight
        else:
            fair_share = desired_total / max(1, len(reports))
        if (
            not self._cfg.fair_distribution
            or consumer_id not in reports
            or (
                self._cfg.balance_deadband > 0
                and abs(desired_total) < self._cfg.balance_deadband
            )
        ):
            return fair_share
        return self._balance_correction(consumer_id, reports, weights, fair_share)

    def _compute_probe_target(
        self,
        consumer_id: str | None,
        reports: dict,
        grid_total: float,
        eff_part: dict[str, float],
    ) -> list[float] | None:
        probe = self._probe_state
        if probe is None or consumer_id is None:
            return None
        candidate_id = probe.candidate_id
        if candidate_id not in reports:
            return None
        support_reports = {
            cid: reports[cid]
            for cid in (
                *probe.backup_ids,
                *(cid for cid in probe.active_ids if cid != candidate_id),
            )
            if cid in reports
        }
        if consumer_id != candidate_id and consumer_id not in support_reports:
            return None

        desired_total = (
            sum(parse_int(report.get("power", 0)) for report in reports.values())
            + grid_total
        )
        state = self._get_consumer(consumer_id)
        probe_actual = parse_int(reports.get(candidate_id, {}).get("power", 0))
        probe_ceiling = max(abs(desired_total), self._cfg.probe_min_power)

        if consumer_id == candidate_id:
            next_requested_abs = self._next_probe_requested_abs(
                probe.requested_power_abs, probe_ceiling
            )
            desired_probe = 0.0
            if desired_total > 0:
                desired_probe = max(
                    abs(probe_actual),
                    next_requested_abs,
                )
            elif desired_total < 0:
                desired_probe = -max(
                    abs(probe_actual),
                    next_requested_abs,
                )
            elif probe.requested_power_abs > 0:
                desired_probe = max(
                    0.0, probe.requested_power_abs - self._probe_success_threshold
                )
            if desired_total < 0 and desired_probe > 0:
                desired_probe = -desired_probe
            probe.requested_power_abs = abs(desired_probe)
            reading = to_grid_reading(NetOutputW(desired_probe), probe_actual)
            state.last_target = reading
            state.last_intent = desired_probe
            return self._split_by_phase(reading, {candidate_id: reports[candidate_id]})

        backup_weights = {
            cid: max(0.01, eff_part.get(cid, 1.0))
            * _report_weight(reports.get(cid, {}))
            for cid in support_reports
        }
        qualified_probe_actual = probe_actual if probe.proof_samples > 0 else 0
        desired = self._compute_desired_contribution(
            consumer_id,
            support_reports,
            backup_weights,
            desired_total - qualified_probe_actual,
        )
        reported = parse_int(support_reports.get(consumer_id, {}).get("power", 0))
        reading = to_grid_reading(NetOutputW(desired), reported)
        state.last_target = reading
        state.last_intent = desired
        return self._split_by_phase(reading, support_reports, backup_weights)

    # ------------------------------------------------------------------
    # Primary interface
    # ------------------------------------------------------------------

    def compute_target(
        self,
        consumer_id: str | None,
        consumer_mode: ConsumerMode,
        all_reports: dict,
        grid_total: float,
        inactive: frozenset[str],
        manual: frozenset[str],
        sample_id: tuple = (),
    ) -> list[float]:
        """Return ``[phase_A, phase_B, phase_C]`` target for *consumer_id*.

        *all_reports* contains every known consumer's report dict.
        *inactive* / *manual* are the sets of paused and manual-override
        consumer IDs; this method filters internally.
        *sample_id* identifies the current meter reading for cache keying.
        """
        # --- Inactive consumer: steer to zero ---
        if consumer_mode.mode == "inactive":
            return self._steer_to_zero(consumer_id, all_reports)

        # Reports excluding inactive consumers
        active_reports = {
            cid: r for cid, r in all_reports.items() if cid not in inactive
        }

        # Update saturation (skip manual, probe, and deprioritized consumers).
        #
        # Deprioritized consumers are steered toward zero, but while their
        # ``_fade_efficiency_weights`` EMA is still winding down from 1.0
        # their ``last_target`` carries a transient, non-zero value from
        # the fade path (see ``_compute_auto_target``).  Feeding that
        # transient into the saturation EMA causes a false-positive
        # "cannot follow target" spike for a battery that's really just
        # in the process of being phased out — and with the time-weighted
        # EMA that spike is large enough to stay above the swap threshold
        # for many ticks, locking ``_maybe_force_swap_saturated`` out of
        # ever promoting the consumer back.  Simply skipping the update
        # while the consumer is deprioritized leaves the score pinned to
        # whatever the symmetric clear in ``_compute_efficiency_deprioritized``
        # set it to (zero), which is exactly what the swap path expects
        # for a "healthy" candidate.
        state = self._get_consumer(consumer_id) if consumer_id else None
        last_target = state.last_target if state else None
        if (
            consumer_id
            and state
            and consumer_id in active_reports
            and consumer_mode.mode != "manual"
            and consumer_id not in self._probe_participants()
            and consumer_id not in self._deprioritized
        ):
            actual = parse_int(active_reports.get(consumer_id, {}).get("power", 0))
            self._saturation.update(state, last_target, actual)

        # --- Manual override ---
        if consumer_mode.mode == "manual" and consumer_id and state:
            reported = parse_int(active_reports.get(consumer_id, {}).get("power", 0))
            reading = to_grid_reading(NetOutputW(consumer_mode.manual_value), reported)
            state.last_target = reading
            state.last_intent = consumer_mode.manual_value
            return self._split_by_phase(reading, active_reports)

        # Auto-pool reports (exclude manual consumers)
        reports = {cid: r for cid, r in active_reports.items() if cid not in manual}

        result = self._compute_auto_target(consumer_id, reports, grid_total, sample_id)
        return self._apply_min_dc_output(consumer_id, reports, result)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def remove_consumer(self, consumer_id: str) -> None:
        """Full cleanup for a departing consumer."""
        self._consumers.pop(consumer_id, None)
        self._deprioritized.discard(consumer_id)
        if consumer_id in self._priority:
            self._priority.remove(consumer_id)
            self._invalidate_efficiency_cache()
        if consumer_id in self._probe_participants():
            self._clear_probe_state(f"consumer removed: {consumer_id[:16]}")

    def detach_from_auto_pool(self, consumer_id: str) -> None:
        """Remove from efficiency rotation (consumer switched to manual)."""
        self._deprioritized.discard(consumer_id)
        self._priority = [cid for cid in self._priority if cid != consumer_id]
        self._consumers.pop(consumer_id, None)
        self._invalidate_efficiency_cache()
        if consumer_id in self._probe_participants():
            self._clear_probe_state(f"consumer detached: {consumer_id[:16]}")

    def reset_consumer(self, consumer_id: str) -> None:
        """Clear stale state and set a grace period.

        Called when a consumer transitions back to auto mode or resumes
        from inactive.
        """
        state = self._get_consumer(consumer_id)
        state.last_target = None
        state.last_intent = None
        state.pace_cap = 0.0
        state.pace_sign = 0
        state.pace_prev_reported = None
        state.pace_last_at = 0.0
        state.osc_score = 0.0
        state.osc_last_sign = 0
        state.saturation_score = 0.0
        grace = self._clock() + min(
            self._saturation_grace_seconds, self._cfg.efficiency_rotation_interval
        )
        self._saturation.set_grace(state, grace)

    # ------------------------------------------------------------------
    # Rotation
    # ------------------------------------------------------------------

    def force_rotation(self, current_pool: set[str]) -> None:
        """Manually rotate priority order."""
        self._priority = [cid for cid in self._priority if cid in current_pool]
        for cid in sorted(current_pool):
            if cid not in self._priority:
                self._priority.append(cid)
        self._deprioritized.intersection_update(current_pool)

        if len(self._priority) < 2:
            return
        self._priority.append(self._priority.pop(0))
        self._last_rotation = self._clock()
        self._probe_state = None
        self._invalidate_efficiency_cache()
        for cid in list(self._consumers):
            if cid in current_pool:
                self._consumers[cid].fade_weight = 1.0
            else:
                self._consumers.pop(cid, None)
        logger.info(
            "Efficiency: forced rotation, new order: %s",
            [c[:16] for c in self._priority],
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def get_saturation(self, consumer_id: str) -> float:
        state = self._consumers.get(consumer_id)
        return state.saturation_score if state else 0.0

    def get_last_target(self, consumer_id: str) -> float | None:
        state = self._consumers.get(consumer_id)
        return state.last_target if state else None

    def get_last_intent(self, consumer_id: str) -> float | None:
        """Absolute net-output target intended for the consumer, pre-pacing.

        ``None`` until the consumer has received its first instruction.  See
        :attr:`BalancerConsumerState.last_intent`.
        """
        state = self._consumers.get(consumer_id)
        return state.last_intent if state else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _effective_min_dc_output(self, consumer_id: str | None, reports: dict) -> float:
        """Per-consumer MIN_DC_OUTPUT floor (W); 0 means no floor.

        An explicit per-device override (``min_dc_output`` in the report) wins
        for any battery; otherwise the global floor applies only to batteries
        that depend on a sleep-prone external inverter (``_needs_dc_output_floor``).
        """
        report = reports.get(consumer_id, {}) if consumer_id else {}
        override = report.get("min_dc_output")
        if override is not None:
            return max(0.0, float(override))
        if _needs_dc_output_floor(report.get("device_type", "")):
            return self._cfg.min_dc_output
        return 0.0

    def _apply_min_dc_output(
        self, consumer_id: str | None, reports: dict, result: list[float]
    ) -> list[float]:
        """Hold an external-inverter DC battery at ``MIN_DC_OUTPUT`` discharge.

        Wraps the auto-path result so a battery that would otherwise be
        commanded below the floor (e.g. steered to 0 under surplus) keeps a
        minimum net discharge — enough to stop its DC-fed inverter sleeping.
        Only the auto path reaches here; manual/inactive return earlier.

        NOTE: combining this with ``MIN_EFFICIENT_POWER`` rotation is in tension
        — a unit parked at 0 for efficiency is instead held at the floor.  With
        ``MIN_DC_OUTPUT >= saturation min_target`` they coexist stably (a parked,
        empty unit still trips saturation and stays deprioritized while awake).
        A value below the saturation ``min_target`` would mask saturation for a
        floored unit (its target never clears the gate) — main.py warns on that.
        """
        if not consumer_id or consumer_id not in reports:
            return result
        eff_min = self._effective_min_dc_output(consumer_id, reports)
        if eff_min <= 0:
            return result
        report = reports[consumer_id]
        # Respect an explicit park: distribution_weight=0 means "take no share",
        # i.e. sit at 0 — don't silently wake it (mirrors manual/inactive).
        if _report_weight(report) == 0:
            return result
        reported = parse_int(report.get("power", 0))
        # Use the consumer's FULL intended reading: ``_split_by_phase`` spreads
        # the scalar across phases but preserves the total, so sum(result)
        # recovers it regardless of phase distribution. ``result[idx]`` alone is
        # only a phase-apportioned fragment.
        net_self = reported + sum(result)
        # Floor whenever the commanded net output is below the floor — including
        # negative (charge) commands.  A floor-eligible battery has no AC input
        # and cannot charge, so an all-DC-under-surplus fair-share that commands
        # a (futile) charge must still be lifted to the minimum discharge,
        # otherwise the lone-B2500 case (issue #425) would never engage.  An
        # explicit per-device override on a chargeable battery thus also holds a
        # minimum discharge — the user opted into that by setting it.
        if net_self >= eff_min:
            return result
        reading = to_grid_reading(NetOutputW(eff_min), reported)
        phase = (report.get("phase") or "A").upper()
        out = [0.0, 0.0, 0.0]
        out[{"A": 0, "B": 1, "C": 2}.get(phase, 0)] = reading
        state = self._get_consumer(consumer_id)
        state.last_target = reading
        state.last_intent = eff_min
        return out

    def _steer_to_zero(
        self, consumer_id: str | None, reports: dict, *, paced: bool = False
    ) -> list[float]:
        """Drive a consumer's output to zero (``NetOutputW(0)``).

        With ``paced=True`` the wind-down reading goes through the ramp-pacing
        cap like any other auto-path command.  The auto-pool callers
        (deprioritized fade-out, charge-blind hold) use this: the battery
        firmware applies a *negative* (charge-direction) reading in full in a
        single cycle — its accelerating ramp only paces the discharge
        direction — so an unpaced wind-down dumps a discharging consumer's
        whole output in one poll, leaving the rest of the pool a step
        disturbance the meter only reports a poll later (issue #469's
        load-off import spikes).  Inactive consumers and the
        ``min_efficient_power <= 0`` paths keep the one-shot behaviour: those
        are user-initiated mode changes, not part of a closed-loop handoff.
        """
        reported = parse_int(
            reports.get(consumer_id, {}).get("power", 0) if consumer_id else 0
        )
        reading = to_grid_reading(NetOutputW(0), reported)
        if paced and consumer_id:
            reading = self._pace_reading(consumer_id, reading, reported)
        if consumer_id:
            state = self._get_consumer(consumer_id)
            state.last_target = reading if paced else 0
            state.last_intent = 0
        if reading == 0:
            return [0, 0, 0]
        phase = (
            reports.get(consumer_id, {}).get("phase") or "A" if consumer_id else "A"
        ).upper()
        result = [0.0, 0.0, 0.0]
        result[{"A": 0, "B": 1, "C": 2}.get(phase, 0)] = reading
        return result

    @staticmethod
    def _split_by_phase(
        target: float,
        reports: dict,
        weights: dict[str, float] | None = None,
    ) -> list[float]:
        """Distribute *target* across phases proportional to weights."""
        phase_effective: dict[str, float] = {"A": 0.0, "B": 0.0, "C": 0.0}
        for cid, report in reports.items():
            phase = (report.get("phase") or "A").upper()
            if phase not in phase_effective:
                phase = "A"
            w = (weights or {}).get(cid, 1.0)
            phase_effective[phase] += w

        total = sum(phase_effective.values())
        if total <= 0:
            return [target, 0, 0]
        return [
            target * (phase_effective["A"] / total),
            target * (phase_effective["B"] / total),
            target * (phase_effective["C"] / total),
        ]

    # ------------------------------------------------------------------
    # Auto-target pipeline
    # ------------------------------------------------------------------

    def _compute_auto_target(
        self,
        consumer_id: str | None,
        reports: dict,
        grid_total: float,
        sample_id: tuple = (),
    ) -> list[float]:
        """Automatic allocation for auto-pool consumers."""
        # Predicted grid the residual/fair-share control acts on (compensates
        # for meter latency; see ``_predict_control_grid``).  Updated on every
        # call so the running estimate stays continuous across the probe /
        # fading / charge-blind early-return paths, which keep using the raw
        # meter for their categorical (charge-territory, demand-sizing)
        # decisions.  Only the steady residual loop below acts on the
        # prediction.
        control_grid = self._predict_control_grid(reports, grid_total, sample_id)

        saturation = {cid: s.saturation_score for cid, s in self._consumers.items()}
        num_consumers = max(1, len(reports))
        eff_part = {cid: max(0.01, 1.0 - saturation.get(cid, 0.0)) for cid in reports}

        # Exclude batteries that can't charge from AC (``not
        # _is_ac_chargeable(...)`` — the B2500 family and Jupiter; unknown /
        # empty types are treated as AC-chargeable, see ``device_capabilities``)
        # from charge distribution whenever the grid is in charge territory.
        # The base gate is ``grid_total < 0`` (surplus), but we also extend it
        # to the exact zero-crossing when an AC-chargeable battery is already
        # charging (``power < 0``) — that signals pass-through
        # equilibrium, which happens when a full B2500 is passing its DC
        # solar input through as AC output (+P W) while the Venus
        # charges a matching -P W, leaving grid at 0.  Without this
        # extension the balance-correction fires at the zero-crossing
        # and oscillates the Venus back out of its steady state.  We
        # deliberately don't fire on ``grid_total == 0`` during pure
        # discharge (both batteries discharging to serve the house load)
        # because no AC-chargeable battery is charging there.
        # See issue #338.
        #
        # The whole gate is further conditioned on ``any_ac_chargeable``:
        # if no AC-coupled battery is reporting there is nothing to
        # protect from B2500 interference, so we let the normal fair-
        # share path handle brief negative-grid transients (load drops,
        # ramp overshoot) by smoothly reducing discharge rather than
        # slamming the whole pool to 0 W and forcing a re-ramp cycle.
        # See issue #359.
        ac_charging = any(
            _is_ac_chargeable(r.get("device_type", ""))
            and parse_int(r.get("power", 0)) < 0
            for r in reports.values()
        )
        any_ac_chargeable = any(
            _is_ac_chargeable(r.get("device_type", "")) for r in reports.values()
        )
        in_charge_territory = any_ac_chargeable and (
            grid_total < 0 or (grid_total == 0 and ac_charging)
        )
        charge_blind = (
            {
                cid
                for cid, r in reports.items()
                if not _is_ac_chargeable(r.get("device_type", ""))
            }
            if in_charge_territory
            else set()
        )
        for cid in charge_blind:
            eff_part[cid] = 0.0

        efficiency_adjustments = self._compute_efficiency_deprioritized(
            reports, sample_id, grid_total
        )
        faded_adjustments = self._fade_efficiency_weights(
            efficiency_adjustments, set(reports.keys())
        )
        any_fading = any(0.0 < w < 1.0 for w in faded_adjustments.values())

        probe_target = self._compute_probe_target(
            consumer_id, reports, grid_total, eff_part
        )
        if probe_target is not None:
            return probe_target

        # Degenerate case: every reporter is DC-only but we're under
        # surplus.  Nothing can absorb; log once so the user can see why
        # the grid is still feeding back.  In this all-DC mode we leave
        # ``in_charge_territory`` off (see above) so that the regular
        # fair-share path can still smoothly reduce discharge through
        # brief negative-grid transients (e.g. a load drop while the
        # batteries are mid-discharge — see issue #359); the B2500s'
        # own AC-charge clamp keeps them at 0 W under a sustained
        # surplus regardless.
        all_dc_under_surplus = (
            grid_total < 0 and bool(reports) and not any_ac_chargeable
        )
        if all_dc_under_surplus and not self._all_dc_surplus_warned:
            logger.info(
                "CT002: %.0f W surplus but no AC-chargeable battery "
                "reporting — nothing here can absorb it. Reporting "
                "device_types: %s",
                -grid_total,
                sorted({reports[cid].get("device_type", "") or "?" for cid in reports}),
            )
            self._all_dc_surplus_warned = True
        elif not all_dc_under_surplus:
            self._all_dc_surplus_warned = False

        # A DC-only consumer under surplus must be told explicitly to hold
        # at 0 — don't fall through to the fair-share math where a residual
        # correction could leak a nonzero target.
        if consumer_id and consumer_id in charge_blind:
            return self._steer_to_zero(consumer_id, reports, paced=True)

        # --- Fading path ---
        if any_fading and consumer_id:
            state = self._get_consumer(consumer_id)
            fade_w = state.fade_weight
            reported = parse_int(reports.get(consumer_id, {}).get("power", 0))
            if fade_w == 0.0:
                return self._steer_to_zero(consumer_id, reports, paced=True)

            total_battery = sum(
                parse_int(reports.get(cid, {}).get("power", 0)) for cid in reports
            )
            demand = total_battery + grid_total
            total_fade = sum(self._get_consumer(cid).fade_weight for cid in reports)
            desired = demand * fade_w / total_fade if total_fade > 0 else 0.0
            reading = to_grid_reading(NetOutputW(desired), reported)
            reading = self._pace_reading(consumer_id, reading, reported)

            state.last_target = reading
            state.last_intent = desired

            return self._split_by_phase(reading, reports, eff_part)

        # --- Non-fading path ---
        for cid, fade_w in faded_adjustments.items():
            if cid in eff_part and fade_w == 0.0:
                eff_part[cid] = 0.0
        if (
            faded_adjustments
            and consumer_id
            and faded_adjustments.get(consumer_id) == 0.0
        ):
            return self._steer_to_zero(consumer_id, reports, paced=True)

        # Fold the per-battery user weight into the effectiveness map so the
        # fair-share split honours the configured ratio.  ``eff_part`` stays the
        # pure health/saturation map (used for participation and probing); the
        # weighted ``share_part`` only drives the proportional distribution.  At
        # the neutral default (every weight 1.0) ``share_part == eff_part`` and
        # the math is identical to the unweighted behaviour.
        #
        # The ``total_effective > 0`` guard also covers the degenerate case
        # where every participant's share rounds to zero (charge-blind / faded
        # / zero-weight): fall back to an even split rather than dividing by
        # zero. Mirrors the C++ port (balancer.cpp ``compute_auto_target_``).
        share_part = {
            cid: eff_part[cid] * _report_weight(reports.get(cid, {}))
            for cid in eff_part
        }
        total_effective = sum(share_part.values())
        fair_share = (
            (control_grid / total_effective) * share_part.get(consumer_id, 1.0)
            if consumer_id and consumer_id in reports and total_effective > 0
            else control_grid / num_consumers
        )

        cfg = self._cfg
        # ``fair_share`` / ``_balance_correction`` produce the residual: this
        # consumer's slice of the grid imbalance to fold into its current
        # output.  The absolute net-output target is therefore "what I report
        # now plus my residual share" — see the NetOutputW wrap below.
        if (
            not cfg.fair_distribution
            or consumer_id is None
            or consumer_id not in reports
        ):
            residual = fair_share
        elif consumer_id in eff_part:
            residual = self._balance_correction(
                consumer_id, reports, eff_part, fair_share
            )
        else:
            residual = fair_share

        # Clamp sign disagreement: prevent the inverter from acting
        # against the current (predicted) grid direction.
        if (control_grid < 0 and residual > 0) or (control_grid > 0 and residual < 0):
            residual = 0

        if consumer_id:
            residual = self._damp_oscillation(consumer_id, residual)

        reported = (
            parse_int(reports.get(consumer_id, {}).get("power", 0))
            if consumer_id
            else 0
        )
        reading = to_grid_reading(NetOutputW(reported + residual), reported)

        if consumer_id:
            reading = self._pace_reading(consumer_id, reading, reported)
            state = self._get_consumer(consumer_id)
            state.last_target = reading
            state.last_intent = reported + residual

        return self._split_by_phase(reading, reports, eff_part)

    def _predict_control_grid(
        self, reports: dict, grid_total: float, sample_id: tuple
    ) -> float:
        """Return the grid power the control path should act on.

        An online grid-state observer that compensates for meter latency
        *without* per-meter tuning — the whole point, since the same setting
        then works whether the meter refreshes once a second or pushes a
        reading delayed by several seconds, and the user no longer has to hunt
        for the right throttle/pacing for their powermeter.

        Two signals observe the same physical grid at different delays: the
        batteries' self-reported output (fresh — as fresh as each battery's
        poll) and the grid meter (latent — its refresh interval plus
        transport/measurement delay).  The observer fuses them:

        * **Output crediting** — every call, advance the estimate by the pool's
          actual reported output change ``Δu`` (grid moves opposite to net
          output: more discharge ⇒ less import).  A correction is credited the
          moment the battery delivers it, long before the meter shows it, so the
          loop never re-issues (and winds up) a correction that is already in
          flight — the double-count that makes a latent loop overshoot and
          limit-cycle.  Between two meter refreshes the estimate falls as the
          battery ramps, so each poll commands only the *remaining* error.
        * **Meter correction** — on a genuinely fresh reading, pull the estimate
          toward the meter by the *adaptive* trust.  This is the only channel by
          which disturbances the pool did not cause (load steps, clouds, PV
          ramps) enter the estimate.

        The trust is the adaptive element, and it resolves the one real tension:
        a high trust tracks disturbance steps fast but lets meter latency drive
        overshoot/hunting back in; a low trust is rock-steady but lags real
        steps.  The right value is not a property of the install to be
        configured — it depends on what the grid is doing *right now*, so the
        loop learns it from the **innovation** ``meter - estimate`` on each
        fresh sample:

        * Consecutive innovations of the *same* sign mean the estimate is
          lagging a genuine, sustained disturbance — raise the trust to catch up
          (a single big step ramps the trust up over a few samples, then the
          innovation collapses and the trust stops rising).
        * Innovations that *flip* sign mean the loop is overshooting / hunting
          on stale feedback — shrink the trust hard so the fast output-credited
          prediction dominates and the hunt is starved of the gain that sustains
          it.

        Only innovations above a small magnitude gate adapt the trust, so
        steady-state meter noise (whose sign flips at random) neither collapses
        nor inflates it.

        Returns the raw meter total unchanged when disabled
        (``grid_predict_trust <= 0``).  ``grid_predict_trust`` seeds the initial
        trust; the loop adapts away from it within
        ``[PRED_TRUST_MIN, PRED_TRUST_MAX]``, so its exact value barely matters
        — that insensitivity is the point.  The prediction uses the auto-pool
        reports, so manual/inactive batteries and the house load enter only via
        the innovation, like any other disturbance the pool did not command.
        """
        if self._cfg.grid_predict_trust <= 0.0:
            return grid_total
        pool_output = sum(parse_int(r.get("power", 0)) for r in reports.values())
        if self._pred_grid is None:
            self._pred_grid = grid_total
            self._pred_pool_output = pool_output
            self._pred_sample_id = sample_id
            self._pred_trust = min(
                PRED_TRUST_MAX, max(PRED_TRUST_MIN, self._cfg.grid_predict_trust)
            )
            return grid_total
        self._pred_grid -= pool_output - self._pred_pool_output
        self._pred_pool_output = pool_output
        if sample_id != self._pred_sample_id:
            self._pred_sample_id = sample_id
            innovation = grid_total - self._pred_grid
            sign = 1 if innovation > 0 else (-1 if innovation < 0 else 0)
            if abs(innovation) >= PRED_INNOVATION_GATE_W and sign != 0:
                if self._pred_innov_sign == 0 or sign == self._pred_innov_sign:
                    self._pred_trust = min(
                        PRED_TRUST_MAX, self._pred_trust + PRED_TRUST_RAISE_STEP
                    )
                else:
                    self._pred_trust = max(
                        PRED_TRUST_MIN, self._pred_trust * PRED_TRUST_SHRINK
                    )
                self._pred_innov_sign = sign
            self._pred_grid += self._pred_trust * innovation
        return self._pred_grid

    def _damp_oscillation(self, consumer_id: str, residual: float) -> float:
        """Scale ``residual`` down while the consumer is hunting (issue #473).

        Tracks an EMA (``osc_score``) of how often the residual reverses sign.
        A genuine load step holds one sign, so the score decays to 0 and the
        residual passes through unchanged; a latency-driven limit cycle flips
        sign nearly every poll, driving the score toward 1 and shrinking the
        residual by up to ``osc_damp_max`` — bleeding the loop gain that
        sustains the hunt without slowing same-direction reactions.  Mirrors
        the C++ port (balancer.cpp ``damp_oscillation_``).
        """
        cfg = self._cfg
        if cfg.osc_damp_max <= 0.0:
            return residual
        state = self._get_consumer(consumer_id)
        sign = 1 if residual > 0 else (-1 if residual < 0 else 0)
        # A residual past the threshold is a genuine demand step, not hunting:
        # react at full gain (and bleed any hunt memory) so a real load/solar
        # change isn't slowed just because the loop was hunting beforehand.
        if cfg.osc_damp_threshold > 0.0 and abs(residual) > cfg.osc_damp_threshold:
            state.osc_score *= 1.0 - cfg.osc_damp_decay
            if sign != 0:
                state.osc_last_sign = sign
            return residual
        # Accumulate the score by ``osc_damp_alpha`` on each sign reversal and
        # bleed it by ``osc_damp_decay`` otherwise.  A few reversals (a solar
        # ramp crossing zero, or the brief ring-down after a load step) only
        # nudge it, so genuine reactions keep near-full gain; only *repeated*
        # reversals — a hunting limit cycle —
        # accumulate the score toward 1 and engage the damping.  The reversal
        # rate, not a one-off flip, is what distinguishes a hunt from a step.
        if sign != 0 and state.osc_last_sign != 0 and sign != state.osc_last_sign:
            state.osc_score = min(1.0, state.osc_score + cfg.osc_damp_alpha)
        else:
            state.osc_score *= 1.0 - cfg.osc_damp_decay
        if sign != 0:
            state.osc_last_sign = sign
        return residual * (1.0 - cfg.osc_damp_max * state.osc_score)

    def _pace_reading(self, consumer_id: str, reading: float, reported: float) -> float:
        """Clamp the auto-path *reading* to the consumer's ramp-pacing cap.

        The battery integrates the reading with its own accelerating ramp,
        stepping by at most ``min(GAIN[ramp], |reading|)`` per poll — so the
        reading we send is the only bound on its per-poll movement once the
        ramp has accelerated.  Pacing keeps that bound tight: the cap starts
        at the firmware's first-step gain (``pace_base_step``), doubles per
        reference second toward ``pace_max_step`` only while the battery
        demonstrably tracks the command, follows the error back down as it
        shrinks (so the final approach is gentle), and resets to the base
        step when the command direction reverses.  This bounds
        stale-feedback overshoot to roughly the battery's *demonstrated*
        slew instead of its maximum ramp gain.  The caps are defined in W
        per :data:`PACE_REFERENCE_DT`; the per-poll clamp scales with the
        consumer's observed inter-poll time (clamped at 1.0) so a fast
        poller cannot integrate the same per-poll reading into a higher W/s
        slew.

        Only the auto-pool paths are paced — the persistent regulation
        loop, the fade transition, and the deprioritized/charge-blind
        wind-down to zero (the firmware applies a charge-direction reading
        in full in one cycle, so an unpaced wind-down would dump a
        discharging consumer's whole output as a one-poll step disturbance
        on the rest of the pool).  Probe targets (own ramp schedule, must
        clear inverter floors in one step), the MIN_DC_OUTPUT floor (must
        jump to the floor), manual targets and inactive-consumer
        steer-to-zero (a user-initiated one-shot, not a closed-loop
        handoff) deliberately bypass this.

        Pacing deliberately applies to direction reversals too (capping the
        wind-down/reversal rate bounds the overshoot at zero crossings, a
        major oscillation source on load drops).  Consumers needing the
        *unpaced* control intent — the cross-talk chrg/dchrg attribution
        that filters involuntary PV passthrough (issue #376) — read it from
        ``last_intent`` / :meth:`get_last_intent`, which every control path
        records before pacing.
        """
        base = self._cfg.pace_base_step
        if base <= 0:
            return reading
        state = self._get_consumer(consumer_id)
        now = self._clock()
        # Cadence scale: the caps are W per PACE_REFERENCE_DT; a faster
        # poller gets a proportionally smaller per-poll clamp so its W/s
        # slew matches a reference-cadence battery.  Clamped at 1.0 so a
        # slow poller keeps the per-poll cap (see PACE_REFERENCE_DT).  The
        # first paced poll, and anything past a long gap, uses the full
        # reference scale.
        dt = now - state.pace_last_at if state.pace_last_at > 0.0 else 0.0
        if dt <= 0.0:
            # First paced poll, a non-advancing clock, or a backwards jump:
            # assume one reference period rather than starving the clamp.
            dt = PACE_REFERENCE_DT
        state.pace_last_at = now
        dt_ratio = min(1.0, dt / PACE_REFERENCE_DT)
        sign = 1 if reading > 0 else -1 if reading < 0 else 0
        cap = state.pace_cap if state.pace_cap > 0 else base
        # The clamp never drops below the base step: devices with
        # hysteresis-style regulators (B2500) need a minimum reading to
        # clear their input hold window at all, and the base step is the
        # rate the cap growth schedule was tuned to bootstrap from.  The
        # cadence scale still bounds the *grown* cap, which is where the
        # fast-poller overshoot lived.
        limit = max(base, cap * dt_ratio)
        if sign == 0 or sign != state.pace_sign:
            cap = base
        elif abs(reading) > limit:
            moved = (
                (reported - state.pace_prev_reported) * sign
                if state.pace_prev_reported is not None
                else 0.0
            )
            # The tracking threshold and growth rate scale with the same
            # cadence ratio: a fast poller is expected to have moved less
            # between polls, and its cap doubles per reference second, not
            # per poll.
            if moved >= PACE_TRACKING_DELTA_W * dt_ratio:
                cap = min(cap * PACE_GROWTH_FACTOR**dt_ratio, self._cfg.pace_max_step)
        else:
            cap = max(base, abs(reading) / dt_ratio)
        # Enforce the pace_max_step contract: the grow branch already clamps,
        # but the else branch back-computes cap as abs(reading) / dt_ratio,
        # which a fast poll (small dt_ratio) can inflate past the max — and a
        # later normal-cadence poll would then slew beyond pace_max_step.
        cap = min(cap, self._cfg.pace_max_step)
        state.pace_cap = cap
        state.pace_sign = sign
        state.pace_prev_reported = reported
        limit = max(base, cap * dt_ratio)
        return max(-limit, min(limit, reading))

    def _balance_correction(
        self,
        consumer_id: str,
        reports: dict,
        eff_part: dict[str, float],
        fair_share: float,
    ) -> float:
        """Apply fair-share balance correction for *consumer_id*."""
        cfg = self._cfg
        actual_self = parse_int(reports.get(consumer_id, {}).get("power", 0))
        participating = [cid for cid in reports if eff_part.get(cid, 1.0) > 0.1]
        if not participating:
            return fair_share

        actual_total = sum(
            parse_int(reports.get(cid, {}).get("power", 0)) for cid in participating
        )
        # Pull each battery toward its weight-proportional share of the pool's
        # total output rather than the plain average, so the configured ratio is
        # the steady state.  Participation is still decided by ``eff_part`` (the
        # health map) above, so a healthy battery with a small weight is not
        # dropped from the pool.  With neutral weights this reduces to the plain
        # average (``actual_total / len(participating)``).
        weights = {cid: _report_weight(reports.get(cid, {})) for cid in participating}
        total_weight = sum(weights.values())
        if total_weight > 0:
            target_share = actual_total * weights.get(consumer_id, 0.0) / total_weight
        else:
            target_share = actual_total / len(participating)
        error = target_share - actual_self
        err_abs = abs(error)
        if cfg.balance_deadband > 0 and err_abs < cfg.balance_deadband:
            return fair_share

        gain = cfg.balance_gain
        if cfg.error_reduce_threshold > 0 and err_abs < cfg.error_reduce_threshold:
            gain = gain * (err_abs / cfg.error_reduce_threshold)
        elif cfg.error_boost_threshold > 0 and cfg.error_boost_max > 0:
            boost = min(err_abs / cfg.error_boost_threshold, 1.0) * cfg.error_boost_max
            gain = gain * (1.0 + boost)
        correction = gain * error
        if cfg.max_correction_per_step > 0:
            cap = cfg.max_correction_per_step
            correction = max(-cap, min(cap, correction))
        target = fair_share + correction
        if cfg.max_target_step > 0:
            lo = actual_self - cfg.max_target_step
            hi = actual_self + cfg.max_target_step
            target = max(lo, min(hi, target))
        return target

    # ------------------------------------------------------------------
    # Efficiency deprioritization
    # ------------------------------------------------------------------

    def _compute_efficiency_deprioritized(
        self, reports: dict, sample_id: tuple, grid_total: float
    ) -> dict[str, float]:
        """Decide which consumers to deprioritize for efficiency."""
        cfg = self._cfg
        if cfg.min_efficient_power <= 0 or len(reports) < 2:
            self._probe_state = None
            self._deprioritized = set()
            self._invalidate_efficiency_cache()
            return {}

        now = self._clock()
        current = set(reports)
        self._priority = [c for c in self._priority if c in current]
        self._deprioritized.intersection_update(current)
        grace = now + min(
            self._saturation_grace_seconds, cfg.efficiency_rotation_interval
        )
        for cid in sorted(current):
            if cid not in self._priority:
                self._priority.append(cid)
                self._set_consumer_grace(cid, grace)

        prev_slots = max(
            0, min(len(self._priority), len(self._priority) - len(self._deprioritized))
        )
        previous_active = tuple(self._priority[:prev_slots])
        probe_resolved = self._resolve_probe_state(reports, now, grid_total)
        probe_active = self._probe_state is not None

        # Rotation check BEFORE cache
        if (
            not probe_active
            and not probe_resolved
            and self._priority
            and now - self._last_rotation >= cfg.efficiency_rotation_interval
        ):
            self._last_rotation = now
            self._priority.append(self._priority.pop(0))
            self._invalidate_efficiency_cache()

        # Saturation swap check BEFORE cache
        if (
            not probe_active
            and not probe_resolved
            and cfg.efficiency_saturation_threshold > 0
            and self._cache_sample is not None
        ):
            slots_est = len(self._priority) - len(self._deprioritized)
            for cid in self._priority[:slots_est]:
                state = self._consumers.get(cid)
                if (
                    state
                    and state.saturation_score >= cfg.efficiency_saturation_threshold
                ):
                    self._invalidate_efficiency_cache()
                    break

        cache_key = (sample_id, tuple(self._priority))
        if cache_key == self._cache_sample:
            return self._cache_result or {}

        # Estimate demand
        total_battery_power = sum(
            parse_int(reports.get(cid, {}).get("power", 0)) for cid in self._priority
        )
        abs_target = abs(total_battery_power + grid_total)
        n = len(self._priority)
        per_consumer = abs_target / n

        # Hysteresis
        was_limiting = len(self._deprioritized) > 0
        if was_limiting:
            enter_limiting = per_consumer < (
                cfg.min_efficient_power * EFFICIENCY_HYSTERESIS_FACTOR
            )
        else:
            enter_limiting = per_consumer < cfg.min_efficient_power

        if enter_limiting and n > 1:
            slots = max(1, min(n - 1, int(abs_target / cfg.min_efficient_power)))
            if was_limiting and 1 <= prev_slots < slots:
                # Growing the active set while limiting takes the same 20%
                # margin as exiting limiting entirely.  Without it, demand
                # sitting at an exact multiple of min_efficient_power (e.g.
                # ~300 W base load with a 150 W floor) toggles a unit
                # active/deprioritized on every meter-noise tick, keeping the
                # fade EMA permanently mid-transition and the pool hunting
                # (issue #469).  Shrinking stays immediate, mirroring how
                # entering limiting is immediate.
                grown = int(
                    abs_target
                    / (cfg.min_efficient_power * EFFICIENCY_HYSTERESIS_FACTOR)
                )
                slots = max(prev_slots, min(n - 1, grown))
        else:
            slots = n

        deprioritized = set(self._priority[slots:])
        result: dict[str, float] = {cid: 0.0 for cid in deprioritized}
        pre_swap_active = set(self._priority[:slots])

        # Reset saturation for consumers transitioning to active
        for cid in self._deprioritized - deprioritized:
            state = self._get_consumer(cid)
            self._saturation.clear(state)
            self._set_consumer_grace(cid, grace)

        if (
            not probe_active
            and not probe_resolved
            and self._maybe_force_swap_saturated(self._priority, slots, now)
        ):
            deprioritized = set(self._priority[slots:])
            result = {cid: 0.0 for cid in deprioritized}
            cache_key = (sample_id, tuple(self._priority))
            for cid in set(self._priority[:slots]) - pre_swap_active:
                state = self._get_consumer(cid)
                self._saturation.clear(state)
                self._set_consumer_grace(cid, grace)

        final_active = tuple(self._priority[:slots])
        if not probe_active and not probe_resolved and previous_active:
            promoted = [cid for cid in final_active if cid not in previous_active]
            backups = [cid for cid in previous_active if cid not in final_active]
            if promoted and backups:
                self._begin_probe(
                    promoted[0],
                    final_active,
                    tuple(backups),
                    previous_active,
                    now,
                )

        for cid in deprioritized - self._deprioritized:
            state = self._consumers.get(cid)
            if state:
                # Clearing saturation here is symmetric with the
                # `deprioritized -> active` branch above (line 1018):
                # the score is a memory of the *previous* role, and once
                # the consumer is moved into the deprioritized set it
                # will be steered toward zero, so any residual score is
                # no longer an accurate estimate of whether it could
                # follow an active-slot target.  Without this clear,
                # a consumer that accumulated saturation during an
                # active-to-deprioritized transition (common when the
                # time-weighted EMA integrates over the fading window)
                # cannot be promoted back via `_maybe_force_swap_saturated`
                # because that path requires a healthy deprioritized
                # candidate.
                self._saturation.clear(state)
            logger.info(
                "Efficiency: deprioritizing consumer %s (demand %.0fW, %d active)",
                cid[:16],
                abs_target,
                slots,
            )
        for cid in self._deprioritized - deprioritized:
            logger.info(
                "Efficiency: activating consumer %s (demand %.0fW, %d active)",
                cid[:16],
                abs_target,
                slots,
            )

        self._deprioritized = deprioritized
        self._cache_sample = cache_key
        self._cache_result = result
        return result

    def _maybe_force_swap_saturated(
        self, priority: list[str], slots: int, now: float
    ) -> bool:
        """Swap a saturated active battery with a healthy deprioritized one.

        A healthy candidate is one whose saturation score is *strictly
        below* ``efficiency_saturation_threshold``.  Note that this works
        in concert with the symmetric-clear logic in
        :meth:`_compute_efficiency_deprioritized`: when a consumer
        transitions from active → deprioritized the saturation score is
        cleared to zero (the score is a memory of the previous role and
        no longer reflects the can-it-follow question relevant to the
        new role).  That clear guarantees a healthy candidate is
        available the first time the balancer decides to swap a
        newly-saturated active unit post-probe, which previously
        dead-locked because both consumers were still above the threshold
        during the fade window.
        """
        cfg = self._cfg
        if cfg.efficiency_saturation_threshold <= 0 or slots >= len(priority):
            return False
        threshold = cfg.efficiency_saturation_threshold
        saturated_idx = None
        for i in range(slots):
            state = self._consumers.get(priority[i])
            if state and state.saturation_score >= threshold:
                saturated_idx = i
                break
        if saturated_idx is None:
            return False
        healthy_idx = None
        for i in range(slots, len(priority)):
            state = self._consumers.get(priority[i])
            if not state or state.saturation_score < threshold:
                healthy_idx = i
                break
        if healthy_idx is None:
            return False
        sat_state = self._consumers.get(priority[saturated_idx])
        logger.info(
            "Efficiency: %s cannot follow target (sat=%.2f), rotating to %s",
            priority[saturated_idx][:16],
            sat_state.saturation_score if sat_state else 0.0,
            priority[healthy_idx][:16],
        )
        priority[saturated_idx], priority[healthy_idx] = (
            priority[healthy_idx],
            priority[saturated_idx],
        )
        self._last_rotation = now
        return True

    def _fade_efficiency_weights(
        self, raw_adjustments: dict[str, float], consumer_ids: set[str]
    ) -> dict[str, float]:
        """Apply EMA fade to efficiency weights for smooth transitions."""
        alpha = self._cfg.efficiency_fade_alpha
        result: dict[str, float] = {}
        frozen = self._probe_participants()
        now = self._clock()
        post_probe_active = now < self._post_probe_fade_until
        for cid in consumer_ids:
            state = self._get_consumer(cid)
            if cid in frozen:
                state.fade_weight = 1.0
                continue
            goal = raw_adjustments.get(cid, 1.0)
            prev = state.fade_weight
            effective_alpha = alpha
            if post_probe_active and cid in self._post_probe_fade_ids:
                effective_alpha = min(alpha, 0.25)
            new = prev + effective_alpha * (goal - prev)
            if abs(new - goal) < 0.05:
                new = goal
            state.fade_weight = new
            if new < 1.0:
                result[cid] = new
        if not post_probe_active:
            self._clear_post_probe_fade()
        # Clean up consumers no longer in the pool
        for cid in list(self._consumers):
            if cid not in consumer_ids and cid not in self._priority:
                self._consumers.pop(cid, None)
        return result
