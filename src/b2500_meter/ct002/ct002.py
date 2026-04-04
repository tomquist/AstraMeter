from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import math
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from b2500_meter.config.logger import logger

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
from .smoother import TargetSmoother

# Re-export protocol symbols for backward compatibility
__all__ = [
    "CT002",
    "ETX",
    "RESPONSE_LABELS",
    "SEPARATOR",
    "SOH",
    "STX",
    "UDP_PORT",
    "build_payload",
    "calculate_checksum",
    "compute_length",
    "parse_int",
    "parse_request",
]

UDP_PORT = 12345
CLEANUP_INTERVAL_SECONDS = 5


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
    timestamp: float = 0.0
    device_type: str = ""
    # Control state (set by explicit API calls)
    manual_target: float = 0.0
    manual_enabled: bool = False
    active: bool = True


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
        dedupe_time_window=0,
        consumer_ttl=120,
        debug_status=False,
        active_control=True,
        smooth_target_alpha=0.9,
        max_smooth_step=0,
        fair_distribution=True,
        balance_gain=0.2,
        error_boost_threshold=150,
        error_boost_max=0.5,
        error_reduce_threshold=20,
        balance_deadband=15,
        deadband=20,
        max_correction_per_step=80,
        max_target_step=0,
        saturation_detection=True,
        saturation_alpha=0.15,
        min_target_for_saturation=20,
        min_efficient_power=0,
        probe_min_power=80,
        efficiency_rotation_interval=900,
        efficiency_fade_alpha=0.15,
        efficiency_saturation_threshold=0.4,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=SATURATION_GRACE_SECONDS,
        saturation_stall_timeout_seconds=SATURATION_STALL_TIMEOUT_SECONDS,
        device_id="",
    ):
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
        self._last_response_time: dict[tuple, float] = {}
        self._transport = None
        self._protocol: _CT002Protocol | None = None
        self._cleanup_task = None
        self._stopped = asyncio.Event()

        # Composed components
        self._smoother = TargetSmoother(
            alpha=max(0.01, min(1.0, smooth_target_alpha)),
            max_step=max(0, max_smooth_step),
            deadband=max(0, deadband),
        )
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
                deadband=deadband,
                min_efficient_power=min_efficient_power,
                probe_min_power=probe_min_power,
                efficiency_rotation_interval=efficiency_rotation_interval,
                efficiency_fade_alpha=efficiency_fade_alpha,
                efficiency_saturation_threshold=efficiency_saturation_threshold,
            ),
            saturation_alpha=saturation_alpha,
            saturation_min_target=min_target_for_saturation,
            saturation_decay_factor=saturation_decay_factor,
            saturation_grace_seconds=saturation_grace_seconds,
            saturation_stall_timeout_seconds=saturation_stall_timeout_seconds,
            saturation_enabled=saturation_detection,
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
            logger.warning("event_listener failed for %s: %s", consumer_id, exc)

    def _update_consumer_report(self, consumer_id, phase, power, device_type=""):
        normalized_phase = str(phase).upper() if phase else "A"
        consumer = self._get_consumer(consumer_id)
        previous_phase = consumer.phase if consumer.timestamp > 0 else None
        consumer.phase = normalized_phase
        consumer.power = parse_int(power, 0)
        consumer.timestamp = time.time()
        consumer.device_type = device_type

        if normalized_phase in ("A", "B", "C") and previous_phase != normalized_phase:
            if previous_phase in ("A", "B", "C"):
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

    def _cleanup_consumers(self):
        now = time.time()
        stale = [
            key
            for key, consumer in self._consumers.items()
            if consumer.timestamp > 0 and now - consumer.timestamp > self.consumer_ttl
        ]
        for key in stale:
            self._call_event_listener(key, {"_removed": True})
            del self._consumers[key]
            self._balancer.remove_consumer(key)
        stale_addrs = [
            addr
            for addr, ts in self._last_response_time.items()
            if now - ts > self.dedupe_time_window
        ]
        for addr in stale_addrs:
            self._last_response_time.pop(addr, None)

    def _consumer_mode(self, consumer_id: str | None) -> ConsumerMode:
        if not consumer_id:
            return ConsumerMode("auto")
        consumer = self._consumers.get(consumer_id)
        if consumer is None:
            return ConsumerMode("auto")
        if not consumer.active:
            return ConsumerMode("inactive")
        if consumer.manual_enabled:
            return ConsumerMode("manual", consumer.manual_target)
        return ConsumerMode("auto")

    def _compute_smooth_target(self, values, consumer_id=None):
        """Active control: smooth the raw grid reading and delegate
        target allocation to the load balancer."""
        if not self.active_control or not values or len(values) != 3:
            return values

        raw_total = sum(parse_int(v, 0) for v in values)
        sample_id = tuple(values)
        smoothed = self._smoother.update(raw_total, sample_id)
        mode = self._consumer_mode(consumer_id)

        reports = {
            cid: {"phase": c.phase, "power": c.power}
            for cid, c in self._consumers.items()
            if c.timestamp > 0
        }
        inactive = frozenset(cid for cid, c in self._consumers.items() if not c.active)
        manual = frozenset(
            cid for cid, c in self._consumers.items() if c.manual_enabled
        )

        return self._balancer.compute_target(
            consumer_id,
            mode,
            reports,
            smoothed,
            raw_total,
            inactive,
            manual,
            sample_id,
        )

    def _collect_reports_by_phase(self):
        by_phase = {
            "A": {"chrg_power": 0, "dchrg_power": 0, "active": False},
            "B": {"chrg_power": 0, "dchrg_power": 0, "active": False},
            "C": {"chrg_power": 0, "dchrg_power": 0, "active": False},
        }

        for consumer in self._consumers.values():
            if consumer.timestamp <= 0:
                continue
            phase = consumer.phase.upper()
            if phase not in by_phase:
                phase = "A"
            power = consumer.power
            if power == 0:
                continue
            by_phase[phase]["active"] = True
            if power < 0:
                by_phase[phase]["chrg_power"] += power
            else:
                by_phase[phase]["dchrg_power"] += power
        return by_phase

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
        chrg = " ".join(f"{p}:{phase_values[p]['chrg_power']}" for p in "ABC")
        dchrg = " ".join(f"{p}:{phase_values[p]['dchrg_power']}" for p in "ABC")
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
        for phase, idx in (("A", 0), ("B", 1), ("C", 2)):
            if phase_values[phase]["active"]:
                response_fields[8 + idx] = "1"
            response_fields[15 + idx] = str(phase_values[phase]["chrg_power"])
            response_fields[20 + idx] = str(phase_values[phase]["dchrg_power"])

        response_fields += ["0"] * (len(RESPONSE_LABELS) - len(response_fields))
        self._info_idx_counter = (self._info_idx_counter + 1) % 256
        return response_fields

    async def _call_before_send(self, addr, fields, consumer_id):
        if not self.before_send:
            return None
        try:
            return await self.before_send(addr, fields, consumer_id)
        except Exception as exc:
            logger.warning("before_send failed for %s: %s", addr, exc)
            return None

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

        if reported_phase not in ("A", "B", "C", "0", ""):
            logger.debug(
                "CT002 request from %s has invalid phase '%s'",
                addr,
                reported_phase,
            )
            return

        in_inspection_mode = reported_phase in ("0", "")

        logger.debug(
            "CT002 parsed fields from %s: meter_dev_type=%s meter_mac=%s ct_type=%s ct_mac=%s phase=%s power=%s consumer_id=%s%s",
            addr,
            fields[0] if len(fields) > 0 else None,
            fields[1] if len(fields) > 1 else None,
            fields[2] if len(fields) > 2 else None,
            fields[3] if len(fields) > 3 else None,
            reported_phase or "(inspection)",
            reported_power,
            consumer_id,
            " in inspection mode" if in_inspection_mode else "",
        )

        # Deduplication check
        current_time = time.time()
        last_time = self._last_response_time.get(addr)
        if last_time and (current_time - last_time) < self.dedupe_time_window:
            logger.debug("Ignoring request from %s due to dedupe window", addr)
            return
        self._last_response_time[addr] = current_time

        if not in_inspection_mode:
            meter_dev_type = fields[0] if len(fields) > 0 else ""
            self._update_consumer_report(
                consumer_id,
                phase=reported_phase,
                power=reported_power,
                device_type=meter_dev_type,
            )

        updated = await self._call_before_send(addr, fields, consumer_id)
        if updated is not None:
            self.set_consumer_value(consumer_id, updated)

        values = self._get_consumer_value(consumer_id)
        if values is None:
            values = [0, 0, 0]
        raw_values = ([*list(values), 0, 0, 0])[:3]
        meter_value = sum(parse_int(v, 0) for v in raw_values)
        is_active = self.is_consumer_active(consumer_id)
        if self.active_control and not in_inspection_mode:
            values = self._compute_smooth_target(values, consumer_id)
        values = ([*list(values), 0, 0, 0])[:3]

        try:
            response_fields = self._build_response_fields(fields, values)
            response = build_payload(response_fields)
        except Exception as exc:
            logger.warning(
                "Failed to build CT002 response for %s (%s): %s",
                addr,
                fields,
                exc,
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
                    "last_seen": datetime.now(timezone.utc).isoformat(),
                    "smooth_target": self._smoother.value
                    if self._smoother.value is not None
                    else 0.0,
                    "manual_target": consumer.manual_target if consumer else None,
                    "auto_target": not consumer.manual_enabled if consumer else True,
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
