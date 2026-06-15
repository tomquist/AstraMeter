from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, Literal, cast

from astrameter.config.logger import debug_traceback, logger
from astrameter.request_dedupe import RequestDeduplicator

from .balancer import (
    SATURATION_GRACE_SECONDS,
    SATURATION_STALL_TIMEOUT_SECONDS,
    BalancerConfig,
    ConsumerMode,
    LoadBalancer,
)
from .protocol import (
    ETX,
    RESPONSE_LABELS,
    SEPARATOR,
    SOH,
    STX,
    build_payload,
    calculate_checksum,
    compute_length,
    parse_int,
    parse_request,
)

# Re-export protocol symbols for backward compatibility
__all__ = [
    "CT002",
    "ETX",
    "RESPONSE_LABELS",
    "SEPARATOR",
    "SOH",
    "STX",
    "UDP_PORT",
    "ReportingConsumerRow",
    "ReportingPhase",
    "build_payload",
    "calculate_checksum",
    "compute_length",
    "parse_int",
    "parse_request",
]

UDP_PORT = 12345
CLEANUP_INTERVAL_SECONDS = 5
POLL_INTERVAL_EMA_ALPHA = 0.3

# Cross-talk aggregation buckets, mirroring the real CT (see
# docs/ct002-ct003-protocol.md): one per phase, plus ``x`` for
# unassigned/inspection ("0") reporters and ``ABC`` for combined-mode
# (phase "D") reporters.
PHASE_BUCKETS = ("x", "A", "B", "C", "ABC")

# Default eviction policy (``consumer_ttl=None``): the real CT clears a slave
# slot that missed roughly 1-2 of its own poll cycles, so by default a
# consumer expires after missing ~2 cycles of its observed cadence.  The floor
# keeps a transient EMA dip (e.g. a burst of retransmits) from evicting a live
# battery, and the fallback covers a consumer whose cadence is still unknown
# (only one poll seen).  Issue #462.
ADAPTIVE_TTL_POLL_MULTIPLIER = 2.0
ADAPTIVE_TTL_MIN_SECONDS = 5.0
ADAPTIVE_TTL_FALLBACK_SECONDS = 30.0


def _bucket_for_phase(phase: str) -> str:
    """Map a stored consumer phase to its aggregation bucket."""
    p = (phase or "").strip().upper()
    if p in ("A", "B", "C"):
        return p
    if p == "D":
        return "ABC"
    return "x"


# ---------------------------------------------------------------------------
# Per-consumer state
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Consumer:
    """Bundled per-consumer state owned by CT002."""

    consumer_id: str
    # Meter readings (set externally, e.g. by powermeter integration)
    values: list | None = None
    # Report data (updated each UDP request)
    phase: str = "A"
    power: int = 0
    # Net AC power we expect this consumer to be at after applying the
    # last instruction (its reported output plus the per-phase delta we
    # delivered).  Negative = charging, positive = discharging.  Used to
    # populate cross-talk *_dchrg / *_chrg fields in responses to other
    # batteries — see _collect_reports_by_phase.
    last_instructed_power: float = 0.0
    timestamp: float = 0.0
    device_type: str = ""
    poll_interval: float | None = None
    # "Participate" flag from the request's optional 7th field. ``0`` on the
    # wire means "do not aggregate me"; defaults to ``True`` when the field is
    # absent (older senders send only 6 fields).
    participates: bool = True
    # Control state (set by explicit API calls)
    manual_target: float = 0.0
    manual_enabled: bool = False
    active: bool = True
    # Relative weight for fair-share distribution across batteries.  1.0 is
    # neutral; a battery with weight 2.0 takes roughly twice the share of a
    # weight-1.0 battery.  Tuned live via the MQTT "Distribution Weight" entity.
    distribution_weight: float = 1.0
    # Per-device override (W) for the MIN_DC_OUTPUT wake floor; ``None`` inherits
    # the global setting.  Tuned live via the MQTT "Min DC Output" entity.
    min_dc_output: float | None = None
    # Last UDP source address seen for this consumer, if the protocol provides it.
    last_ip: str = ""


# Lowercase phase label carried on reporting rows: the three physical phases,
# ``d`` (combined / whole-home) and ``0`` (unassigned / inspection).
ReportingPhase = Literal["a", "b", "c", "d", "0"]


@dataclasses.dataclass(frozen=True, slots=True)
class ReportingConsumerRow:
    """One UDP-reporting consumer, for integrations that need a stable device list."""

    device_type: str
    consumer_id: str
    last_ip: str
    phase: ReportingPhase


