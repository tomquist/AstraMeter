"""Load balancing with efficiency optimization and saturation detection."""

from __future__ import annotations

import dataclasses
import logging
import time
from collections.abc import Callable, Mapping
from typing import Any, Literal, NamedTuple, NewType, get_args

from astrameter.config.logger import logger

from .protocol import parse_int

# ---------------------------------------------------------------------------
# Net-output target: the single currency of all control logic
# ---------------------------------------------------------------------------

# An absolute net-output target in watts — the one value every control policy
# (steer-to-zero, manual, fair-share, probing) declares.  Sign convention:
#     +  =  net discharge  (export to grid / serve house load)
#     -  =  net charge      (import from grid)
# A distinct type so a target can never be silently mixed with a grid-meter
# *reading*, the relative delta a battery adds to its own output; the
# conversion happens in exactly one place, :func:`to_grid_reading`.
NetOutputW = NewType("NetOutputW", float)


def to_grid_reading(target: NetOutputW, reported: float) -> float:
    """Grid-meter reading that lands the battery on *target*: ``target - reported``.

    Positive = grid import (raise net output), negative = grid export; the
    battery integrates ``new_output = reported + reading``.  Callers phase-split
    the scalar (:meth:`LoadBalancer._split_by_phase`).
    """
    return float(target) - reported


def phase_index(phase: str) -> int:
    """Index of *phase* in a ``[phase_A, phase_B, phase_C]`` vector.

    Anything that isn't A/B/C — including the combined-mode "D" — falls back to
    phase A, which is where a single-phase command goes when the reported phase
    is unknown.
    """
    return {"A": 0, "B": 1, "C": 2}.get(phase.upper(), 0)


@dataclasses.dataclass(frozen=True)
class ConsumerReport:
    """One consumer's latest poll, as the balancer sees it.

    Every field is normalized on construction, so the control path reads
    plain numbers and never re-parses or re-defaults: ``None`` means
    "neutral" for the two weights and "no override" for ``min_dc_output``.
    """

    power: int = 0
    """Reported net output in watts; positive discharges, negative charges."""

    phase: str = "A"
    """Steered phase (A/B/C, or D for combined mode); A when unknown."""

    device_type: str = ""

    weight: float = 1.0
    """Fair-share weight. ``0.0`` parks the battery; the setter bounds it to [0, 10]."""

    efficiency_window_weight: float = 1.0
    """Fraction of ``efficiency_rotation_interval`` an active slot is held for,
    clamped to [0, 1]. ``0.0`` rotates out on the next tick."""

    min_dc_output: float | None = None
    """Per-device MIN_DC_OUTPUT override in watts; ``None`` uses the global rule."""

    def __post_init__(self) -> None:
        eww = self.efficiency_window_weight
        floor = self.min_dc_output
        for field, value in (
            ("power", parse_int(self.power)),
            ("phase", (self.phase or "A").upper()),
            ("device_type", self.device_type or ""),
            ("weight", 1.0 if self.weight is None else float(self.weight)),
            (
                "efficiency_window_weight",
                1.0 if eww is None else max(0.0, min(1.0, float(eww))),
            ),
            ("min_dc_output", None if floor is None else max(0.0, float(floor))),
        ):
            object.__setattr__(self, field, value)


# Reports keyed by consumer id — what the balancer is handed every poll.
Reports = Mapping[str, ConsumerReport]

NO_REPORT = ConsumerReport()
"""Stand-in for a consumer that did not report this tick (unknown or just removed)."""


def _report_of(reports: Reports, consumer_id: str | None) -> ConsumerReport:
    """*consumer_id*'s report, or :data:`NO_REPORT` when it did not report.

    A consumer can drop out between the snapshot a caller took and the
    allocation that reads it (evicted, deprioritized, filtered out of the auto
    pool), and the neutral report keeps every reader on plain attribute access
    instead of inventing its own default.
    """
    if not consumer_id:
        return NO_REPORT
    return reports.get(consumer_id, NO_REPORT)


# Ramp pacing (issue #458).  The pace cap grows only once the battery's reported
# output has moved at least PACE_TRACKING_DELTA_W in the commanded direction
# since the last paced poll.  The threshold sits below the firmware's worst-case
# 10 W step on a constant reading (issue #469) so a normal step response is not
# read as a stall.
PACE_TRACKING_DELTA_W = 5.0
PACE_GROWTH_FACTOR = 2.0
# Consecutive clamped polls with no movement after which the cap grows anyway.
# Needed for any actuator whose minimum actionable command exceeds
# pace_base_step (a B2500's DC channels cannot energize below ~40 W each): the
# clamp holds the command below what the device can execute, so it never
# moves, and never moving is what withholds the bigger command.
PACE_STALL_ESCAPE_POLLS = 3
# Reference poll interval the pace caps are defined against: pace_base_step /
# pace_max_step are watts per reference second, scaled by the consumer's
# observed inter-poll time (clamped at 1.0, so a slow poller keeps the per-poll
# cap).
PACE_REFERENCE_DT = 1.0

# Adaptive grid-state predictor (see BalancerConfig.grid_predict_trust and
# LoadBalancer._predict_control_grid).  Meter trust is bounded to
# [PRED_TRUST_MIN, PRED_TRUST_MAX] and adapted on each fresh sample whose
# innovation clears PRED_INNOVATION_GATE_W: raised additively under a same-sign
# innovation run (a real step), shrunk multiplicatively on a sign flip
# (latency-driven hunting).
PRED_TRUST_MIN = 0.15
PRED_TRUST_MAX = 0.9
PRED_TRUST_RAISE_STEP = 0.2
PRED_TRUST_SHRINK = 0.5
PRED_INNOVATION_GATE_W = 40.0

# Steady-import trim (see BalancerConfig.import_trim_w and
# LoadBalancer._apply_import_trim): engages once the predicted grid has held
# inside (0, IMPORT_TRIM_GATE_W) for IMPORT_TRIM_DWELL consecutive fresh
# samples.  The gate sits above the firmware deadband / hold window but below
# the import a saturated or empty pack leaves.
IMPORT_TRIM_GATE_W = 120.0
IMPORT_TRIM_DWELL = 6

# Control-quality assessment (see ControlQualityTracker).  The accuracy band is
# the balancer's own settling deadband and the EMAs are time-weighted against
# CONTROL_QUALITY_REFERENCE_DT, so pollers at different cadences reach the same
# verdict under the same physical conditions.
CONTROL_QUALITY_REFERENCE_DT = 1.0
# ~50 reference seconds of memory: a kettle switching on is averaged away, a
# loop that starts hunting is called out within a minute.
CONTROL_QUALITY_ALPHA = 0.02
# A gap this long means the old window describes a different house; re-seed
# rather than dosing the EMAs with minutes of rise or decay.  A loop polling
# slower than this (``DEDUPE_TIME_WINDOW`` above 60 s) never leaves "warmup" —
# deliberate, the same trade-off ``SATURATION_LONG_GAP_SECONDS`` makes at 30 s.
CONTROL_QUALITY_LONG_GAP_SECONDS = 60.0
# Observation before the tracker commits to a verdict.  A duration, not a
# sample count: a CT is polled once per battery, so a sample count would let a
# large pool publish a verdict off a fraction of a second.
CONTROL_QUALITY_WARMUP_SECONDS = 10.0
# Floor under the accuracy band, so the verdict never demands tracking tighter
# than the meter noise the loop deliberately ignores.
CONTROL_QUALITY_MIN_BAND_W = 25.0
# Mean error, in multiples of the band, up to which the loop counts as stable.
# Well above the band, since every load step lands on the meter before any
# battery can answer it; 100 W at the default band.
CONTROL_QUALITY_STABLE_BANDS = 4.0
# Out-of-band error at which the score bottoms out, in multiples of the band
# (~525 W at the default), so the score stays a gradient across real houses.
CONTROL_QUALITY_ERROR_SCALE = 20.0
# Share of the window the pool must have spent with no headroom before a
# persistent error is blamed on the pack rather than the loop.  A majority, so
# one saturated sample cannot excuse a whole window of accumulated error.
CONTROL_QUALITY_LIMITED_SHARE = 0.5
# Saturation score above which a battery counts as out of headroom.
CONTROL_QUALITY_SATURATED = 0.6

EFFICIENCY_HYSTERESIS_FACTOR = 1.2
# Seconds to suppress saturation checks after a battery is promoted from
# deprioritized to active (inverter ramp-up); cleared early once the battery
# produces meaningful output.
SATURATION_GRACE_SECONDS = 90
# A battery still producing nothing after this long under a real target is
# marked saturated without waiting out the rest of its grace.
SATURATION_STALL_TIMEOUT_SECONDS = 60.0
# Smallest net output a DC-only battery (B2500 family) can produce: each DC
# channel is a hard on/off below ~40 W, so the unit cannot answer a command
# under ~80 W at all, and scoring saturation against one is self-reinforcing
# (issue #624).  A nominal fallback where neither the user's MIN_DC_OUTPUT nor
# observed evidence says otherwise (see saturation_floor).  The simulator
# models the same physics in b2500_steering.MIN_CHANNEL_OUTPUT_W; keep them in
# step but separate, so the plant can disagree with the controller.
DC_MIN_ACTIONABLE_OUTPUT_W = 80.0
# Reference poll interval (seconds) at which ``SATURATION_ALPHA`` and
# ``SATURATION_DECAY_FACTOR`` apply one full step; the EMA is time-weighted
# against it so batteries polling at different cadences converge to the same
# score.
SATURATION_REFERENCE_DT = 1.0
# A longer gap between saturation updates (battery offline) re-seeds the EMA
# instead of dosing it with a huge rise or decay step.
SATURATION_LONG_GAP_SECONDS = 30.0

