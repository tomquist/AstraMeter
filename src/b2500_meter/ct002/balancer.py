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
SATURATION_GRACE_SECONDS = 30


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
# Saturation tracker
# ---------------------------------------------------------------------------


class SaturationTracker:
    """EMA-based actuator saturation detector with grace periods.

    A saturation score of 1.0 means the actuator cannot follow its target
    (e.g. battery full/empty); 0.0 means it is tracking well.
    """

    def __init__(
        self,
        alpha: float,
        min_target: float,
        decay_factor: float,
        *,
        enabled: bool = True,
    ) -> None:
        self._enabled = enabled
        self._alpha = max(0.01, min(1.0, alpha))
        self._min_target = max(1, min_target)
        self._decay_factor = max(0.0, min(1.0, decay_factor))
        self._scores: dict[str, float] = {}
        self._grace_until: dict[str, float] = {}

    def update(
        self, consumer_id: str, last_target: float | None, actual: float
    ) -> None:
        """Update the saturation score for *consumer_id*."""
        if not self._enabled or last_target is None:
            return
        # Grace period handling
        grace_deadline = self._grace_until.get(consumer_id)
        if grace_deadline is not None:
            if time.time() < grace_deadline:
                if abs(actual) >= self._min_target:
                    del self._grace_until[consumer_id]
                else:
                    return
            else:
                del self._grace_until[consumer_id]
        target_abs = abs(last_target)
        if target_abs < self._min_target:
            prev = self._scores.get(consumer_id, 0.0)
            if prev > 0:
                decayed = prev * self._decay_factor
                if decayed < 0.001:
                    self._scores.pop(consumer_id, None)
                else:
                    self._scores[consumer_id] = decayed
            return
        inst_saturation = 1.0 if abs(actual) < self._min_target else 0.0
        prev = self._scores.get(consumer_id, 0.0)
        self._scores[consumer_id] = (
            self._alpha * inst_saturation + (1 - self._alpha) * prev
        )

    def get(self, consumer_id: str) -> float:
        return self._scores.get(consumer_id, 0.0)

    def set_grace(self, consumer_id: str, deadline: float) -> None:
        self._grace_until[consumer_id] = deadline

    def clear(self, consumer_id: str) -> None:
        self._scores.pop(consumer_id, None)
        self._grace_until.pop(consumer_id, None)

    def remove(self, consumer_id: str) -> None:
        self._scores.pop(consumer_id, None)
        self._grace_until.pop(consumer_id, None)


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
        *,
        saturation_enabled: bool = True,
    ) -> None:
        self._cfg = config
        self._saturation = SaturationTracker(
            alpha=saturation_alpha,
            enabled=saturation_enabled,
            min_target=saturation_min_target,
            decay_factor=saturation_decay_factor,
        )
        self._last_target: dict[str, float] = {}
        self._deprioritized: set[str] = set()
        self._priority: list[str] = []
        self._last_rotation: float = time.time()
        self._cache_sample: tuple | None = None
        self._cache_result: dict[str, float] | None = None
        self._fade_weights: dict[str, float] = {}

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
        last_target = self._last_target.get(consumer_id) if consumer_id else None
        if (
            consumer_id
            and consumer_id in active_reports
            and consumer_mode.mode != "manual"
        ):
            actual = parse_int(active_reports.get(consumer_id, {}).get("power", 0))
            self._saturation.update(consumer_id, last_target, actual)

        # --- Manual override ---
        if consumer_mode.mode == "manual" and consumer_id:
            reported = parse_int(active_reports.get(consumer_id, {}).get("power", 0))
            target = consumer_mode.manual_value - reported
            self._last_target[consumer_id] = target
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
        self._last_target.pop(consumer_id, None)
        self._saturation.remove(consumer_id)
        self._deprioritized.discard(consumer_id)
        self._fade_weights.pop(consumer_id, None)
        if consumer_id in self._priority:
            self._priority.remove(consumer_id)
            self._cache_sample = None
            self._cache_result = None

    def detach_from_auto_pool(self, consumer_id: str) -> None:
        """Remove from efficiency rotation (consumer switched to manual)."""
        self._deprioritized.discard(consumer_id)
        self._priority = [cid for cid in self._priority if cid != consumer_id]
        self._fade_weights.pop(consumer_id, None)
        self._cache_sample = None
        self._cache_result = None

    def reset_consumer(self, consumer_id: str) -> None:
        """Clear stale state and set a grace period.

        Called when a consumer transitions back to auto mode or resumes
        from inactive.
        """
        self._last_target.pop(consumer_id, None)
        self._saturation.clear(consumer_id)
        grace = time.time() + min(
            SATURATION_GRACE_SECONDS, self._cfg.efficiency_rotation_interval
        )
        self._saturation.set_grace(consumer_id, grace)

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
        self._cache_sample = None
        self._cache_result = None
        self._fade_weights.clear()
        logger.info(
            "Efficiency: forced rotation, new order: %s",
            [c[:16] for c in self._priority],
        )

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def get_saturation(self, consumer_id: str) -> float:
        return self._saturation.get(consumer_id)

    def get_last_target(self, consumer_id: str) -> float | None:
        return self._last_target.get(consumer_id)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _steer_to_zero(self, consumer_id: str | None, reports: dict) -> list[float]:
        """Drive a consumer's output to zero."""
        if consumer_id:
            self._last_target[consumer_id] = 0
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
        saturation = dict(self._saturation._scores)
        num_consumers = max(1, len(reports))
        eff_part = {cid: max(0.01, 1.0 - saturation.get(cid, 0.0)) for cid in reports}

        efficiency_adjustments = self._compute_efficiency_deprioritized(
            reports, sample_id, smoothed_target
        )
        faded_adjustments = self._fade_efficiency_weights(
            efficiency_adjustments, set(reports.keys())
        )
        any_fading = any(0.0 < w < 1.0 for w in faded_adjustments.values())

        # --- Fading path ---
        if any_fading and consumer_id:
            fade_w = self._fade_weights.get(consumer_id, 1.0)
            reported = parse_int(reports.get(consumer_id, {}).get("power", 0))
            if fade_w == 0.0:
                return self._steer_to_zero(consumer_id, reports)

            total_battery = sum(
                parse_int(reports.get(cid, {}).get("power", 0)) for cid in reports
            )
            demand = total_battery + smoothed_target
            total_fade = sum(self._fade_weights.get(cid, 1.0) for cid in reports)
            desired = demand * fade_w / total_fade if total_fade > 0 else 0.0
            target = desired - reported

            if consumer_id:
                self._last_target[consumer_id] = target

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
            self._last_target[consumer_id] = target

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
            self._deprioritized = set()
            self._cache_sample = None
            self._cache_result = None
            return {}

        now = time.time()

        # Rotation check BEFORE cache
        if (
            self._priority
            and now - self._last_rotation >= cfg.efficiency_rotation_interval
        ):
            self._last_rotation = now
            self._priority.append(self._priority.pop(0))
            self._cache_sample = None

        # Sync priority list with current active consumers
        current = set(reports)
        self._priority = [c for c in self._priority if c in current]
        grace = now + min(SATURATION_GRACE_SECONDS, cfg.efficiency_rotation_interval)
        for cid in sorted(current):
            if cid not in self._priority:
                self._priority.append(cid)
                self._saturation.set_grace(cid, grace)

        # Saturation swap check BEFORE cache
        if cfg.efficiency_saturation_threshold > 0 and self._cache_sample is not None:
            slots_est = len(self._priority) - len(self._deprioritized)
            for cid in self._priority[:slots_est]:
                if self._saturation.get(cid) >= cfg.efficiency_saturation_threshold:
                    self._cache_sample = None
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
            self._saturation.clear(cid)
            self._saturation.set_grace(cid, grace)

        if self._maybe_force_swap_saturated(self._priority, slots, now):
            deprioritized = set(self._priority[slots:])
            result = {cid: 0.0 for cid in deprioritized}
            cache_key = (sample_id, tuple(self._priority))
            for cid in set(self._priority[:slots]) - pre_swap_active:
                self._saturation.clear(cid)
                self._saturation.set_grace(cid, grace)

        for cid in deprioritized - self._deprioritized:
            self._saturation._grace_until.pop(cid, None)
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
            if self._saturation.get(priority[i]) >= threshold:
                saturated_idx = i
                break
        if saturated_idx is None:
            return False
        healthy_idx = None
        for i in range(slots, len(priority)):
            if self._saturation.get(priority[i]) < threshold:
                healthy_idx = i
                break
        if healthy_idx is None:
            return False
        logger.info(
            "Efficiency: %s cannot follow target (sat=%.2f), rotating to %s",
            priority[saturated_idx][:16],
            self._saturation.get(priority[saturated_idx]),
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
        for cid in consumer_ids:
            goal = raw_adjustments.get(cid, 1.0)
            prev = self._fade_weights.get(cid, 1.0)
            new = prev + alpha * (goal - prev)
            if abs(new - goal) < 0.05:
                new = goal
            self._fade_weights[cid] = new
            if new < 1.0:
                result[cid] = new
        self._fade_weights = {
            cid: w for cid, w in self._fade_weights.items() if cid in consumer_ids
        }
        return result
