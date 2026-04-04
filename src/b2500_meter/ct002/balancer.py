"""Load balancing with efficiency optimization and saturation detection."""

from __future__ import annotations

import dataclasses
import time
from typing import Literal, NamedTuple

from b2500_meter.config.logger import logger

from .protocol import parse_int

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


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class BalancerConfig:
    """Tuning knobs for :class:`LoadBalancer`."""

    fair_distribution: bool = True
    balance_gain: float = 0.2
    balance_deadband: float = 15
    error_boost_threshold: float = 150
    error_boost_max: float = 0.5
    error_reduce_threshold: float = 20
    max_correction_per_step: float = 80
    max_target_step: float = 0
    deadband: float = 20
    min_efficient_power: float = 0
    probe_min_power: float = 80
    efficiency_rotation_interval: float = 900
    efficiency_fade_alpha: float = 0.15
    efficiency_saturation_threshold: float = 0.4

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
        _clamp("deadband", 0, float("inf"))
        _clamp("min_efficient_power", 0, float("inf"))
        _clamp("probe_min_power", 0, float("inf"))
        _clamp("efficiency_rotation_interval", 1, float("inf"))
        _clamp("efficiency_fade_alpha", 0.01, 1.0)
        _clamp("efficiency_saturation_threshold", 0.0, 1.0)


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
    fade_weight: float = 1.0
    saturation_score: float = 0.0
    saturation_grace_until: float = 0.0
    saturation_grace_started_at: float = 0.0


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
    """EMA-based actuator saturation detector with grace periods.

    A saturation score of 1.0 means the actuator cannot follow its target
    (e.g. battery full/empty); 0.0 means it is tracking well.

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
    ) -> None:
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
        now = time.time()
        target_abs = abs(last_target)
        # Grace period handling
        if state.saturation_grace_until > 0:
            if now < state.saturation_grace_until:
                if abs(actual) >= self._min_target:
                    state.saturation_grace_until = 0.0
                    state.saturation_grace_started_at = 0.0
                elif (
                    target_abs >= self._min_target
                    and state.saturation_grace_started_at > 0
                    and now - state.saturation_grace_started_at
                    >= self._stall_timeout_seconds
                ):
                    state.saturation_score = 1.0
                    state.saturation_grace_until = 0.0
                    state.saturation_grace_started_at = 0.0
                    return
                else:
                    return
            else:
                state.saturation_grace_until = 0.0
                state.saturation_grace_started_at = 0.0
        if target_abs < self._min_target:
            prev = state.saturation_score
            if prev > 0:
                decayed = prev * self._decay_factor
                if decayed < 0.001:
                    state.saturation_score = 0.0
                else:
                    state.saturation_score = decayed
            return
        inst_saturation = 1.0 if abs(actual) < self._min_target else 0.0
        prev = state.saturation_score
        state.saturation_score = (
            self._alpha * inst_saturation + (1 - self._alpha) * prev
        )

    def get(self, state: BalancerConsumerState) -> float:
        return state.saturation_score

    def set_grace(self, state: BalancerConsumerState, deadline: float) -> None:
        state.saturation_grace_until = deadline
        state.saturation_grace_started_at = time.time()

    def clear(self, state: BalancerConsumerState) -> None:
        state.saturation_score = 0.0
        state.saturation_grace_until = 0.0
        state.saturation_grace_started_at = 0.0


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
    ) -> None:
        self._cfg = config
        self._saturation = SaturationTracker(
            alpha=saturation_alpha,
            enabled=saturation_enabled,
            min_target=saturation_min_target,
            decay_factor=saturation_decay_factor,
            stall_timeout_seconds=saturation_stall_timeout_seconds,
        )
        self._saturation_grace_seconds = max(0.0, saturation_grace_seconds)
        self._consumers: dict[str, BalancerConsumerState] = {}
        self._deprioritized: set[str] = set()
        self._priority: list[str] = []
        self._last_rotation: float = time.time()
        self._cache_sample: tuple | None = None
        self._cache_result: dict[str, float] | None = None
        self._probe_state: ProbeState | None = None
        self._probe_timeout_seconds = max(0.0, saturation_grace_seconds)
        self._probe_success_threshold = max(1.0, float(saturation_min_target))
        self._post_probe_fade_until = 0.0
        self._post_probe_fade_ids: set[str] = set()

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
            cid for cid in self._priority if cid not in probe.restore_active_ids
        ]
        self._priority = list(probe.restore_active_ids) + [
            cid for cid in remaining if cid not in probe.restore_active_ids
        ]
        self._probe_state = None
        self._last_rotation = now
        logger.info(
            "Efficiency: probe rejected for %s (%s), restoring backups %s",
            probe.candidate_id[:16],
            reason,
            [cid[:16] for cid in probe.backup_ids],
        )
        self._invalidate_efficiency_cache()

    def _resolve_probe_state(
        self, reports: dict, now: float, smoothed_target: float
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
            + smoothed_target
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
        smoothed_target: float,
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
            + smoothed_target
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
            target = desired_probe - probe_actual
            state.last_target = target
            return self._split_by_phase(target, {candidate_id: reports[candidate_id]})

        backup_weights = {
            cid: max(0.01, eff_part.get(cid, 1.0)) for cid in support_reports
        }
        qualified_probe_actual = probe_actual if probe.proof_samples > 0 else 0
        desired = self._compute_desired_contribution(
            consumer_id,
            support_reports,
            backup_weights,
            desired_total - qualified_probe_actual,
        )
        reported = parse_int(support_reports.get(consumer_id, {}).get("power", 0))
        target = desired - reported
        state.last_target = target
        return self._split_by_phase(target, support_reports, backup_weights)

    # ------------------------------------------------------------------
    # Primary interface
    # ------------------------------------------------------------------

    def compute_target(
        self,
        consumer_id: str | None,
        consumer_mode: ConsumerMode,
        all_reports: dict,
        smoothed_target: float,
        raw_total: float,
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

        # Update saturation (skip manual consumers)
        state = self._get_consumer(consumer_id) if consumer_id else None
        last_target = state.last_target if state else None
        if (
            consumer_id
            and state
            and consumer_id in active_reports
            and consumer_mode.mode != "manual"
            and consumer_id not in self._probe_participants()
        ):
            actual = parse_int(active_reports.get(consumer_id, {}).get("power", 0))
            self._saturation.update(state, last_target, actual)

        # --- Manual override ---
        if consumer_mode.mode == "manual" and consumer_id and state:
            reported = parse_int(active_reports.get(consumer_id, {}).get("power", 0))
            target = consumer_mode.manual_value - reported
            state.last_target = target
            return self._split_by_phase(target, active_reports)

        # Auto-pool reports (exclude manual consumers)
        reports = {cid: r for cid, r in active_reports.items() if cid not in manual}

        return self._compute_auto_target(
            consumer_id, reports, smoothed_target, raw_total, sample_id
        )

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
        state.saturation_score = 0.0
        grace = time.time() + min(
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
        self._last_rotation = time.time()
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

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _steer_to_zero(self, consumer_id: str | None, reports: dict) -> list[float]:
        """Drive a consumer's output to zero."""
        if consumer_id:
            self._get_consumer(consumer_id).last_target = 0
        reported = parse_int(
            reports.get(consumer_id, {}).get("power", 0) if consumer_id else 0
        )
        if reported == 0:
            return [0, 0, 0]
        phase = (
            reports.get(consumer_id, {}).get("phase") or "A" if consumer_id else "A"
        ).upper()
        result = [0.0, 0.0, 0.0]
        result[{"A": 0, "B": 1, "C": 2}.get(phase, 0)] = float(-reported)
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
        smoothed_target: float,
        raw_total: float,
        sample_id: tuple = (),
    ) -> list[float]:
        """Automatic allocation for auto-pool consumers."""
        saturation = {cid: s.saturation_score for cid, s in self._consumers.items()}
        num_consumers = max(1, len(reports))
        eff_part = {cid: max(0.01, 1.0 - saturation.get(cid, 0.0)) for cid in reports}

        efficiency_adjustments = self._compute_efficiency_deprioritized(
            reports, sample_id, smoothed_target
        )
        faded_adjustments = self._fade_efficiency_weights(
            efficiency_adjustments, set(reports.keys())
        )
        any_fading = any(0.0 < w < 1.0 for w in faded_adjustments.values())

        probe_target = self._compute_probe_target(
            consumer_id, reports, smoothed_target, eff_part
        )
        if probe_target is not None:
            return probe_target

        # --- Fading path ---
        if any_fading and consumer_id:
            state = self._get_consumer(consumer_id)
            fade_w = state.fade_weight
            reported = parse_int(reports.get(consumer_id, {}).get("power", 0))
            if fade_w == 0.0:
                return self._steer_to_zero(consumer_id, reports)

            total_battery = sum(
                parse_int(reports.get(cid, {}).get("power", 0)) for cid in reports
            )
            demand = total_battery + smoothed_target
            total_fade = sum(self._get_consumer(cid).fade_weight for cid in reports)
            desired = demand * fade_w / total_fade if total_fade > 0 else 0.0
            target = desired - reported

            state.last_target = target

            return self._split_by_phase(target, reports, eff_part)

        # --- Non-fading path ---
        for cid, fade_w in faded_adjustments.items():
            if cid in eff_part and fade_w == 0.0:
                eff_part[cid] = 0.0
        if (
            faded_adjustments
            and consumer_id
            and faded_adjustments.get(consumer_id) == 0.0
        ):
            return self._steer_to_zero(consumer_id, reports)

        total_effective = sum(eff_part.values())
        fair_share = (
            (smoothed_target / total_effective) * eff_part.get(consumer_id, 1.0)
            if consumer_id and consumer_id in reports
            else smoothed_target / num_consumers
        )

        cfg = self._cfg
        if (
            not cfg.fair_distribution
            or consumer_id is None
            or consumer_id not in reports
            or (cfg.deadband > 0 and abs(raw_total) < cfg.deadband)
        ):
            target = fair_share
        elif consumer_id in eff_part:
            target = self._balance_correction(
                consumer_id, reports, eff_part, fair_share
            )
        else:
            target = fair_share

        # Clamp sign disagreement
        if (raw_total < 0 and target > 0) or (raw_total > 0 and target < 0):
            target = 0

        if consumer_id:
            self._get_consumer(consumer_id).last_target = target

        return self._split_by_phase(target, reports, eff_part)

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
        actual_avg = actual_total / len(participating)
        error = actual_avg - actual_self
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
        self, reports: dict, sample_id: tuple, smoothed_target: float
    ) -> dict[str, float]:
        """Decide which consumers to deprioritize for efficiency."""
        cfg = self._cfg
        if cfg.min_efficient_power <= 0 or len(reports) < 2:
            self._probe_state = None
            self._deprioritized = set()
            self._invalidate_efficiency_cache()
            return {}

        now = time.time()
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
        probe_resolved = self._resolve_probe_state(reports, now, smoothed_target)
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
        abs_target = abs(total_battery_power + smoothed_target)
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
                state.saturation_grace_until = 0.0
                state.saturation_grace_started_at = 0.0
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
        """Swap a saturated active battery with a healthy deprioritized one."""
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
        now = time.time()
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