# ---------------------------------------------------------------------------
# Device capabilities — every device-type decision (AC-charge eligibility, the
# MIN_DC_OUTPUT wake floor) derives from these, never from ad-hoc prefix checks.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class DeviceCapabilities:
    """What a battery model can physically do.

    - ``has_builtin_inverter``: produces its own AC output, so it never depends
      on a separate inverter that could sleep at low DC output.
    - ``has_ac_input``: can be charged from AC (Venus lineup).
    - ``has_dc_input``: has a DC (solar) input.
    - ``min_actionable_output_w``: smallest net output it can be commanded to
      produce; 0 when it follows a target down to its own deadband (see
      :func:`min_actionable_output`).
    """

    has_builtin_inverter: bool
    has_ac_input: bool
    has_dc_input: bool
    min_actionable_output_w: float = 0.0


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

    Unknown / empty device types are assumed AC-coupled (built-in inverter +
    AC input), so they are AC-chargeable and never floored by MIN_DC_OUTPUT.
    """
    dt = (device_type or "").upper()
    if dt.startswith(("VNSA", "VNSD")):
        return DeviceCapabilities(True, True, True)
    if dt.startswith(("HMG", "VNS")):
        return DeviceCapabilities(True, True, False)
    if dt.startswith(("HMN", "HMM", "JPLS")):
        return DeviceCapabilities(True, False, True)
    if dt.startswith(("HMA", "HMJ", "HMK")):
        return DeviceCapabilities(False, False, True, DC_MIN_ACTIONABLE_OUTPUT_W)
    return DeviceCapabilities(True, True, False)


def _is_ac_chargeable(device_type: str) -> bool:
    """True iff *device_type* can be charged from AC (the Venus lineup).

    Excludes DC-only batteries (B2500 family) from charge distribution under a
    grid surplus (issue #338); unknown types count as AC-chargeable.
    """
    return device_capabilities(device_type).has_ac_input


def _needs_dc_output_floor(device_type: str) -> bool:
    """True iff *device_type* depends on a sleep-prone *external* inverter.

    No built-in inverter and no AC input — the B2500 family — so its only way
    to stay awake is to keep discharging through that inverter.
    """
    caps = device_capabilities(device_type)
    return not caps.has_ac_input and not caps.has_builtin_inverter


def min_actionable_output(device_type: str) -> float:
    """The nominal start floor for *device_type* (see its capabilities)."""
    return device_capabilities(device_type).min_actionable_output_w


def saturation_floor(
    state: BalancerConsumerState, report: ConsumerReport, configured_floor: float
) -> float:
    """Smallest command worth judging this consumer by (W).

    The higher of two lower bounds: *configured_floor* (the effective
    MIN_DC_OUTPUT — a command at or below it may be our own parking command,
    and judging the battery by that is circular) and the model's start floor,
    lowered to ``pace_responded_at`` when this unit has answered something
    smaller.  That evidence is opportunistic — pacing records it only while its
    clamp is active and clears it on reversal — so its absence means "no
    evidence", never "cannot go lower".  A configured floor can raise the gate
    above the model's but never lower it below what the hardware can do
    (issue #600): a too-low gate starves the battery for good (issue #624), a
    too-high one only delays detecting a full or empty one.
    """
    nominal = min_actionable_output(report.device_type)
    if nominal > 0.0:
        observed = state.pace_responded_at
        if observed > 0.0:
            nominal = min(nominal, observed)
    return max(configured_floor, nominal)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BalancerConfig:
    """Tuning knobs for :class:`LoadBalancer`."""

    fair_distribution: bool = True
    balance_gain: float = 0.2
    # Share-rebalance deadband; kept above the battery firmware's own ±20 W
    # input deadband so the balancer never chases errors the battery ignores.
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
    # EMA factor for the household-demand estimate that sizes the active set
    # (see ``_compute_efficiency_deprioritized``); smoothing keeps meter noise
    # from thrashing a battery active/deprioritized.  ``1.0`` disables it.
    efficiency_demand_alpha: float = 0.1
    # Minimum net discharge (W) to hold an external-inverter DC battery (B2500
    # family, ``_needs_dc_output_floor``) at so its inverter never sleeps at
    # 0 W (issue #425).  0 disables.
    min_dc_output: float = 0
    # Ramp pacing (issue #458): each consumer's sent reading is clamped to a cap
    # that starts at ``pace_base_step`` (the firmware's first-step gain), grows
    # x2 toward ``pace_max_step`` only while the battery is observed tracking,
    # follows the error back down, and resets on direction reversal.  Bounds
    # the overshoot of the firmware's own gain-scheduled ramp under feedback
    # lag.  ``pace_base_step = 0`` disables.
    pace_base_step: float = 30
    pace_max_step: float = 100
    # Oscillation-gated damping (issue #473): an EMA of how often a consumer's
    # residual reverses sign — the signature of latency-driven hunting, not of
    # a load step — scales the residual down by up to ``osc_damp_max``.
    # ``0`` disables.
    osc_damp_max: float = 0.95
    osc_damp_alpha: float = 0.3
    osc_damp_decay: float = 0.05
    # A residual above this magnitude is a genuine demand step and passes at
    # full gain, so the damper never bleeds a real step response.
    osc_damp_threshold: float = 300
    # Adaptive grid-state predictor (see ``_predict_control_grid``): the control
    # path acts on a predicted grid that credits the pool's freshly reported
    # output before the meter shows it.  ``0`` disables; a positive value only
    # seeds the self-adapting trust, so ``0.5`` is a neutral default.
    grid_predict_trust: float = 0.5
    # Deadband concentration: below this absolute (predicted) grid error, with
    # more than one battery active on the same phase, the whole correction goes
    # to the most-active battery so it clears the firmware's ~20 W deadband
    # (see ``_compute_auto_target``).  ``0`` disables.
    concentrate_deadband: float = 60.0
    # Steady-import trim (W): every Marstek firmware parks the grid a few watts
    # to the import side of zero; once the predicted grid has held in a small
    # import band for a few fresh polls the control grid is nudged up by this
    # much (see ``_apply_import_trim``).  ``0`` disables.
    import_trim_w: float = 15.0

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
        _clamp("efficiency_demand_alpha", 0.01, 1.0)
        _clamp("min_dc_output", 0, float("inf"))
        _clamp("pace_base_step", 0, float("inf"))
        _clamp("pace_max_step", self.pace_base_step, float("inf"))
        _clamp("osc_damp_max", 0.0, 1.0)
        _clamp("osc_damp_alpha", 0.0, 1.0)
        _clamp("osc_damp_decay", 0.0, 1.0)
        _clamp("osc_damp_threshold", 0.0, float("inf"))
        _clamp("grid_predict_trust", 0.0, 1.0)
        _clamp("concentrate_deadband", 0.0, float("inf"))
        _clamp("import_trim_w", 0.0, float("inf"))


def split_balancer_knobs(knobs: Mapping[str, Any]) -> tuple[BalancerConfig, dict]:
    """Split flat tuning knobs into a config and whatever else was named.

    Scenario files and CLI overrides carry knobs flat, by field name; this is
    the one place that sorts them into the balancer's config. Python-only —
    the firmware takes its configuration from codegen.
    """
    mine = {f.name for f in dataclasses.fields(BalancerConfig)}
    return (
        BalancerConfig(**{k: v for k, v in knobs.items() if k in mine}),
        {k: v for k, v in knobs.items() if k not in mine},
    )


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
    # Absolute net-output target (NetOutputW) the control path intended for
    # this consumer, recorded *before* wire pacing, so the cross-talk
    # chrg/dchrg attribution can filter involuntary outputs such as PV
    # passthrough from a full battery (issue #376).
    last_intent: float | None = None
    # The *unpaced* grid reading the control path wanted to send, before
    # ``_pace_reading`` throttled it.  Saturation detection keys off this, not
    # ``last_target``: pacing pins a battery that can't follow its command at
    # the base step, so a full battery capped at e.g. 15 W would look idle
    # whenever ``pace_base_step < min_target`` and never saturate (issue #522).
    last_intent_reading: float | None = None
    fade_weight: float = 1.0
    # Ramp-pacing state (see BalancerConfig.pace_base_step): the current cap on
    # the sent reading in W per reference second, the sign of the last paced
    # reading, the reported power at the last pacing step (tracking detection)
    # and the wall-clock time of the last paced poll (0.0 = none yet).
    pace_cap: float = 0.0
    pace_sign: int = 0
    pace_prev_reported: float | None = None
    pace_last_at: float = 0.0
    # Consecutive clamped polls this consumer has not moved for (see
    # PACE_STALL_ESCAPE_POLLS).
    pace_stall_polls: int = 0
    # Smallest command this consumer has actually been observed to respond to,
    # in the current direction (see ``_pace_reading``). 0 = nothing learned yet.
    pace_responded_at: float = 0.0
    # The reading put on the wire last poll, used to attribute the movement
    # observed this poll to the command that caused it.
    pace_last_sent: float = 0.0
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


@dataclasses.dataclass(frozen=True, slots=True)
class BalancerConsumerSnapshot:
    """Immutable view of one consumer's control state.

    Produced by :meth:`LoadBalancer.snapshot_consumer` for the status API.
    Powers are watts; ``saturation`` and ``fade_weight`` are 0..1.
    """

    last_target: float | None
    last_intent: float | None
    last_intent_reading: float | None
    saturation: float
    saturation_grace_remaining: float
    fade_weight: float
    deprioritized: bool
    pace_cap: float
    pace_sign: int
    osc_score: float
    osc_last_sign: int


@dataclasses.dataclass(frozen=True, slots=True)
class PredictorSnapshot:
    """Adaptive grid-state observer state (see ``_predict_control_grid``)."""

    grid_estimate: float | None
    trust: float
    innovation_sign: int
    pool_output: float


@dataclasses.dataclass(frozen=True, slots=True)
class ImportTrimSnapshot:
    """Steady-import trim state (see ``_apply_import_trim``)."""

    dwell: int
    dwell_target: int
    gate: float
    engaged: bool


@dataclasses.dataclass(frozen=True, slots=True)
class EfficiencySnapshot:
    """Efficiency rotation / active-set state."""

    demand_ema: float | None
    priority_order: tuple[str, ...]
    deprioritized: tuple[str, ...]
    last_rotation_age: float
    all_dc_under_surplus: bool


@dataclasses.dataclass(frozen=True, slots=True)
class ProbeSnapshot:
    """In-flight efficiency handoff, or absent when no probe is running."""

    candidate_id: str
    active_ids: tuple[str, ...]
    backup_ids: tuple[str, ...]
    proof_samples: int
    requested_power_abs: float
    started_age: float
    deadline_in: float


# Deliberately a description of the *grid*, never a diagnosis of a cause: no
# signal measured here separates a hunting loop from a busy house or a noisy
# meter (a reversal fraction tracks the poll cadence, a crossing rate scores a
# jittery meter above an over-tuned loop, ``osc_score`` is a damping gate), and
# the remedies for the two are opposites.  The verdict states how far from zero
# and whether the pack had anything left; the crossing rate is published beside
# it (``crossings_per_min`` in HA) as evidence.
ControlQualityVerdict = Literal["idle", "warmup", "stable", "off_target", "limited"]

#: The verdict vocabulary, derived from the type so HA's enum ``options`` and
#: the dashboard's labels cannot drift from what the tracker can emit.
CONTROL_QUALITY_STATES: tuple[str, ...] = get_args(ControlQualityVerdict)


@dataclasses.dataclass(frozen=True, slots=True)
class ControlQualitySnapshot:
    """How well the loop is holding the grid at zero — and how it misses.

    ``idle``
        Nothing is being steered (no batteries polling, relay mode, or the
        pool has gone quiet).
    ``warmup``
        Steering, but too few samples yet to call it.
    ``stable``
        Mean grid error sits within a few settling bands — every load step
        lands on the meter before any battery can answer it.
    ``off_target``
        Mean grid error is beyond that while the pack still has headroom.
        *That* the grid is not held, deliberately not *why* (see
        :data:`ControlQualityVerdict`).
    ``limited``
        Same, but every battery is out of headroom for most of the window:
        the pack is full, empty or clamped and the loop is doing all it can.
    """

    verdict: ControlQualityVerdict
    #: 0..100, or ``None`` while there is nothing to score.  Accuracy of the
    #: mean error against the band only (see :meth:`ControlQualityTracker._score`).
    score: float | None
    #: Mean absolute grid error over the recent window, watts.
    error_ema: float
    #: Share of that window spent inside the band, 0..1.
    in_band_fraction: float
    #: Zero crossings per second among excursions large enough to matter — a
    #: rate, so it describes the house rather than the poll cadence.
    crossings_per_second: float
    #: The settling band the verdict is measured against, watts.
    band: float
    samples: int


@dataclasses.dataclass(frozen=True, slots=True)
class BalancerSnapshot:
    """Immutable whole-balancer view for the status API."""

    config: BalancerConfig
    efficiency_rotation_enabled: bool
    predictor: PredictorSnapshot
    import_trim: ImportTrimSnapshot
    efficiency: EfficiencySnapshot
    probe: ProbeSnapshot | None
    control_quality: ControlQualitySnapshot


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

    A score of 1.0 means the actuator cannot follow its target (battery
    full/empty); 0.0 means it is tracking well.  The EMA is weighted against
    :data:`SATURATION_REFERENCE_DT` so batteries polling at different cadences
    converge to the same score.  State lives in :class:`BalancerConsumerState`;
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
        self,
        state: BalancerConsumerState,
        last_target: float | None,
        actual: float,
        min_actionable: float,
    ) -> None:
        """Update the saturation score for a consumer.

        A target below *min_actionable* (the smallest command the device can
        physically execute) is treated like a below-``min_target`` one — too
        small to judge the battery by — otherwise a DC-only battery handed a
        sub-floor share is scored as unable to follow, cutting its share
        further for good (issue #624).
        """
        if not self._enabled or last_target is None:
            return
        now = self._clock()
        target_abs = abs(last_target)
        min_target = max(self._min_target, min_actionable)
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
                    target_abs >= min_target
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
        # Elapsed time since the previous EMA step.  A first sample (prev_t ==
        # 0) counts as one full reference period so a cold start responds to
        # the very first poll; a backwards clock (NTP correction) is clamped to
        # zero; a long gap (battery offline) is dropped and re-seeded so the
        # EMA is never dosed with hundreds of seconds of rise or decay.
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
        if target_abs < min_target or sign_reversing:
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
# Control quality
# ---------------------------------------------------------------------------


class ControlQualityTracker:
    """Judges the closed loop the way a user would: by what the meter shows.

    Reads the meter total the control path acts on — after the user's filter
    chain, never the balancer's internal prediction, so a confidently wrong
    estimate cannot flatter the verdict.  ``error_ema`` says whether there is
    a problem; whether it is the loop's fault is decided separately: when every
    battery has been out of headroom for most of the window the verdict is
    ``limited`` rather than blaming the controller.  It names no cause beyond
    that — see :data:`ControlQualityVerdict`.

    **Everything is measured per second, never per sample.**  A CT is fed once
    per poll *per battery*, so the sample rate describes the installation, not
    the house: a per-sample reversal fraction converges to ``2*dt/T`` for a
    limit cycle of period ``T`` and halves when a second battery joins.  The
    crossing figure is therefore accumulated as ``flip / dt``.

    Pool-level state, so unlike :class:`SaturationTracker` it owns what it
    tracks.
    """

    def __init__(self, band: float, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.time
        self._band = max(CONTROL_QUALITY_MIN_BAND_W, float(band))
        self._error_ema = 0.0
        self._in_band_ema = 0.0
        #: Zero crossings per second (see the class docstring), not per sample.
        self._crossings_ema = 0.0
        #: Share of the window the pool spent with no headroom, 0..1.  Time-
        #: weighted like everything else: whether the pack is exhausted is a
        #: claim about the same window ``error_ema`` covers, so deciding it
        #: from the latest sample alone would let one noisy reading excuse (or
        #: condemn) a minute of accumulated error.
        self._limited_ema = 0.0
        self._last_sign = 0
        self._samples = 0
        #: Seconds of steering actually observed in this window (see
        #: CONTROL_QUALITY_WARMUP_SECONDS).
        self._observed = 0.0
        self._last_update = 0.0
        self._steering = False

    def update(self, grid: float, *, steering: bool, limited: bool) -> None:
        """Fold one meter sample into the window.

        *grid* is the meter total the loop acted on (watts, + = import).
        *steering* is whether the balancer has anything to steer at all;
        *limited* whether the pool is out of headroom right now, in which case
        a persistent error is not the loop's doing.
        """
        now = self._clock()
        prev_t = self._last_update
        if prev_t <= 0.0:
            prev_t = now - CONTROL_QUALITY_REFERENCE_DT
        dt = max(0.0, now - prev_t)
        self._last_update = now
        self._steering = steering
        if dt == 0.0:
            return
        if dt > CONTROL_QUALITY_LONG_GAP_SECONDS:
            # The pool was away long enough that the old window describes a
            # different house.  Start over rather than blending across the gap.
            self._reset_window()
            return
        if not steering:
            # Nothing is being steered, so the meter is not evidence about a
            # loop.  Hold the window instead of scoring the house's own load.
            return

        error = abs(grid)
        in_band = 1.0 if error <= self._band else 0.0
        limit_hit = 1.0 if limited else 0.0
        if self._samples == 0:
            # Seed from the first sample: a cold EMA reads as a perfectly held
            # grid, which would report "stable" for the first minute of a loop
            # that is anything but.
            self._error_ema = error
            self._in_band_ema = in_band
            self._limited_ema = limit_hit
        else:
            ratio = dt / CONTROL_QUALITY_REFERENCE_DT
            alpha = 1.0 - (1.0 - CONTROL_QUALITY_ALPHA) ** ratio
            self._error_ema += alpha * (error - self._error_ema)
            self._in_band_ema += alpha * (in_band - self._in_band_ema)
            self._limited_ema += alpha * (limit_hit - self._limited_ema)
            # Crossings per second (see the class docstring), counted only
            # between excursions past the "stable" margin: a jittery meter
            # crosses zero constantly at small amplitude, and grading that as
            # hunting would flag the noisiest installs, not the worst-steered.
            flips_per_second = 0.0
            if error > self._band * CONTROL_QUALITY_STABLE_BANDS:
                sign = 1 if grid > 0 else -1
                if self._last_sign != 0 and sign != self._last_sign:
                    flips_per_second = 1.0 / dt
                self._last_sign = sign
            self._crossings_ema += alpha * (flips_per_second - self._crossings_ema)
        self._samples += 1
        self._observed += dt

    def snapshot(self) -> ControlQualitySnapshot:
        """Current verdict and its evidence.  Pure reads — no clock advance."""
        return ControlQualitySnapshot(
            verdict=self._verdict(),
            score=self._score(),
            error_ema=self._error_ema,
            in_band_fraction=self._in_band_ema,
            crossings_per_second=self._crossings_ema,
            band=self._band,
            samples=self._samples,
        )

    # -- internals -----------------------------------------------------

    def _reset_window(self) -> None:
        self._error_ema = 0.0
        self._in_band_ema = 0.0
        self._crossings_ema = 0.0
        self._limited_ema = 0.0
        self._last_sign = 0
        self._samples = 0
        self._observed = 0.0

    def _stale(self) -> bool:
        """Whether the last sample is old enough that there is no live loop.

        A device whose batteries all went away stops calling ``update``
        entirely, so without this the last verdict would hang on the dashboard
        forever, describing a pool that no longer exists.
        """
        if self._last_update <= 0.0:
            return True
        return (self._clock() - self._last_update) > CONTROL_QUALITY_LONG_GAP_SECONDS

    def _has_evidence(self) -> bool:
        """Whether the window says anything about a loop yet."""
        return (
            self._steering
            and not self._stale()
            and self._observed >= CONTROL_QUALITY_WARMUP_SECONDS
        )

    def _verdict(self) -> ControlQualityVerdict:
        if not self._steering or self._stale():
            return "idle"
        if self._observed < CONTROL_QUALITY_WARMUP_SECONDS:
            return "warmup"
        if self._error_ema <= self._band * CONTROL_QUALITY_STABLE_BANDS:
            return "stable"
        if self._limited_ema >= CONTROL_QUALITY_LIMITED_SHARE:
            return "limited"
        return "off_target"

    def _score(self) -> float | None:
        """0..100, or ``None`` while there is nothing to score.

        Absent rather than perfect: the EMAs start at zero, so a fresh or
        just-reset window would otherwise publish a flawless 100 — and a
        "score < 50" alert would clear itself every time the batteries
        dropped out for a minute.
        """
        if not self._has_evidence():
            return None
        excess = max(0.0, self._error_ema - self._band)
        accuracy = max(0.0, 1.0 - excess / (self._band * CONTROL_QUALITY_ERROR_SCALE))
        # Accuracy alone: discounting for a high crossing rate would penalise a
        # noisy meter harder than a badly steered loop (see ControlQualityVerdict).
        return 100.0 * accuracy


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
        # Diagnostics only (see ``_log_steer``): the two allocation
        # intermediates that live nowhere else once ``compute_target``
        # returns.  Reset per call so a manual/inactive consumer never
        # reports the previous consumer's figures.  Never read by the
        # control path.
        self._diag_control_grid: float | None = None
        self._diag_fair_share: float | None = None
        # Adaptive grid-state observer (see ``_predict_control_grid``):
        # ``_pred_grid`` is the grid estimate the control path acts on,
        # ``_pred_pool_output`` the pool's last-seen reported output (its
        # per-call delta advances the estimate), ``_pred_sample_id`` flags a
        # genuinely fresh meter reading, and ``_pred_trust`` /
        # ``_pred_innov_sign`` are the adaptive meter trust and the sign of the
        # last significant innovation that drove it.
        self._pred_grid: float | None = None
        self._pred_pool_output: float = 0.0
        self._pred_sample_id: tuple | None = None
        self._pred_trust: float = 0.0
        self._pred_innov_sign: int = 0
        # Count of consecutive *fresh* meter samples the predicted grid has held
        # inside the small-import band; gates the steady-import trim (see
        # ``_apply_import_trim``).  ``_trim_sample_id`` is the last meter sample
        # the trim acted on, used to tell a fresh reading from a repeated (stale /
        # frozen) one.
        self._steady_import_dwell: int = 0
        self._trim_sample_id: tuple = ()
        # Low-pass-filtered household-demand estimate for the efficiency
        # active-set decision (see ``_compute_efficiency_deprioritized``); keeps
        # meter noise from thrashing batteries in and out of the active pool.
        self._demand_ema: float | None = None
        # Closed-loop quality verdict for the status API and HA (see
        # ``ControlQualityTracker``).  Measured against the balancer's own
        # settling deadband, so it needs no setting of its own.
        self._control_quality = ControlQualityTracker(
            band=config.balance_deadband, clock=self._clock
        )

    @property
    def efficiency_rotation_enabled(self) -> bool:
        """True when efficiency rotation is active (``min_efficient_power > 0``).

        When disabled the balancer keeps every battery in the active pool, so
        there is nothing to rotate and the "Force Rotation" control is a no-op.
        """
        return self._cfg.min_efficient_power > 0

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

    def _commit_probe(self, reports: Reports, now: float, actual: float) -> None:
        probe = self._probe_state
        if probe is None:
            return
        participants = [
            cid for cid in (*probe.active_ids, *probe.backup_ids) if cid in reports
        ]
        total_actual = sum(abs(_report_of(reports, cid).power) for cid in participants)
        if total_actual > 0:
            for cid in participants:
                actual_share = abs(_report_of(reports, cid).power)
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
        # against a fresh baseline rather than an EMA still carrying pre-probe
        # state (including the transient zero-crossing while the candidate
        # ramps up and the backup drops out).  This tick's target is already
        # computed from the captured ``grid_total``; only the next reading
        # sees the reset.
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
        self, reports: Reports, now: float, grid_total: float
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
        actual = _report_of(reports, probe.candidate_id).power
        desired_total = sum(report.power for report in reports.values()) + grid_total
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
        reports: Reports,
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
        reports: Reports,
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

        desired_total = sum(report.power for report in reports.values()) + grid_total
        probe_actual = _report_of(reports, candidate_id).power
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
            return self._emit(
                consumer_id,
                NetOutputW(desired_probe),
                probe_actual,
                {candidate_id: reports[candidate_id]},
            )

        backup_weights = {
            cid: max(0.01, eff_part.get(cid, 1.0)) * _report_of(reports, cid).weight
            for cid in support_reports
        }
        qualified_probe_actual = probe_actual if probe.proof_samples > 0 else 0
        desired = self._compute_desired_contribution(
            consumer_id,
            support_reports,
            backup_weights,
            desired_total - qualified_probe_actual,
        )
        reported = _report_of(support_reports, consumer_id).power
        return self._emit(
            consumer_id, NetOutputW(desired), reported, support_reports, backup_weights
        )

    # ------------------------------------------------------------------
    # Primary interface
    # ------------------------------------------------------------------

    @staticmethod
    def _diag_num(value: float | None) -> str:
        """Compact fixed-point rendering; ``-`` when the figure doesn't apply."""
        return "-" if value is None else f"{value:.0f}"

    def _log_steer(
        self,
        consumer_id: str | None,
        mode: ConsumerMode,
        reports: Reports,
        grid_total: float,
        result: list[float],
    ) -> None:
        """Emit one DEBUG line recording how this consumer's command was decided.

        The wire carries only the final reading, on which a manual target, a
        deprioritized fade and a small auto share look alike (discussion #625).
        Nothing runs when DEBUG is off: ``logger.debug`` would discard the
        line, but only after rendering its arguments — real work on a busy pool.
        """
        if not consumer_id or not logger.isEnabledFor(logging.DEBUG):
            return
        state = self._consumers.get(consumer_id)
        if consumer_id in self._probe_participants():
            rotation = "probing"
        elif consumer_id in self._deprioritized:
            rotation = "deprioritized"
        else:
            rotation = "active"
        logger.debug(
            "CT002 steer %s: mode=%s rotation=%s weight=%.2f grid=%s ctrl=%s "
            "share=%s reported=%s intent=%s send=%s unpaced=%s pace_cap=%s "
            "sat=%.2f",
            consumer_id,
            f"manual={mode.manual_value:g}" if mode.mode == "manual" else mode.mode,
            rotation,
            _report_of(reports, consumer_id).weight,
            self._diag_num(grid_total),
            self._diag_num(self._diag_control_grid),
            self._diag_num(self._diag_fair_share),
            self._diag_num(_report_of(reports, consumer_id).power),
            self._diag_num(state.last_intent if state else None),
            self._diag_num(sum(result)),
            self._diag_num(state.last_intent_reading if state else None),
            self._diag_num(state.pace_cap if state else None),
            state.saturation_score if state else 0.0,
        )

    def compute_target(
        self,
        consumer_id: str | None,
        consumer_mode: ConsumerMode,
        all_reports: Reports,
        grid_total: float,
        inactive: frozenset[str],
        manual: frozenset[str],
        sample_id: tuple = (),
    ) -> list[float]:
        """Return ``[phase_A, phase_B, phase_C]`` target for *consumer_id*.

        *all_reports* holds every known consumer's :class:`ConsumerReport`.
        *inactive* / *manual* are the sets of paused and manual-override
        consumer IDs; this method filters internally.
        *sample_id* identifies the current meter reading for cache keying.
        """
        # Diagnostics only: cleared per call so a manual/inactive consumer
        # never reports the figures of whichever consumer ran before it.
        self._diag_control_grid = None
        self._diag_fair_share = None

        if consumer_mode.mode == "inactive":
            result = self._steer_to_zero(consumer_id, all_reports)
            self._log_steer(consumer_id, consumer_mode, all_reports, grid_total, result)
            return result

        # Reports excluding inactive consumers
        active_reports = {
            cid: r for cid, r in all_reports.items() if cid not in inactive
        }

        # Update saturation (skip manual, probe, and deprioritized consumers).
        # The detector keys off ``last_intent_reading`` — the *unpaced* command
        # — because pacing pins a battery that can't follow at the base step,
        # and the paced reading would make a full/empty battery look idle
        # (issue #522).  A deprioritized consumer is skipped because its fade
        # path still carries a transient non-zero command that would score as
        # "cannot follow" and lock ``_maybe_force_swap_saturated`` out of
        # promoting it back; its score stays at the zero the symmetric clear in
        # ``_compute_efficiency_deprioritized`` set.
        state = self._get_consumer(consumer_id) if consumer_id else None
        if (
            state is not None
            and consumer_id in active_reports
            and consumer_mode.mode != "manual"
            and consumer_id not in self._probe_participants()
            and consumer_id not in self._deprioritized
        ):
            report = _report_of(active_reports, consumer_id)
            actual = report.power
            # The floor is compared against the unpaced intent too: a battery
            # the clamp holds below its floor must still register as pushed
            # (issue #522); the stall escape bounds how long that lasts.
            self._saturation.update(
                state,
                state.last_intent_reading,
                actual,
                saturation_floor(
                    state,
                    report,
                    self._effective_min_dc_output(consumer_id, active_reports),
                ),
            )

        if consumer_mode.mode == "manual" and state is not None:
            reported = _report_of(active_reports, consumer_id).power
            result = self._emit(
                consumer_id,
                NetOutputW(consumer_mode.manual_value),
                reported,
                active_reports,
            )
            self._log_steer(
                consumer_id, consumer_mode, active_reports, grid_total, result
            )
            return result

        # Auto-pool reports (exclude manual consumers)
        reports = {cid: r for cid, r in active_reports.items() if cid not in manual}

        result = self._compute_auto_target(consumer_id, reports, grid_total, sample_id)
        result = self._apply_min_dc_output(consumer_id, reports, result)
        self._log_steer(consumer_id, consumer_mode, reports, grid_total, result)
        return result

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
        # ``fade_weight`` is the one field that survives: a consumer returning
        # to the auto pool resumes the rotation fade it was mid-way through
        # rather than snapping to full participation.  The three saturation
        # grace fields are reset here and immediately re-set by ``set_grace``.
        fade_weight = self._get_consumer(consumer_id).fade_weight
        state = BalancerConsumerState(fade_weight=fade_weight)
        self._consumers[consumer_id] = state
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
        self._prune_pool(current_pool)
        for state in self._consumers.values():
            state.fade_weight = 1.0
        logger.info(
            "Efficiency: forced rotation, new order: %s",
            [c[:16] for c in self._priority],
        )

    # ------------------------------------------------------------------
    # Read-only status surface (dashboard / diagnostics)
    # ------------------------------------------------------------------

    @property
    def config(self) -> BalancerConfig:
        """Effective, post-clamp balancer configuration.

        Safe to hand out by reference: :class:`BalancerConfig` is frozen.
        """
        return self._cfg

    def snapshot_consumer(self, consumer_id: str) -> BalancerConsumerSnapshot | None:
        """Per-consumer control state, or ``None`` if never steered.

        Pure attribute reads — no clock advance, no mutation.  See
        :meth:`status_snapshot` for the concurrency contract.
        """
        state = self._consumers.get(consumer_id)
        if state is None:
            return None
        grace_remaining = 0.0
        if state.saturation_grace_until > 0:
            grace_remaining = max(0.0, state.saturation_grace_until - self._clock())
        return BalancerConsumerSnapshot(
            last_target=state.last_target,
            last_intent=state.last_intent,
            last_intent_reading=state.last_intent_reading,
            saturation=state.saturation_score,
            saturation_grace_remaining=grace_remaining,
            fade_weight=state.fade_weight,
            deprioritized=consumer_id in self._deprioritized,
            pace_cap=state.pace_cap,
            pace_sign=state.pace_sign,
            osc_score=state.osc_score,
            osc_last_sign=state.osc_last_sign,
        )

    def status_snapshot(self) -> BalancerSnapshot:
        """Whole-balancer control state for the status API.

        MUST stay a plain ``def`` doing attribute reads only: the caller
        builds a snapshot of the live device tree between UDP handlers, so
        any ``await`` here would let a datagram tear the result.
        """
        probe = self._probe_state
        probe_snapshot = None
        if probe is not None:
            now = self._clock()
            probe_snapshot = ProbeSnapshot(
                candidate_id=probe.candidate_id,
                active_ids=probe.active_ids,
                backup_ids=probe.backup_ids,
                proof_samples=probe.proof_samples,
                requested_power_abs=probe.requested_power_abs,
                started_age=max(0.0, now - probe.started_at),
                deadline_in=probe.deadline - now,
            )
        return BalancerSnapshot(
            config=self._cfg,
            efficiency_rotation_enabled=self.efficiency_rotation_enabled,
            predictor=PredictorSnapshot(
                grid_estimate=self._pred_grid,
                trust=self._pred_trust,
                innovation_sign=self._pred_innov_sign,
                pool_output=self._pred_pool_output,
            ),
            import_trim=ImportTrimSnapshot(
                dwell=self._steady_import_dwell,
                dwell_target=IMPORT_TRIM_DWELL,
                gate=IMPORT_TRIM_GATE_W,
                engaged=self._steady_import_dwell >= IMPORT_TRIM_DWELL,
            ),
            efficiency=EfficiencySnapshot(
                demand_ema=self._demand_ema,
                priority_order=tuple(self._priority),
                deprioritized=tuple(sorted(self._deprioritized)),
                last_rotation_age=max(0.0, self._clock() - self._last_rotation),
                all_dc_under_surplus=self._all_dc_surplus_warned,
            ),
            probe=probe_snapshot,
            control_quality=self._control_quality.snapshot(),
        )

    def control_quality(self) -> ControlQualitySnapshot:
        """How well the loop is tracking zero (see ``ControlQualityTracker``).

        Same concurrency contract as :meth:`status_snapshot` — pure reads.
        """
        return self._control_quality.snapshot()

    def _pool_out_of_headroom(self, reports: Reports, grid_total: float) -> bool:
        """Whether the pool physically cannot close the remaining error.

        Either every battery is saturated, or there is a surplus nothing
        reporting can absorb; the remaining error is then the pack's limit,
        not the loop's.  Conservative: with saturation detection off the
        scores stay at zero and this returns ``False``, so a limited pack
        reads ``off_target`` rather than being excused without evidence.
        """
        if not reports:
            return False
        if grid_total < -self._cfg.balance_deadband and self._cannot_absorb(reports):
            return True
        return all(
            (state := self._consumers.get(cid)) is not None
            and state.saturation_score >= CONTROL_QUALITY_SATURATED
            for cid in reports
        )

    def _cannot_absorb(self, reports: Reports) -> bool:
        """Whether a surplus is genuinely beyond what the pool can take.

        Not simply "no AC-chargeable battery reporting": a DC-only battery
        cannot charge from AC, but while discharging it absorbs a surplus by
        discharging less, and only runs out of room at its ``MIN_DC_OUTPUT``
        floor.  Judging by device type alone would report a loop hunting
        symmetrically about zero on an all-DC pool as a full pack.
        """
        for cid, report in reports.items():
            if _is_ac_chargeable(report.device_type):
                return False
            # Output it could still give back.  The floor is where the battery
            # stops being able to reduce, so anything above it is headroom.
            floor = self._effective_min_dc_output(cid, reports)
            if report.power > floor + self._cfg.balance_deadband:
                return False
        return True

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

    def _effective_min_dc_output(
        self, consumer_id: str | None, reports: Reports
    ) -> float:
        """Per-consumer MIN_DC_OUTPUT floor (W); 0 means no floor.

        An explicit per-device override (``min_dc_output`` in the report) wins
        for any battery; otherwise the global floor applies only to batteries
        that depend on a sleep-prone external inverter (``_needs_dc_output_floor``).
        """
        report = _report_of(reports, consumer_id)
        if report.min_dc_output is not None:
            return report.min_dc_output
        if _needs_dc_output_floor(report.device_type):
            return self._cfg.min_dc_output
        return 0.0

    def _apply_min_dc_output(
        self, consumer_id: str | None, reports: Reports, result: list[float]
    ) -> list[float]:
        """Hold an external-inverter DC battery at ``MIN_DC_OUTPUT`` discharge.

        Wraps the auto-path result (manual/inactive return earlier) so a
        battery commanded below the floor keeps enough net discharge to stop
        its DC-fed inverter sleeping.  In tension with ``MIN_EFFICIENT_POWER``
        rotation: a unit parked for efficiency is held at the floor instead,
        which is stable while ``MIN_DC_OUTPUT >= saturation min_target``; a
        lower value masks saturation for a floored unit (main.py warns).
        """
        if not consumer_id or consumer_id not in reports:
            return result
        eff_min = self._effective_min_dc_output(consumer_id, reports)
        if eff_min <= 0:
            return result
        report = reports[consumer_id]
        # Respect an explicit park: distribution_weight=0 means "take no share",
        # i.e. sit at 0 — don't silently wake it (mirrors manual/inactive).
        if report.weight == 0:
            return result
        reported = report.power
        # Use the consumer's FULL intended reading: ``_split_by_phase`` spreads
        # the scalar across phases but preserves the total, so sum(result)
        # recovers it regardless of phase distribution. ``result[idx]`` alone is
        # only a phase-apportioned fragment.
        net_self = reported + sum(result)
        # Floor negative (charge) commands too: a floor-eligible battery cannot
        # charge, so a futile all-DC-under-surplus charge command must still be
        # lifted to the minimum discharge or the lone-B2500 case (issue #425)
        # never engages.  A per-device override on a chargeable battery thus
        # also holds a minimum discharge — the user opted in by setting it.
        if net_self >= eff_min:
            return result
        return self._emit(
            consumer_id,
            NetOutputW(eff_min),
            reported,
            reports,
            single_phase=report.phase,
        )

    def _steer_to_zero(
        self, consumer_id: str | None, reports: Reports, *, paced: bool = False
    ) -> list[float]:
        """Drive a consumer's output to zero (``NetOutputW(0)``).

        The auto-pool callers (deprioritized fade-out, charge-blind hold) pass
        ``paced=True``: the firmware applies a charge-direction reading in full
        in one cycle, so an unpaced wind-down dumps a discharging consumer's
        whole output in one poll and leaves the pool a step disturbance
        (issue #469).  Inactive consumers keep the one-shot behaviour — a
        user-initiated mode change, not a closed-loop handoff.
        """
        report = _report_of(reports, consumer_id)
        reported = report.power
        return self._emit(
            consumer_id,
            NetOutputW(0),
            reported,
            reports,
            pace=paced,
            single_phase=report.phase,
            # An unpaced wind-down is a one-shot command, recorded as 0 rather
            # than as the reading that carries it.
            last_target=None if paced else 0.0,
            intent_reading=0.0,
        )

    def _emit(
        self,
        consumer_id: str | None,
        desired: NetOutputW,
        reported: float,
        reports: Reports,
        weights: dict[str, float] | None = None,
        *,
        pace: bool = False,
        single_phase: str | None = None,
        last_target: float | None = None,
        intent_reading: float | None = None,
    ) -> list[float]:
        """Turn an absolute net-output target into the phase vector to send.

        The tail every steering path shares: convert *desired* to a grid
        reading, optionally ramp-pace it, record the intent triplet
        (``last_target`` / ``last_intent`` / ``last_intent_reading`` — see
        :class:`BalancerConsumerState`) and split the scalar across phases.

        *single_phase* puts the whole reading on that one phase instead of the
        weighted :meth:`_split_by_phase`.  *last_target* and *intent_reading*
        override what is recorded, for the steer-to-zero path: an unpaced
        wind-down records 0, and driving to zero is an intentional "produce
        nothing" command rather than a failed-to-follow one, so it registers as
        idle and lets saturation decay.
        """
        reading = to_grid_reading(desired, reported)
        unpaced = reading
        if pace and consumer_id:
            reading = self._pace_reading(consumer_id, reading, reported, reports)
        if consumer_id:
            state = self._get_consumer(consumer_id)
            state.last_target = reading if last_target is None else last_target
            state.last_intent = float(desired)
            state.last_intent_reading = (
                unpaced if intent_reading is None else intent_reading
            )
        if single_phase is not None:
            out = [0.0, 0.0, 0.0]
            out[phase_index(single_phase)] = reading
            return out
        return self._split_by_phase(reading, reports, weights)

    @staticmethod
    def _split_by_phase(
        target: float,
        reports: Reports,
        weights: dict[str, float] | None = None,
    ) -> list[float]:
        """Distribute *target* across phases proportional to weights."""
        phase_effective: dict[str, float] = {"A": 0.0, "B": 0.0, "C": 0.0}
        for cid, report in reports.items():
            phase = report.phase
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
        reports: Reports,
        grid_total: float,
        sample_id: tuple = (),
    ) -> list[float]:
        """Automatic allocation for auto-pool consumers."""
        # The predicted grid (meter-latency compensation, see
        # ``_predict_control_grid``) is updated on every call so it stays
        # continuous across the probe / fading / charge-blind early returns,
        # which keep using the raw meter for their categorical decisions; only
        # the residual loop below acts on it.
        # The trim integrates a steady-state bias, so it acts only on genuinely
        # fresh meter samples: a frozen / stale meter repeats its ``sample_id``,
        # and without this gate the trim would wind a blind bias the meter can
        # never correct (e.g. through a probe handoff).
        trim_fresh = sample_id != self._trim_sample_id
        self._trim_sample_id = sample_id
        # Control quality is judged on the raw meter before any early return
        # below, so a probe handoff or a fade leaves no holes in the window.
        # Not gated on ``trim_fresh``: a loop holding the grid still repeats
        # its reading, and skipping those samples would stale the verdict
        # exactly when the answer is "stable".  Safe because the EMAs are
        # time-weighted.
        self._control_quality.update(
            grid_total,
            steering=bool(reports),
            limited=self._pool_out_of_headroom(reports, grid_total),
        )
        control_grid = self._predict_control_grid(reports, grid_total, sample_id)
        control_grid = self._apply_import_trim(control_grid, trim_fresh)
        self._diag_control_grid = control_grid

        saturation = {cid: s.saturation_score for cid, s in self._consumers.items()}
        eff_part = {cid: max(0.01, 1.0 - saturation.get(cid, 0.0)) for cid in reports}

        charge_blind, any_ac_chargeable = self._charge_blind(reports, grid_total)
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

        self._note_all_dc_surplus(reports, grid_total, any_ac_chargeable)

        # A DC-only consumer under surplus must be told explicitly to hold
        # at 0 — don't fall through to the fair-share math where a residual
        # correction could leak a nonzero target.
        if consumer_id and consumer_id in charge_blind:
            return self._steer_to_zero(consumer_id, reports, paced=True)

        if any_fading and consumer_id:
            return self._fading_target(consumer_id, reports, grid_total, eff_part)

        for cid, fade_w in faded_adjustments.items():
            if cid in eff_part and fade_w == 0.0:
                eff_part[cid] = 0.0
        if (
            faded_adjustments
            and consumer_id
            and faded_adjustments.get(consumer_id) == 0.0
        ):
            return self._steer_to_zero(consumer_id, reports, paced=True)

        fair_share = self._fair_share(consumer_id, reports, control_grid, eff_part)
        concentrated = self._concentrated_share(
            consumer_id, reports, control_grid, eff_part, charge_blind
        )
        if concentrated is not None:
            fair_share = concentrated
        self._diag_fair_share = fair_share

        cfg = self._cfg

        # ``fair_share`` / ``_balance_correction`` produce the residual: this
        # consumer's slice of the grid imbalance to fold into its current
        # output.  The absolute net-output target is therefore "what I report
        # now plus my residual share" — see the NetOutputW wrap below.
        if (
            not cfg.fair_distribution
            or consumer_id is None
            or consumer_id not in reports
            or concentrated is not None
        ):
            residual = fair_share
        elif consumer_id in eff_part:
            residual = self._balance_correction(
                consumer_id, reports, eff_part, fair_share
            )
        else:
            residual = fair_share

        # Clamp only the grid-tracking half (``fair_share``, which carries the
        # grid's sign by construction) against the predicted grid direction —
        # never the balance-correction term, which is zero-sum across the
        # same-phase pool and so grid-neutral; zeroing it too would make
        # equalization one-sided near steady state (issue #523).
        tracking = fair_share
        if (control_grid < 0 and tracking > 0) or (control_grid > 0 and tracking < 0):
            tracking = 0.0
        residual = tracking + (residual - fair_share)

        if consumer_id:
            residual = self._damp_oscillation(consumer_id, residual)

        reported = _report_of(reports, consumer_id).power if consumer_id else 0
        return self._emit(
            consumer_id,
            NetOutputW(reported + residual),
            reported,
            reports,
            eff_part,
            pace=True,
        )

    @staticmethod
    def _charge_blind(reports: Reports, grid_total: float) -> tuple[set[str], bool]:
        """Batteries that can't absorb the current surplus, and whether any can.

        Excludes batteries that can't charge from AC (B2500 family, Jupiter;
        unknown types count as AC-chargeable) from charge distribution while the
        grid is in charge territory: ``grid_total < 0``, extended to the exact
        zero-crossing when an AC-chargeable battery is already charging — the
        pass-through equilibrium of a full B2500 passing its PV through while a
        Venus absorbs it, where the balance correction would otherwise oscillate
        the Venus out of its steady state (issue #338).  Not on ``grid_total ==
        0`` during pure discharge, since nothing is charging.  Conditioned on
        ``any_ac_chargeable``: with no AC-coupled battery there is nothing to
        protect, and the fair-share path handles brief negative-grid transients
        by reducing discharge smoothly instead of slamming the pool to 0 W
        (issue #359).
        """
        ac_charging = any(
            _is_ac_chargeable(r.device_type) and r.power < 0 for r in reports.values()
        )
        any_ac_chargeable = any(
            _is_ac_chargeable(r.device_type) for r in reports.values()
        )
        in_charge_territory = any_ac_chargeable and (
            grid_total < 0 or (grid_total == 0 and ac_charging)
        )
        charge_blind = (
            {cid for cid, r in reports.items() if not _is_ac_chargeable(r.device_type)}
            if in_charge_territory
            else set()
        )
        return charge_blind, any_ac_chargeable

    def _note_all_dc_surplus(
        self, reports: Reports, grid_total: float, any_ac_chargeable: bool
    ) -> None:
        """Log once while every reporter is DC-only under surplus.

        Nothing in the pool can absorb it.  Charge territory (see
        :meth:`_charge_blind`) stays off in this case so the fair-share path can
        still reduce discharge smoothly through brief negative-grid transients
        (issue #359); the B2500s' own AC-charge clamp holds them at 0 W under a
        sustained surplus regardless.
        """
        all_dc_under_surplus = (
            grid_total < 0 and bool(reports) and not any_ac_chargeable
        )
        if all_dc_under_surplus and not self._all_dc_surplus_warned:
            logger.info(
                "CT002: %.0f W surplus but no AC-chargeable battery "
                "reporting — nothing here can absorb it. Reporting "
                "device_types: %s",
                -grid_total,
                sorted({r.device_type or "?" for r in reports.values()}),
            )
            self._all_dc_surplus_warned = True
        elif not all_dc_under_surplus:
            self._all_dc_surplus_warned = False

    def _fading_target(
        self,
        consumer_id: str,
        reports: Reports,
        grid_total: float,
        eff_part: dict[str, float],
    ) -> list[float]:
        """Share the pool's demand by fade weight while a rotation is in flight."""
        fade_w = self._get_consumer(consumer_id).fade_weight
        if fade_w == 0.0:
            return self._steer_to_zero(consumer_id, reports, paced=True)
        reported = _report_of(reports, consumer_id).power
        total_battery = sum(report.power for report in reports.values())
        demand = total_battery + grid_total
        total_fade = sum(self._get_consumer(cid).fade_weight for cid in reports)
        desired = demand * fade_w / total_fade if total_fade > 0 else 0.0
        return self._emit(
            consumer_id, NetOutputW(desired), reported, reports, eff_part, pace=True
        )

    @staticmethod
    def _fair_share(
        consumer_id: str | None,
        reports: Reports,
        control_grid: float,
        eff_part: dict[str, float],
    ) -> float:
        """This consumer's weight-proportional slice of the grid error.

        Folds the per-battery user weight into the effectiveness map so the
        split honours the configured ratio; ``eff_part`` stays the pure health
        map (participation and probing).  The ``total_effective > 0`` guard also
        covers every share rounding to zero (charge-blind / faded / zero-weight):
        fall back to an even split.
        """
        share_part = {
            cid: eff_part[cid] * _report_of(reports, cid).weight for cid in eff_part
        }
        total_effective = sum(share_part.values())
        if consumer_id and consumer_id in reports and total_effective > 0:
            return (control_grid / total_effective) * share_part.get(consumer_id, 1.0)
        return control_grid / max(1, len(reports))

    def _concentrated_share(
        self,
        consumer_id: str | None,
        reports: Reports,
        control_grid: float,
        eff_part: dict[str, float],
        charge_blind: set[str],
    ) -> float | None:
        """Deadband concentration, or ``None`` when it doesn't apply this tick.

        Hands the whole small correction to the most-active battery
        (deterministic id tiebreak) so it clears the firmware deadband,
        bypassing balance correction this tick.  Restricted to participating
        batteries (a charge-blind B2500 passing PV can't absorb; zero-weight
        units take no share) on the *same* phase (``control_grid`` sums phases),
        gated on ``fair_distribution`` and on the pool already being balanced so
        it never suppresses the equalization of a real imbalance (issue #523).
        """
        cfg = self._cfg
        conc_ids = [
            cid
            for cid in reports
            if cid not in charge_blind
            and eff_part.get(cid, 0.0) > 0.1
            and reports[cid].weight > 0.0
        ]
        if not (
            cfg.fair_distribution
            and cfg.concentrate_deadband > 0
            and len(conc_ids) > 1
            and consumer_id in conc_ids
            and 0 < abs(control_grid) < cfg.concentrate_deadband
            and len({reports[c].phase for c in conc_ids}) == 1
            and self._concentration_pool_balanced(reports, conc_ids)
        ):
            return None
        designated = max(
            conc_ids,
            key=lambda c: (abs(reports[c].power), c),
        )
        return control_grid if consumer_id == designated else 0.0

    def _predict_control_grid(
        self, reports: Reports, grid_total: float, sample_id: tuple
    ) -> float:
        """Return the grid power the control path should act on.

        An online observer that compensates for meter latency: every call the
        estimate is advanced by the pool's reported output change (a correction
        is credited the moment the battery delivers it, so the loop never
        re-issues one still in flight), and each fresh reading pulls it toward
        the meter by an adaptive trust — the only channel by which disturbances
        the pool did not cause enter.  The trust is learned from the innovation
        ``meter - estimate``: same-sign runs raise it, sign flips shrink it,
        and only innovations above ``PRED_INNOVATION_GATE_W`` adapt it.
        Returns the raw total when ``grid_predict_trust <= 0``.
        """
        if self._cfg.grid_predict_trust <= 0.0:
            return grid_total
        pool_output = sum(r.power for r in reports.values())
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

    def _apply_import_trim(self, control_grid: float, fresh: bool) -> float:
        """Cover the small residual grid *import* the battery firmware leaves.

        Every Marstek firmware parks the grid a few watts to the import side of
        zero (the HMG ramp's small-import hold and ±20 W deadband, the Venus-D
        integrator's -5 W bias).  Once the predicted grid has sat inside
        ``(0, IMPORT_TRIM_GATE_W)`` for :data:`IMPORT_TRIM_DWELL` consecutive
        fresh samples, add ``import_trim_w`` so the firmware discharges to
        cover it.  The dwell keeps it inert during transients, the gate keeps
        it clear of a saturated/empty pack, and a stale sample neither advances
        nor fires it (no feedback to bound it).  ``import_trim_w = 0`` disables.
        """
        trim = self._cfg.import_trim_w
        if trim <= 0 or not fresh:
            return control_grid
        if 0.0 < control_grid < IMPORT_TRIM_GATE_W:
            self._steady_import_dwell += 1
        else:
            self._steady_import_dwell = 0
        if self._steady_import_dwell >= IMPORT_TRIM_DWELL:
            return control_grid + trim
        return control_grid

    def _damp_oscillation(self, consumer_id: str, residual: float) -> float:
        """Scale ``residual`` down while the consumer is hunting (issue #473).

        Tracks an EMA (``osc_score``) of how often the residual reverses sign.
        A genuine load step holds one sign, so the score decays to 0 and the
        residual passes through unchanged; a latency-driven limit cycle flips
        sign nearly every poll, driving the score toward 1 and shrinking the
        residual by up to ``osc_damp_max`` — bleeding the loop gain that
        sustains the hunt without slowing same-direction reactions.
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
        # bleed it by ``osc_damp_decay`` otherwise: a few reversals (a solar
        # ramp crossing zero, the ring-down after a load step) only nudge it,
        # and only repeated reversals — a hunting limit cycle — engage the
        # damping.
        if sign != 0 and state.osc_last_sign != 0 and sign != state.osc_last_sign:
            state.osc_score = min(1.0, state.osc_score + cfg.osc_damp_alpha)
        else:
            state.osc_score *= 1.0 - cfg.osc_damp_decay
        if sign != 0:
            state.osc_last_sign = sign
        return residual * (1.0 - cfg.osc_damp_max * state.osc_score)

    def _pace_reading(
        self, consumer_id: str, reading: float, reported: float, reports: Reports
    ) -> float:
        """Clamp the auto-path *reading* to the consumer's ramp-pacing cap.

        The battery integrates the reading with its own accelerating ramp, so
        the reading we send is the only bound on its per-poll movement.  The
        cap starts at ``pace_base_step``, doubles per reference second toward
        ``pace_max_step`` only while the battery demonstrably tracks the
        command, follows the error back down, and resets to the base step on
        direction reversal — bounding stale-feedback overshoot to the battery's
        *demonstrated* slew.  Caps are W per :data:`PACE_REFERENCE_DT`, scaled
        by the observed inter-poll time (clamped at 1.0).

        Paced: the regulation loop, the fade transition and the deprioritized /
        charge-blind wind-down (the firmware applies a charge-direction reading
        in full in one cycle, so an unpaced wind-down is a one-poll step
        disturbance on the rest of the pool).  Not paced: probe targets, the
        MIN_DC_OUTPUT floor, manual targets and the inactive steer-to-zero.
        Callers needing the unpaced intent (issue #376) read ``last_intent``.
        """
        base = self._cfg.pace_base_step
        if base <= 0:
            return reading
        state = self._get_consumer(consumer_id)
        now = self._clock()
        dt = now - state.pace_last_at if state.pace_last_at > 0.0 else 0.0
        if dt <= 0.0:
            # First paced poll, a non-advancing clock, or a backwards jump:
            # assume one reference period rather than starving the clamp.
            dt = PACE_REFERENCE_DT
        state.pace_last_at = now
        dt_ratio = min(1.0, dt / PACE_REFERENCE_DT)
        sign = 1 if reading > 0 else -1 if reading < 0 else 0
        cap = state.pace_cap if state.pace_cap > 0 else base
        # Never below the base step: hysteresis-style regulators (B2500) need a
        # minimum reading to clear their input hold window at all.  The cadence
        # scale still bounds the grown cap.
        limit = max(base, cap * dt_ratio)
        # The stall escape and the response floor below apply only to devices
        # with a minimum actionable command (the DC-output family); any other
        # battery can execute an arbitrarily small command and can never be
        # deadlocked by the clamp, so it stays on the unmodified path.
        can_stall = _needs_dc_output_floor(_report_of(reports, consumer_id).device_type)
        stalled = False
        if sign == 0 or sign != state.pace_sign:
            cap = base
            state.pace_stall_polls = 0
            # A reversal re-opens the question of what this device responds to
            # in the new direction; nothing learned going one way carries over.
            state.pace_responded_at = 0.0
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
                state.pace_stall_polls = 0
                # It moved, so last poll's command is one this device can
                # execute; clamping back under it would switch a hysteresis
                # regulator straight off again.  Keep the *lowest* such
                # command, since during a ramp the latest would ratchet the
                # floor up to the largest.
                if state.pace_last_sent > 0 and (
                    state.pace_responded_at <= 0
                    or state.pace_last_sent < state.pace_responded_at
                ):
                    state.pace_responded_at = state.pace_last_sent
            else:
                # Held below what the device can act on: grow anyway once the
                # stall has persisted, or the clamp is self-sustaining (see
                # PACE_STALL_ESCAPE_POLLS).
                stalled = can_stall
                state.pace_stall_polls += 1
                if can_stall and state.pace_stall_polls >= PACE_STALL_ESCAPE_POLLS:
                    cap = min(
                        cap * PACE_GROWTH_FACTOR**dt_ratio, self._cfg.pace_max_step
                    )
                    state.pace_stall_polls = 0
        else:
            cap = max(base, abs(reading) / dt_ratio)
            state.pace_stall_polls = 0
        # Enforce the pace_max_step contract: the grow branch already clamps,
        # but the else branch back-computes cap as abs(reading) / dt_ratio,
        # which a fast poll (small dt_ratio) can inflate past the max — and a
        # later normal-cadence poll would then slew beyond pace_max_step.
        cap = min(cap, self._cfg.pace_max_step)
        state.pace_cap = cap
        state.pace_sign = sign
        state.pace_prev_reported = reported
        # A stalled device integrates nothing, so there is no slew for the
        # cadence scale to bound — and scaling a fast poller's clamp back to
        # ``base`` is what keeps it stalled (``max(base, cap * dt_ratio)`` stays
        # at ``base`` until ``cap`` reaches ``base / dt_ratio``).  While stalled,
        # clamp on the unscaled cap; the scale resumes on the first poll it moves.
        limit = max(base, cap if stalled else cap * dt_ratio)
        # Never clamp under a level this device has demonstrably responded to:
        # for a hysteresis regulator a smaller command is not a gentler one but
        # an *off* one, and the stall escape would have to lift it again
        # indefinitely.  Still bounded by ``pace_max_step``.  This costs
        # worst-case overshoot (the command that starts the device is one it
        # responds to hard), so it is confined to the devices that can fail to
        # start.
        if can_stall:
            limit = min(max(limit, state.pace_responded_at), self._cfg.pace_max_step)
        out = max(-limit, min(limit, reading))
        state.pace_last_sent = abs(out)
        return out

    def _concentration_pool_balanced(
        self, reports: Reports, conc_ids: list[str]
    ) -> bool:
        """True iff every battery in *conc_ids* already sits at its fair share.

        Deadband concentration bypasses balance correction for the tick, so it
        must only engage on an already-balanced pool — otherwise a real
        imbalance (issue #523: 88 W vs 890 W) would be pinned forever.  "In
        balance" is within ``balance_deadband`` of the weight-proportional
        share of the pool total, the same target ``_balance_correction`` uses.
        """
        deadband = self._cfg.balance_deadband
        if deadband <= 0:
            # No deadband means balance correction always runs at full authority;
            # never let concentration suppress it.
            return False
        actual_total = sum(_report_of(reports, cid).power for cid in conc_ids)
        weights = {cid: _report_of(reports, cid).weight for cid in conc_ids}
        total_weight = sum(weights.values())
        for cid in conc_ids:
            actual_self = _report_of(reports, cid).power
            if total_weight > 0:
                target_share = actual_total * weights[cid] / total_weight
            else:
                target_share = actual_total / len(conc_ids)
            if abs(target_share - actual_self) >= deadband:
                return False
        return True

    def _balance_correction(
        self,
        consumer_id: str,
        reports: Reports,
        eff_part: dict[str, float],
        fair_share: float,
    ) -> float:
        """Apply fair-share balance correction for *consumer_id*."""
        cfg = self._cfg
        actual_self = _report_of(reports, consumer_id).power
        participating = [cid for cid in reports if eff_part.get(cid, 1.0) > 0.1]
        if not participating:
            return fair_share

        actual_total = sum(_report_of(reports, cid).power for cid in participating)
        # Pull each battery toward its weight-proportional share of the pool's
        # total output, so the configured ratio is the steady state; with
        # neutral weights this is the plain average.  Participation is still
        # decided by ``eff_part`` above, so a small weight never drops a
        # healthy battery from the pool.
        weights = {cid: _report_of(reports, cid).weight for cid in participating}
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

    def _sync_pool(self, reports: Reports, grace: float) -> None:
        """Reconcile the rotation order with the reporting pool.

        Drops departed consumers, appends new arrivals (in id order, each with a
        settling grace), then sinks low-weight batteries to the back so they fall
        into the deprioritized tail first while limiting; the *stable* sort
        preserves the fair-wear rotation cycle within each weight tier.
        """
        current = set(reports)
        self._priority = [c for c in self._priority if c in current]
        self._deprioritized.intersection_update(current)
        for cid in sorted(current):
            if cid not in self._priority:
                self._priority.append(cid)
                self._set_consumer_grace(cid, grace)
        self._priority.sort(
            key=lambda cid: _report_of(reports, cid).efficiency_window_weight,
            reverse=True,
        )

    def _demand_estimate(self, reports: Reports, grid_total: float) -> float:
        """Low-pass-filtered household demand driving the active-set decision.

        ``|total_battery_power + grid_total|`` is the true house load; filtering
        it by ``efficiency_demand_alpha`` makes the active set follow *sustained*
        demand rather than meter noise (see the config field).  The regulation
        loop still acts on the unsmoothed grid, so tracking is unaffected.
        """
        total_battery_power = sum(
            _report_of(reports, cid).power for cid in self._priority
        )
        raw_abs_target = abs(total_battery_power + grid_total)
        alpha = self._cfg.efficiency_demand_alpha
        if self._demand_ema is None or alpha >= 1.0:
            self._demand_ema = raw_abs_target
        else:
            self._demand_ema += alpha * (raw_abs_target - self._demand_ema)
        return self._demand_ema

    def _active_slots(
        self, abs_target: float, n: int, was_limiting: bool, prev_slots: int
    ) -> int:
        """How many of *n* pooled batteries keep an active slot at *abs_target*.

        Entering the limiting regime is immediate; leaving it takes a 20% margin
        (``EFFICIENCY_HYSTERESIS_FACTOR``), and so does *growing* the active set
        while limiting — without that, demand sitting at an exact multiple of
        ``min_efficient_power`` toggles a unit on every meter-noise tick (issue
        #469).  Shrinking stays immediate, like entering.
        """
        cfg = self._cfg
        per_consumer = abs_target / n
        floor = cfg.min_efficient_power
        if was_limiting:
            floor = floor * EFFICIENCY_HYSTERESIS_FACTOR
        if not (per_consumer < floor and n > 1):
            return n
        slots = max(1, min(n - 1, int(abs_target / cfg.min_efficient_power)))
        if was_limiting and 1 <= prev_slots < slots:
            grown = int(
                abs_target / (cfg.min_efficient_power * EFFICIENCY_HYSTERESIS_FACTOR)
            )
            slots = max(prev_slots, min(n - 1, grown))
        return slots

    def _compute_efficiency_deprioritized(
        self, reports: Reports, sample_id: tuple, grid_total: float
    ) -> dict[str, float]:
        """Decide which consumers to deprioritize for efficiency."""
        cfg = self._cfg
        if cfg.min_efficient_power <= 0 or len(reports) < 2:
            self._probe_state = None
            self._deprioritized = set()
            self._invalidate_efficiency_cache()
            return {}

        now = self._clock()
        grace = now + min(
            self._saturation_grace_seconds, cfg.efficiency_rotation_interval
        )
        self._sync_pool(reports, grace)

        prev_slots = max(
            0, min(len(self._priority), len(self._priority) - len(self._deprioritized))
        )
        previous_active = tuple(self._priority[:prev_slots])
        probe_resolved = self._resolve_probe_state(reports, now, grid_total)
        probe_active = self._probe_state is not None

        # Rotation check BEFORE cache. The active head holds its slot for
        # ``efficiency_rotation_interval`` scaled by its efficiency window weight,
        # so a lower-weight battery rotates out sooner (weight 0 → threshold 0 →
        # it rotates out on the next tick).
        if not probe_active and not probe_resolved and self._priority:
            head_weight = _report_of(
                reports, self._priority[0]
            ).efficiency_window_weight
            if (
                now - self._last_rotation
                >= cfg.efficiency_rotation_interval * head_weight
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

        abs_target = self._demand_estimate(reports, grid_total)
        slots = self._active_slots(
            abs_target,
            len(self._priority),
            was_limiting=len(self._deprioritized) > 0,
            prev_slots=prev_slots,
        )

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
                # Symmetric with the deprioritized -> active clear above: the
                # score is a memory of the previous role, and a consumer
                # steered toward zero cannot be judged by it.  Without the
                # clear, saturation accumulated over the fading window would
                # bar ``_maybe_force_swap_saturated`` from ever promoting it.
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

        Healthy means a saturation score *strictly below*
        ``efficiency_saturation_threshold``.  Relies on
        :meth:`_compute_efficiency_deprioritized` clearing the score on the
        active -> deprioritized transition, so a healthy candidate exists the
        first time a newly saturated active unit needs swapping out post-probe.
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

    def _prune_pool(self, keep: set[str]) -> None:
        """Drop the control state of consumers that have left the pool.

        ``_consumers`` runs parallel to ``_priority``, so a consumer that stops
        reporting but still holds a rotation slot keeps its state until the slot
        goes too.
        """
        for cid in list(self._consumers):
            if cid not in keep and cid not in self._priority:
                del self._consumers[cid]

    def _fade_efficiency_weights(
        self, raw_adjustments: dict[str, float], consumer_ids: set[str]
    ) -> dict[str, float]:
        """Apply EMA fade to efficiency weights for smooth transitions.

        This is also the per-poll pass that drops control state for consumers
        that have left the pool (see :meth:`_prune_pool`).
        """
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
        self._prune_pool(consumer_ids)
        return result