class _CT002Protocol(asyncio.DatagramProtocol):
    def __init__(self, ct002: CT002):
        self.ct002 = ct002
        self._tasks: set[asyncio.Task] = set()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        task = asyncio.create_task(
            self.ct002._safe_handle_request(data, addr, self.transport)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


class CT002:
    def __init__(
        self,
        udp_port=UDP_PORT,
        ct_mac="",
        ct_type="HME-4",
        wifi_rssi=-50,
        dedupe_time_window=0.0,
        # None (default) = adaptive eviction: a consumer expires after missing
        # ~2 of its own poll cycles, like the real CT.  A number = fixed TTL
        # in seconds (the pre-#462 behavior; set CONSUMER_TTL to get this).
        consumer_ttl=None,
        debug_status=False,
        active_control=True,
        fair_distribution=True,
        balance_gain=0.2,
        error_boost_threshold=150,
        error_boost_max=0.5,
        error_reduce_threshold=20,
        balance_deadband=25,
        max_correction_per_step=80,
        max_target_step=0,
        pace_base_step=30,
        pace_max_step=100,
        osc_damp_max=0.95,
        osc_damp_alpha=0.3,
        osc_damp_decay=0.05,
        osc_damp_threshold=300,
        grid_predict_trust=0.5,
        saturation_detection=True,
        saturation_alpha=0.15,
        min_target_for_saturation=20,
        min_efficient_power=0,
        probe_min_power=80,
        efficiency_rotation_interval=900,
        efficiency_fade_alpha=0.15,
        efficiency_saturation_threshold=0.4,
        min_dc_output=0,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=SATURATION_GRACE_SECONDS,
        saturation_stall_timeout_seconds=SATURATION_STALL_TIMEOUT_SECONDS,
        device_id="",
        clock=None,
        reset_fn=None,
    ) -> None:
        self.udp_port = udp_port
        self.ct_mac = ct_mac
        self.ct_type = ct_type
        self.wifi_rssi = wifi_rssi
        self.dedupe_time_window = dedupe_time_window
        self.consumer_ttl = consumer_ttl
        self.debug_status = debug_status
        self.active_control = active_control
        self.before_send: (
            Callable[[tuple, list, str], Awaitable[list[float] | None]] | None
        ) = None
        self.event_listener: Callable[[str, str, dict[str, Any]], None] | None = None
        self._device_id = device_id
        self._consumers: dict[str, Consumer] = {}
        self._info_idx_counter = 0
        # Use wall-clock (time.time) so the dedup shares a timebase with
        # _cleanup_consumers' purge; RequestDeduplicator would otherwise
        # default to time.monotonic and mix timebases across the class.
        self._dedup: RequestDeduplicator[str] = RequestDeduplicator(
            dedupe_time_window, clock=clock or time.time
        )
        self._transport = None
        self._protocol: _CT002Protocol | None = None
        self._cleanup_task = None
        self._stopped = asyncio.Event()
        # Clock used for rate-limiting ``before_send`` warning logs.
        # Defaults to wall time but tests may inject a fake clock so
        # the rate-limit is deterministic under accelerated stepping.
        self._clock: Callable[[], float] = clock or time.time
        # Rate-limited warnings for powermeter (before_send) failures
        # — see _call_before_send.
        self._before_send_failure_count: int = 0
        self._before_send_last_warn: float = 0.0

        # Composed components
        self._last_smooth_target: float = 0.0
        self._balancer = LoadBalancer(
            config=BalancerConfig(
                fair_distribution=fair_distribution,
                balance_gain=balance_gain,
                balance_deadband=balance_deadband,
                error_boost_threshold=error_boost_threshold,
                error_boost_max=error_boost_max,
                error_reduce_threshold=error_reduce_threshold,
                max_correction_per_step=max_correction_per_step,
                max_target_step=max_target_step,
                pace_base_step=pace_base_step,
                pace_max_step=pace_max_step,
                osc_damp_max=osc_damp_max,
                osc_damp_alpha=osc_damp_alpha,
                osc_damp_decay=osc_damp_decay,
                osc_damp_threshold=osc_damp_threshold,
                grid_predict_trust=grid_predict_trust,
                min_efficient_power=min_efficient_power,
                probe_min_power=probe_min_power,
                efficiency_rotation_interval=efficiency_rotation_interval,
                efficiency_fade_alpha=efficiency_fade_alpha,
                efficiency_saturation_threshold=efficiency_saturation_threshold,
                min_dc_output=min_dc_output,
            ),
            saturation_alpha=saturation_alpha,
            saturation_min_target=min_target_for_saturation,
            saturation_decay_factor=saturation_decay_factor,
            saturation_grace_seconds=saturation_grace_seconds,
            saturation_stall_timeout_seconds=saturation_stall_timeout_seconds,
            saturation_enabled=saturation_detection,
            clock=clock,
            reset_fn=reset_fn,
        )

    def _consumer_key(self, addr, fields):
        battery_mac = fields[1] if len(fields) > 1 else ""
        if battery_mac:
            return battery_mac.lower()
        return f"{addr[0]}:{addr[1]}"

    def _get_consumer(self, consumer_id: str) -> Consumer:
        consumer = self._consumers.get(consumer_id)
        if consumer is None:
            consumer = Consumer(consumer_id=consumer_id)
            self._consumers[consumer_id] = consumer
        return consumer

    def set_consumer_value(self, consumer_id, values):
        self._get_consumer(consumer_id).values = values

    def _get_consumer_value(self, consumer_id):
        consumer = self._consumers.get(consumer_id)
        return consumer.values if consumer else None

    def set_consumer_manual_target(self, consumer_id: str, target: float) -> None:
        value = float(target)
        if not math.isfinite(value):
            msg = f"manual target must be finite, got {target!r}"
            raise ValueError(msg)
        self._get_consumer(consumer_id).manual_target = value

    def set_consumer_distribution_weight(self, consumer_id: str, weight: float) -> None:
        """Set the relative fair-share weight for a battery.

        Must be finite and within ``0 <= weight <= 10``.  1.0 is neutral; 0.0
        means the battery takes no share (parked at 0 W while staying in the
        pool).
        """
        value = float(weight)
        if not math.isfinite(value) or not (0.0 <= value <= 10.0):
            msg = f"distribution weight must be in [0, 10], got {weight!r}"
            raise ValueError(msg)
        self._get_consumer(consumer_id).distribution_weight = value

    def set_consumer_min_dc_output(self, consumer_id: str, value: float) -> None:
        """Set the per-device MIN_DC_OUTPUT floor (W) for a battery.

        Must be finite and ``>= 0``.  Overrides the global ``MIN_DC_OUTPUT`` for
        this battery regardless of its type; ``0`` disables the floor for it.
        """
        v = float(value)
        if not math.isfinite(v) or v < 0.0:
            msg = f"min_dc_output must be finite and >= 0, got {value!r}"
            raise ValueError(msg)
        self._get_consumer(consumer_id).min_dc_output = v

    def set_consumer_auto_target(self, consumer_id: str, auto: bool) -> None:
        """Toggle auto target. auto=True means automatic control (default).
        auto=False means use manual target override."""
        consumer = self._get_consumer(consumer_id)
        if auto:
            was_manual = consumer.manual_enabled
            consumer.manual_enabled = False
            if was_manual:
                self._balancer.reset_consumer(consumer_id)
        else:
            consumer.manual_enabled = True
            self._balancer.detach_from_auto_pool(consumer_id)

    def force_efficiency_rotation(self) -> None:
        current = {
            cid
            for cid, c in self._consumers.items()
            if c.timestamp > 0 and c.active and not c.manual_enabled
        }
        self._balancer.force_rotation(current)

    def set_active_control(self, active: bool) -> None:
        """Toggle device-level active control (on = emulator computes targets,
        off = relay mode forwarding consumer aggregates). Surfaced as the
        device's "Active Control" switch in Home Assistant; defaults on."""
        if self.active_control == active:
            return
        self.active_control = active
        logger.info(
            "Active control %s for %s",
            "enabled" if active else "disabled (relay mode)",
            self._device_id or "(default)",
        )

    def set_consumer_active(self, consumer_id: str, active: bool) -> None:
        consumer = self._get_consumer(consumer_id)
        if active:
            consumer.active = True
            self._balancer.reset_consumer(consumer_id)
        else:
            consumer.active = False

    def is_consumer_active(self, consumer_id: str) -> bool:
        consumer = self._consumers.get(consumer_id)
        return consumer.active if consumer else True

    def _call_event_listener(self, consumer_id: str, data: dict[str, Any]) -> None:
        if not self.event_listener:
            return
        try:
            self.event_listener(self._device_id, consumer_id, data)
        except Exception as exc:
            logger.warning(
                "event_listener failed for %s: %s", consumer_id, exc, exc_info=True
            )

    def _update_consumer_report(
        self,
        consumer_id,
        phase,
        power,
        device_type="",
        *,
        source_ip: str | None = None,
        participates: bool = True,
    ):
        normalized_phase = str(phase).strip().upper() if phase else ""
        if normalized_phase not in ("A", "B", "C", "D"):
            # Anything else ("0", empty, future markers) is the
            # unassigned/inspection state; store the wire's canonical "0" so
            # aggregation routes it to the x bucket instead of inventing a
            # phase (issue #460).
            normalized_phase = "0"
        consumer = self._get_consumer(consumer_id)
        previous_phase = consumer.phase if consumer.timestamp > 0 else None
        now = self._clock()
        if consumer.timestamp > 0:
            raw_interval = now - consumer.timestamp
            if consumer.poll_interval is None:
                consumer.poll_interval = round(raw_interval, 1)
            else:
                consumer.poll_interval = round(
                    POLL_INTERVAL_EMA_ALPHA * raw_interval
                    + (1 - POLL_INTERVAL_EMA_ALPHA) * consumer.poll_interval,
                    1,
                )
        consumer.phase = normalized_phase
        consumer.power = parse_int(power, 0)
        consumer.timestamp = now
        consumer.device_type = device_type
        consumer.participates = participates
        if source_ip:
            consumer.last_ip = source_ip

        if (
            normalized_phase in ("A", "B", "C", "D")
            and previous_phase != normalized_phase
        ):
            if previous_phase in ("A", "B", "C", "D"):
                logger.info(
                    "CT002 consumer %s phase changed: %s -> %s",
                    consumer_id,
                    previous_phase,
                    normalized_phase,
                )
            else:
                logger.info(
                    "CT002 consumer %s phase detected: %s",
                    consumer_id,
                    normalized_phase,
                )

    def _consumer_ttl_seconds(self, consumer: Consumer) -> float:
        """Seconds of silence after which *consumer* counts as gone.

        A configured ``consumer_ttl`` is used verbatim; otherwise the TTL
        adapts to the consumer's observed poll cadence (~2 missed cycles,
        like the real CT — see the ADAPTIVE_TTL_* constants).
        """
        if self.consumer_ttl is not None:
            return float(self.consumer_ttl)
        if consumer.poll_interval is None:
            return ADAPTIVE_TTL_FALLBACK_SECONDS
        return max(
            ADAPTIVE_TTL_MIN_SECONDS,
            ADAPTIVE_TTL_POLL_MULTIPLIER * consumer.poll_interval,
        )

    def _consumer_expired(self, consumer: Consumer, now: float) -> bool:
        return (
            consumer.timestamp > 0
            and now - consumer.timestamp > self._consumer_ttl_seconds(consumer)
        )

    def _cleanup_consumers(self):
        now = self._clock()
        stale = [
            key
            for key, consumer in self._consumers.items()
            if self._consumer_expired(consumer, now)
        ]
        for key in stale:
            self._call_event_listener(key, {"_removed": True})
            del self._consumers[key]
            self._balancer.remove_consumer(key)
        # Dedup entries only matter within the dedupe window; with an adaptive
        # TTL there is no single number, so purge on a horizon that is safely
        # past any per-consumer TTL and the dedupe window itself.
        purge_horizon = (
            float(self.consumer_ttl)
            if self.consumer_ttl is not None
            else max(ADAPTIVE_TTL_FALLBACK_SECONDS, self.dedupe_time_window)
        )
        self._dedup.purge_older_than(purge_horizon)

    def _consumer_mode(self, consumer_id: str | None) -> ConsumerMode:
        if not consumer_id:
            return ConsumerMode("auto")
        consumer = self._consumers.get(consumer_id)
        if consumer is None:
            return ConsumerMode("auto")
        # A consumer that opted out via the "participate" flag is treated as
        # inactive (not driven by active control).
        if not consumer.active or not consumer.participates:
            return ConsumerMode("inactive")
        if consumer.manual_enabled:
            return ConsumerMode("manual", consumer.manual_target)
        return ConsumerMode("auto")

    def _compute_smooth_target(self, values, consumer_id=None):
        """Active control: smooth the raw grid reading and delegate
        target allocation to the load balancer."""
        if not self.active_control or not values:
            return values

        total = sum(parse_int(v, 0) for v in values)
        self._last_smooth_target = total
        sample_id = tuple(values)
        mode = self._consumer_mode(consumer_id)

        reports = {
            cid: {
                "phase": c.phase,
                "power": c.power,
                "device_type": c.device_type,
                "weight": c.distribution_weight,
                "min_dc_output": c.min_dc_output,
            }
            for cid, c in self._consumers.items()
            if c.timestamp > 0
        }
        # A consumer that opted out via the request's "participate" flag is
        # treated as inactive: active control excludes it from the distribution
        # pool (it isn't driven), mirroring the aggregation exclusion above.
        inactive = frozenset(
            cid
            for cid, c in self._consumers.items()
            if not c.active or not c.participates
        )
        manual = frozenset(
            cid for cid, c in self._consumers.items() if c.manual_enabled
        )

        return self._balancer.compute_target(
            consumer_id,
            mode,
            reports,
            total,
            inactive,
            manual,
            sample_id,
        )

    def _collect_reports_by_phase(self):
        by_phase = {
            bucket: {"chrg_power": 0, "dchrg_power": 0, "active": False, "count": 0}
            for bucket in PHASE_BUCKETS
        }

        now = self._clock()
        for consumer in self._consumers.values():
            if consumer.timestamp <= 0:
                continue
            # Respect the request's "participate" flag: a battery that opted out
            # (7th field == 0) is not aggregated into the per-phase buckets or
            # the forwarded count.
            if not consumer.participates:
                continue
            # The real CT clears a slot that missed ~1-2 poll cycles before
            # aggregating, so a battery that drops off the network stops being
            # counted almost immediately.  Mirror that per response here; the
            # cleanup loop removes the entry shortly after (issue #462).
            if self._consumer_expired(consumer, now):
                continue
            bucket = _bucket_for_phase(consumer.phase)
            # Count every battery reporting into the bucket (regardless of its
            # current power) so relay mode can forward the real per-phase
            # battery count — each battery divides the forwarded aggregate by it
            # to take its 1/N share.
            by_phase[bucket]["count"] += 1
            if self.active_control and bucket in ("A", "B", "C"):
                # Active control: use the net AC power we *instructed* this
                # consumer to be at (its reported output plus the delta in the
                # last response), not what it physically reported.  A battery
                # passing PV through to AC at 100% SoC reports positive power
                # even though we told it to charge; reporting the instructed
                # net power keeps the cross-talk dchrg signal free of those
                # involuntary outputs (issue #376).
                power = round(consumer.last_instructed_power)
                # With ramp pacing the per-poll delta is capped, so the
                # instructed net power can keep the sign of the battery's
                # involuntary output for many polls while the *control
                # intent* points the other way (the issue #376 scenario:
                # full battery passing PV through while told to charge).
                # Filter by the balancer's recorded unpaced intent.
                intent = self._balancer.get_last_intent(consumer.consumer_id)
                if intent is not None and (
                    (intent <= 0 and power > 0) or (intent >= 0 and power < 0)
                ):
                    power = 0
            else:
                # Relay mode forwards each battery's *reported* power, exactly
                # like the real CT (issue #457).  x/ABC consumers are never
                # actively instructed (the emulator has no combined control
                # mode and gives no instruction during inspection), so their
                # reported power is the only truthful signal in either mode.
                power = consumer.power
            if power == 0:
                continue
            by_phase[bucket]["active"] = True
            if power < 0:
                by_phase[bucket]["chrg_power"] += power
            else:
                by_phase[bucket]["dchrg_power"] += power
        return by_phase

    def reporting_consumer_count(self) -> int:
        """Number of consumers that have reported at least once over UDP."""
        return sum(1 for c in self._consumers.values() if c.timestamp > 0)

    def reporting_consumer_rows(self) -> tuple[ReportingConsumerRow, ...]:
        """Stable-ordered view of reporting consumers for integrations.

        *phase* is normalized to ``a``/``b``/``c``/``d``/``0`` — the canonical
        phase char the battery reported (``d`` = combined, ``0`` = unassigned/
        inspection), matching what the ESPHome mirror and a real CT's ``cd=4``
        slave list carry; *last_ip* may be empty when unknown.  Rows follow
        sorted ``consumer_id`` so list position stays predictable.
        """
        reporters = sorted(
            (c for c in self._consumers.values() if c.timestamp > 0),
            key=lambda c: c.consumer_id,
        )
        out: list[ReportingConsumerRow] = []
        for c in reporters:
            pu = (c.phase or "0").strip().lower()
            if pu not in ("a", "b", "c", "d"):
                pu = "0"
            host = c.last_ip.strip() if c.last_ip else ""
            out.append(
                ReportingConsumerRow(
                    device_type=(c.device_type or "").strip(),
                    consumer_id=c.consumer_id.strip(),
                    last_ip=host,
                    phase=cast(ReportingPhase, pu),
                )
            )
        return tuple(out)

    def _format_status(self, values, phase_values, consumer_id=None, meter_value=None):
        """Concise one-line status: phase consumption and consumer charge/discharge reports."""
        if not values or len(values) != 3:
            values = [0, 0, 0]
        parts = []
        if consumer_id is not None:
            parts.append(
                f"consumer {consumer_id[:16]}" if consumer_id else "consumer -"
            )
        if meter_value is not None:
            parts.append(f"meter {meter_value}W")
        phases = " ".join(f"{p}:{int(v)}W" for p, v in zip("ABC", values, strict=False))
        chrg = " ".join(f"{p}:{phase_values[p]['chrg_power']}" for p in PHASE_BUCKETS)
        dchrg = " ".join(f"{p}:{phase_values[p]['dchrg_power']}" for p in PHASE_BUCKETS)
        consumers_with_reports = sorted(
            ((c.consumer_id, c) for c in self._consumers.values() if c.timestamp > 0),
            key=lambda x: x[0],
        )
        consumers = (
            " ".join(
                f"{cid[:8]}@{c.phase}:{c.power}" for cid, c in consumers_with_reports
            )
            or "none"
        )
        parts.extend(
            [
                f"phases {phases}",
                f"chrg {chrg}",
                f"dchrg {dchrg}",
                f"consumers {consumers}",
            ]
        )
        return " | ".join(parts)

    def _build_response_fields(self, request_fields, values):
        if not values or len(values) != 3:
            values = [0, 0, 0]
        phase_a, phase_b, phase_c = values
        measured_total_power = phase_a + phase_b + phase_c
        meter_dev_type = request_fields[0] if len(request_fields) > 0 else "HMG-50"
        meter_mac = request_fields[1] if len(request_fields) > 1 else ""
        ct_type = self.ct_type
        ct_mac = (
            self.ct_mac
            if self.ct_mac
            else (request_fields[3] if len(request_fields) > 3 else "")
        )
        response_fields = [
            ct_type,
            ct_mac,
            meter_dev_type,
            meter_mac,
            str(round(phase_a)),
            str(round(phase_b)),
            str(round(phase_c)),
            str(round(measured_total_power)),
            "0",
            "0",
            "0",
            "0",  # A/B/C/ABC_chrg_nb
            str(self.wifi_rssi),
            str(self._info_idx_counter),
            "0",
            "0",
            "0",
            "0",
            "0",  # x/A/B/C/ABC_chrg_power
            "0",
            "0",
            "0",
            "0",
            "0",  # x/A/B/C/ABC_dchrg_power
        ]

        phase_values = self._collect_reports_by_phase()
        phase_power = [phase_a, phase_b, phase_c]
        for phase, idx in (("A", 0), ("B", 1), ("C", 2)):
            pv = phase_values[phase]
            if self.active_control:
                # Active control distributes a per-consumer target, so each
                # battery should apply it as-is (not divide): report a count of
                # 1 when the phase is active, 0 otherwise.
                #
                # Deliberately NOT the real per-phase count (issue #459): the
                # battery firmware divides the grid value it reads by this
                # count (the relay-mode share-split, g / nb).  Our active
                # control already did the distribution — the value in the
                # phase-power field is this battery's *individual* target — so
                # a real count N would make every battery under-respond by a
                # factor of N.  The issue #455 relay-count fix applies to the
                # relay branch below only; don't generalize it here.
                if pv["active"] or phase_power[idx] != 0:
                    response_fields[8 + idx] = "1"
            else:
                # Relay mode forwards the per-phase aggregate; report the real
                # battery count so each battery takes its 1/N share.
                response_fields[8 + idx] = str(pv["count"])
            response_fields[15 + idx] = str(pv["chrg_power"])
            response_fields[20 + idx] = str(pv["dchrg_power"])

        # x (unassigned/inspection) bucket — chrg/dchrg only; the response
        # carries no x count field.
        response_fields[14] = str(phase_values["x"]["chrg_power"])
        response_fields[19] = str(phase_values["x"]["dchrg_power"])
        # ABC (combined, phase "D") bucket.  Combined-mode consumers are never
        # actively instructed (the emulator has no combined control mode), so
        # they are effectively relayed in both modes: forward the real count.
        response_fields[11] = str(phase_values["ABC"]["count"])
        response_fields[18] = str(phase_values["ABC"]["chrg_power"])
        response_fields[23] = str(phase_values["ABC"]["dchrg_power"])

        response_fields += ["0"] * (len(RESPONSE_LABELS) - len(response_fields))
        self._info_idx_counter = (self._info_idx_counter + 1) % 256
        return response_fields

    async def _call_before_send(self, addr, fields, consumer_id):
        """Invoke the ``before_send`` powermeter hook.

        Returns ``(result, failed)``.  ``failed`` is ``True`` only when the
        hook *raised* (the powermeter is unavailable); the caller uses it to
        send a zero-adjustment "hold" instead of re-driving control from a
        stale cached reading.  A hook that simply returns ``None`` (e.g. no
        powermeter matches this client) is *not* a failure.
        """
        if not self.before_send:
            return None, False
        try:
            result = await self.before_send(addr, fields, consumer_id)
        except Exception as exc:
            # Rate-limit: log loudly on the first failure after a
            # healthy spell, then at most once every 30 s while the
            # failure persists.  The CT002 UDP server sees every
            # battery request, so logging on every failure would flood
            # the log with hundreds of lines per minute during a meter
            # outage.  We use ``self._clock`` (not wall time) so that
            # deterministic test harnesses with a ``_FakeClock`` see
            # the same rate-limit behaviour as production.
            self._before_send_failure_count += 1
            now = self._clock()
            if (
                self._before_send_failure_count == 1
                or now - self._before_send_last_warn >= 30.0
            ):
                logger.warning(
                    "CT002 before_send failed (%d in a row) for %s: %s. "
                    "The CT002 emulator is sending a zero adjustment so "
                    "batteries hold their current output until the "
                    "powermeter recovers.",
                    self._before_send_failure_count,
                    addr,
                    exc,
                    exc_info=debug_traceback(),
                )
                self._before_send_last_warn = now
            return None, True
        # Success path: if we were in a failure spell, log the recovery.
        if self._before_send_failure_count > 0:
            logger.info(
                "CT002 before_send recovered after %d consecutive failures",
                self._before_send_failure_count,
            )
            self._before_send_failure_count = 0
            self._before_send_last_warn = 0.0
        return result, False

    def _validate_ct_mac(self, request_fields):
        if not self.ct_mac:
            return True
        if len(request_fields) < 4:
            return False
        req_ct_mac = request_fields[3]
        if not req_ct_mac:
            return False
        return req_ct_mac.lower() == self.ct_mac.lower()

    async def _safe_handle_request(self, data, addr, transport):
        try:
            await self._handle_request(data, addr, transport)
        except Exception:
            logger.exception("Error handling CT002 request from %s", addr)

    async def _handle_request(self, data, addr, transport):
        logger.debug("CT002 request from %s: %s", addr, data.hex())
        fields, error = parse_request(data)
        if error:
            logger.debug("Invalid CT002 request from %s: %s", addr, error)
            return
        if len(fields) < 4:
            logger.debug("CT002 request from %s missing required fields", addr)
            return
        if not self._validate_ct_mac(fields):
            logger.debug(
                "Ignoring CT002 request from %s due to CT MAC mismatch (req=%s, cfg=%s)",
                addr,
                fields[3] if len(fields) > 3 else None,
                self.ct_mac,
            )
            return
        consumer_id = self._consumer_key(addr, fields)
        reported_phase = (fields[4] if len(fields) > 4 else "").strip().upper()
        reported_power = parse_int(fields[5] if len(fields) > 5 else 0)
        # Optional 7th field: "participate" flag (newer senders, e.g. B2500).
        # Absent/empty defaults to participating; an explicit 0 opts out of
        # aggregation.
        participate_raw = fields[6].strip() if len(fields) > 6 else ""
        participates = participate_raw == "" or parse_int(participate_raw, 1) != 0

        # Anything other than A/B/C is treated as inspection mode. Observed
        # inspection markers in real traffic include "0", empty, and "D"
        # (newer Marstek battery firmwares); accept any other value too so
        # future markers don't break phase detection.
        in_inspection_mode = reported_phase not in ("A", "B", "C")
        if in_inspection_mode:
            logger.debug(
                "CT002 request from %s in inspection mode (phase=%r)",
                addr,
                reported_phase,
            )

        logger.debug(
            "CT002 parsed fields from %s: meter_dev_type=%s meter_mac=%s ct_type=%s ct_mac=%s phase=%r power=%s consumer_id=%s%s",
            addr,
            fields[0] if len(fields) > 0 else None,
            fields[1] if len(fields) > 1 else None,
            fields[2] if len(fields) > 2 else None,
            fields[3] if len(fields) > 3 else None,
            reported_phase,
            reported_power,
            consumer_id,
            " in inspection mode" if in_inspection_mode else "",
        )

        # Deduplication check (keyed by consumer id so repeats from the
        # same battery are suppressed regardless of source UDP port).
        if not self._dedup.should_process(consumer_id):
            logger.debug(
                "Ignoring request from %s (consumer=%s) due to dedupe window",
                addr,
                consumer_id,
            )
            return

        meter_dev_type = fields[0] if len(fields) > 0 else ""
        # Store the phase exactly as reported: "D" selects the combined ABC
        # bucket and any inspection marker is normalized to "0" (the x bucket)
        # by _update_consumer_report — forcing "A" here would mis-count
        # inspection and combined reporters into phase A (issue #460).
        self._update_consumer_report(
            consumer_id,
            phase=reported_phase,
            power=reported_power,
            device_type=meter_dev_type,
            source_ip=str(addr[0]),
            participates=participates,
        )

        updated, meter_failed = await self._call_before_send(addr, fields, consumer_id)
        if updated is not None:
            self.set_consumer_value(consumer_id, updated)

        if meter_failed:
            # Powermeter unavailable: do NOT re-drive control from the stale
            # cached reading.  The CT002 instruction is a delta
            # (``new_target = current_power + grid_field``), so re-issuing a
            # delta derived from a frozen reading winds the battery up in
            # active control, and feeds frozen per-phase values into a phase
            # self-diagnosis in inspection mode (issue #403).  Send a zero
            # adjustment instead so each battery holds its current output —
            # matching the ESPHome component, which uses ``[0, 0, 0]`` when
            # its sensor ages out (see esphome/components/ct002/ct002.cpp).
            values = [0, 0, 0]
        else:
            values = self._get_consumer_value(consumer_id)
            if values is None:
                values = [0, 0, 0]
        raw_values = ([*list(values), 0, 0, 0])[:3]
        meter_value = sum(parse_int(v, 0) for v in raw_values)
        is_active = self.is_consumer_active(consumer_id)
        # On a meter failure the ``[0, 0, 0]`` above is a *sentinel*, not a real
        # reading: run active control only when the meter is healthy.  Feeding
        # the sentinel through the balancer would let the stateful controller
        # (the grid-state predictor, saturation EMA, ...) treat a fabricated
        # zero grid as a fresh sample and emit a non-zero delta from its
        # internal state — exactly the wind-up issue #403 guards against — so
        # the battery must instead hold on the literal zero adjustment.
        if self.active_control and not in_inspection_mode and not meter_failed:
            values = self._compute_smooth_target(values, consumer_id)
        values = ([*list(values), 0, 0, 0])[:3]

        # Record the *net* power we expect this battery to be at after
        # applying the instruction (its reported output plus the delta we
        # deliver — the battery's firmware computes
        # ``new_target = current_power + grid_reading_field``).  In active
        # control the cross-talk *_chrg_power / *_dchrg_power fields convey
        # this net power per phase so other batteries can see who is actively
        # charging/discharging cells; storing only the delta would lose
        # the steady-state signal and flip signs on small corrections
        # (issue #376).  In relay mode the buckets forward the *reported*
        # power instead, like the real CT (issue #457) — this value is then
        # only a diagnostic.  Skip during inspection mode: there is no
        # instruction to record (we send raw meter readings as information,
        # not a target; the battery is running its phase-discovery routine,
        # not our integral controller); its reported power is aggregated
        # into the x bucket from ``consumer.power`` instead.
        if not in_inspection_mode:
            consumer = self._get_consumer(consumer_id)
            phase_idx = {"A": 0, "B": 1, "C": 2}.get(consumer.phase.upper(), 0)
            consumer.last_instructed_power = float(reported_power + values[phase_idx])

        try:
            response_fields = self._build_response_fields(fields, values)
            response = build_payload(response_fields)
        except Exception as exc:
            logger.warning(
                "Failed to build CT002 response for %s (%s): %s",
                addr,
                fields,
                exc,
                exc_info=True,
            )
            return
        logger.debug(
            "CT002 response to %s: %s (fields=%s)",
            addr,
            response.hex(),
            response_fields,
        )
        if self.debug_status:
            phase_values = self._collect_reports_by_phase()
            logger.info(
                "CT002 status: %s",
                self._format_status(values, phase_values, consumer_id, meter_value),
            )
        transport.sendto(response, addr)

        # Fire event listener after response is sent
        if not in_inspection_mode:
            consumer = self._consumers.get(consumer_id)
            self._call_event_listener(
                consumer_id,
                {
                    "grid_power": {
                        "l1": float(raw_values[0]),
                        "l2": float(raw_values[1]),
                        "l3": float(raw_values[2]),
                        "total": sum(float(v) for v in raw_values),
                    },
                    "target": {
                        "l1": float(values[0]),
                        "l2": float(values[1]),
                        "l3": float(values[2]),
                    },
                    "phase": consumer.phase if consumer else reported_phase,
                    "reported_power": reported_power,
                    "device_type": consumer.device_type if consumer else "",
                    "battery_ip": addr[0],
                    "ct_type": fields[2] if len(fields) > 2 else "",
                    "ct_mac": fields[3] if len(fields) > 3 else "",
                    "saturation": self._balancer.get_saturation(consumer_id),
                    "last_target": self._balancer.get_last_target(consumer_id),
                    "active": is_active,
                    "poll_interval": consumer.poll_interval if consumer else None,
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "smooth_target": self._last_smooth_target,
                    "manual_target": consumer.manual_target if consumer else None,
                    "auto_target": not consumer.manual_enabled if consumer else True,
                    "distribution_weight": (
                        consumer.distribution_weight if consumer else 1.0
                    ),
                    "min_dc_output": consumer.min_dc_output if consumer else None,
                    "active_control": self.active_control,
                    "consumer_count": sum(
                        1 for c in self._consumers.values() if c.timestamp > 0
                    ),
                },
            )

    async def _cleanup_loop(self):
        try:
            while True:
                await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
                self._cleanup_consumers()
        except asyncio.CancelledError:
            pass

    async def start(self):
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _CT002Protocol(self),
            local_addr=("0.0.0.0", self.udp_port),
        )
        self._transport = transport
        self._protocol = protocol
        self._stopped.clear()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("CT002 UDP server listening on port %s", self.udp_port)

    async def wait(self):
        await self._stopped.wait()

    async def stop(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
            self._cleanup_task = None
        if self._transport:
            self._transport.close()
            self._transport = None
        if self._protocol:
            for task in list(self._protocol._tasks):
                task.cancel()
            await asyncio.gather(*self._protocol._tasks, return_exceptions=True)
        self._protocol = None
        self._stopped.set()
