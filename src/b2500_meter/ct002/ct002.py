from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable

from b2500_meter.config.logger import logger

SOH = 0x01
STX = 0x02
ETX = 0x03
SEPARATOR = "|"
UDP_PORT = 12345
CLEANUP_INTERVAL_SECONDS = 5
EFFICIENCY_HYSTERESIS_FACTOR = 1.2

RESPONSE_LABELS = [
    "meter_dev_type",
    "meter_mac_code",
    "hhm_dev_type",
    "hhm_mac_code",
    "A_phase_power",
    "B_phase_power",
    "C_phase_power",
    "total_power",
    "A_chrg_nb",
    "B_chrg_nb",
    "C_chrg_nb",
    "ABC_chrg_nb",
    "wifi_rssi",
    "info_idx",
    "x_chrg_power",
    "A_chrg_power",
    "B_chrg_power",
    "C_chrg_power",
    "ABC_chrg_power",
    "x_dchrg_power",
    "A_dchrg_power",
    "B_dchrg_power",
    "C_dchrg_power",
    "ABC_dchrg_power",
]


def calculate_checksum(data_bytes):
    xor = 0
    for b in data_bytes:
        xor ^= b
    return xor


def parse_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def compute_length(payload_without_length):
    base_size = 1 + 1 + len(payload_without_length) + 1 + 2
    for length_digits in range(1, 5):
        total_length = base_size + length_digits
        if len(str(total_length)) == length_digits:
            return total_length
    raise ValueError("Payload length too large")


def build_payload(fields):
    message_str = SEPARATOR + SEPARATOR.join(fields)
    message_bytes = message_str.encode("ascii")
    total_length = compute_length(message_bytes)
    payload = bytearray([SOH, STX])
    payload.extend(str(total_length).encode("ascii"))
    payload.extend(message_bytes)
    payload.append(ETX)
    checksum_val = calculate_checksum(payload)
    checksum = f"{checksum_val:02x}".encode("ascii")
    payload.extend(checksum)
    return payload


def parse_request(data):
    if len(data) < 10:
        return None, "Too short"
    if data[0] != SOH or data[1] != STX:
        return None, "Missing SOH/STX"
    sep_index = data.find(b"|", 2)
    if sep_index == -1:
        return None, "No separator after length"
    try:
        length = int(data[2:sep_index].decode("ascii"))
    except ValueError:
        return None, "Invalid length field"
    if len(data) != length:
        return None, f"Length mismatch (expected {length}, got {len(data)})"
    if data[-3] != ETX:
        return None, "Missing ETX"
    xor = 0
    for b in data[: length - 2]:
        xor ^= b
    expected_checksum = f"{xor:02x}".encode("ascii")
    actual_checksum = data[-2:]
    if actual_checksum.lower() != expected_checksum:
        # Tolerate a leading space in the checksum: some firmware versions
        # emit a space instead of the high hex nibble.
        if (
            actual_checksum[0:1] == b" "
            and actual_checksum[1:2].lower() == expected_checksum[1:2]
        ):
            pass
        else:
            return (
                None,
                f"Checksum mismatch (expected {expected_checksum}, got {actual_checksum})",
            )
    try:
        message = data[sep_index:-3].decode("ascii")
    except UnicodeDecodeError:
        return None, "Invalid ASCII encoding"
    fields = message.split("|")[1:]
    return fields, None


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
        efficiency_rotation_interval=300,
    ):
        self.udp_port = udp_port
        self.ct_mac = ct_mac
        self.ct_type = ct_type
        self.wifi_rssi = wifi_rssi
        self.dedupe_time_window = dedupe_time_window
        self.consumer_ttl = consumer_ttl
        self.debug_status = debug_status
        self.active_control = active_control
        self.smooth_target_alpha = max(0.01, min(1.0, smooth_target_alpha))
        self.max_smooth_step = max(0, max_smooth_step)
        self.fair_distribution = fair_distribution
        self.balance_gain = max(0.0, min(1.0, balance_gain))
        self.error_boost_threshold = max(0, error_boost_threshold)
        self.error_boost_max = max(0.0, error_boost_max)
        self.error_reduce_threshold = max(0, error_reduce_threshold)
        self.balance_deadband = max(0, balance_deadband)
        self.deadband = max(0, deadband)
        self.max_correction_per_step = max(0, max_correction_per_step)
        self.max_target_step = max(0, max_target_step)
        self.saturation_detection = saturation_detection
        self.saturation_alpha = max(0.01, min(1.0, saturation_alpha))
        self.min_target_for_saturation = max(1, min_target_for_saturation)
        self.min_efficient_power = max(0, min_efficient_power)
        self.efficiency_rotation_interval = max(10, efficiency_rotation_interval)
        self.before_send: (
            Callable[[tuple, list, str], Awaitable[list[float] | None]] | None
        ) = None
        self._info_idx_counter = 0
        self._values_by_consumer = {}
        self._reports_by_consumer = {}
        self._last_target_by_consumer = {}
        self._saturation_by_consumer = {}
        self._last_response_time: dict[tuple, float] = {}
        self._smoothed_target = None
        self._last_smooth_sample = None
        self._efficiency_deprioritized: set[str] = set()
        self._efficiency_priority: list[str] = []
        self._efficiency_last_rotation: float = time.time()
        self._efficiency_cache_sample: tuple | None = None
        self._efficiency_cache_result: dict[str, float] | None = None
        self._transport = None
        self._protocol: _CT002Protocol | None = None
        self._cleanup_task = None
        self._stopped = asyncio.Event()

    def _consumer_key(self, addr, fields):
        battery_mac = fields[1] if len(fields) > 1 else ""
        if battery_mac:
            return battery_mac.lower()
        return f"{addr[0]}:{addr[1]}"

    def set_consumer_value(self, consumer_id, values):
        self._values_by_consumer[consumer_id] = values

    def _get_consumer_value(self, consumer_id):
        return self._values_by_consumer.get(consumer_id)

    def _update_consumer_report(self, consumer_id, phase, power, device_type=""):
        normalized_phase = str(phase).upper() if phase else "A"
        previous = self._reports_by_consumer.get(consumer_id, {})
        previous_phase = previous.get("phase")
        self._reports_by_consumer[consumer_id] = {
            "phase": normalized_phase,
            "power": parse_int(power, 0),
            "timestamp": time.time(),
            "device_type": device_type,
        }

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
            for key, report in self._reports_by_consumer.items()
            if now - report.get("timestamp", 0) > self.consumer_ttl
        ]
        for key in stale:
            self._reports_by_consumer.pop(key, None)
            self._values_by_consumer.pop(key, None)
            self._last_target_by_consumer.pop(key, None)
            self._saturation_by_consumer.pop(key, None)
            self._efficiency_deprioritized.discard(key)
            if key in self._efficiency_priority:
                self._efficiency_priority.remove(key)
                # Invalidate cache so next call rebuilds with updated topology
                self._efficiency_cache_sample = None
                self._efficiency_cache_result = None
        stale_addrs = [
            addr
            for addr, ts in self._last_response_time.items()
            if now - ts > self.dedupe_time_window
        ]
        for addr in stale_addrs:
            self._last_response_time.pop(addr, None)

    def _update_saturation(self, consumer_id, last_target, actual):
        """
        Update saturation score using EMA. Saturation = 1 when consumer cannot
        follow target (e.g. full/empty battery); 0 when following well.
        """
        if not self.saturation_detection or last_target is None:
            return
        target_abs = abs(last_target)
        if target_abs < self.min_target_for_saturation:
            return
        if (last_target > 0 and actual < 0) or (last_target < 0 and actual > 0):
            inst_saturation = 1.0
            alpha = self.saturation_alpha
            prev = self._saturation_by_consumer.get(consumer_id, 0.0)
            self._saturation_by_consumer[consumer_id] = (
                alpha * inst_saturation + (1 - alpha) * prev
            )
            return
        follow_ratio = min(1.0, abs(actual) / target_abs)
        inst_saturation = 1.0 - follow_ratio
        alpha = self.saturation_alpha
        prev = self._saturation_by_consumer.get(consumer_id, 0.0)
        self._saturation_by_consumer[consumer_id] = (
            alpha * inst_saturation + (1 - alpha) * prev
        )

    def _compute_efficiency_deprioritized(self, reports, sample_id):
        """Decide which consumers to deprioritize for efficiency.

        At low demand, concentrates power on fewer consumers by reducing
        excess consumers' effective participation weight.  Uses hysteresis
        to prevent oscillation and rotates priority for fairness.

        Returns a dict mapping consumer_id -> weight (0.0 = fully
        deprioritized).  Empty dict means no deprioritization.  Future
        strategies (SOC-based, device-type-aware, proportional) can change
        the weights without modifying the integration point.
        """
        if self.min_efficient_power <= 0 or len(reports) < 2:
            self._efficiency_deprioritized = set()
            self._efficiency_cache_sample = None
            self._efficiency_cache_result = None
            return {}

        now = time.time()

        # Rotation check BEFORE cache: when the grid is stable the
        # sample_id never changes, so the cache would prevent rotation
        # from being evaluated.  Invalidate cache when rotation fires.
        if (
            self._efficiency_priority
            and now - self._efficiency_last_rotation
            >= self.efficiency_rotation_interval
        ):
            self._efficiency_last_rotation = now
            self._efficiency_priority.append(self._efficiency_priority.pop(0))
            self._efficiency_cache_sample = None  # force recompute

        # Sync priority list with current consumers (prune stale, add new at end)
        current = set(reports)
        self._efficiency_priority = [
            c for c in self._efficiency_priority if c in current
        ]
        for cid in sorted(current):
            if cid not in self._efficiency_priority:
                self._efficiency_priority.append(cid)

        # Cache per sample for consistency across consumer calls.
        # Checked AFTER consumer sync so topology changes (new/removed
        # consumers) invalidate stale cached results.
        cache_key = (sample_id, tuple(self._efficiency_priority))
        if cache_key == self._efficiency_cache_sample:
            return self._efficiency_cache_result or {}

        # Estimate total demand from battery outputs + grid residual.
        # smoothed_target alone is wrong: it's the grid residual which
        # approaches 0 when balanced, regardless of actual demand.
        total_battery_power = sum(
            parse_int(reports.get(cid, {}).get("power", 0))
            for cid in self._efficiency_priority
        )
        abs_target = abs(total_battery_power + (self._smoothed_target or 0))
        n = len(self._efficiency_priority)
        per_consumer = abs_target / n

        # Hysteresis: require HIGHER per-consumer demand to EXIT limiting
        # than to ENTER it, preventing oscillation at the boundary.
        was_limiting = len(self._efficiency_deprioritized) > 0
        if was_limiting:
            enter_limiting = per_consumer < (
                self.min_efficient_power * EFFICIENCY_HYSTERESIS_FACTOR
            )
        else:
            enter_limiting = per_consumer < self.min_efficient_power

        if enter_limiting and n > 1:
            # Cap at n-1 to ensure at least one consumer is deprioritized
            # when hysteresis says we should be limiting.
            slots = max(1, min(n - 1, int(abs_target / self.min_efficient_power)))
        else:
            slots = n

        # First `slots` by priority are active, rest deprioritized
        deprioritized = set(self._efficiency_priority[slots:])
        result: dict[str, float] = {cid: 0.0 for cid in deprioritized}

        for cid in deprioritized - self._efficiency_deprioritized:
            logger.info(
                "Efficiency: deprioritizing consumer %s (demand %.0fW, %d active)",
                cid[:16],
                abs_target,
                slots,
            )
        for cid in self._efficiency_deprioritized - deprioritized:
            logger.info(
                "Efficiency: activating consumer %s (demand %.0fW, %d active)",
                cid[:16],
                abs_target,
                slots,
            )

        self._efficiency_deprioritized = deprioritized
        self._efficiency_cache_sample = cache_key
        self._efficiency_cache_result = result
        return result

    def _compute_smooth_target(self, values, consumer_id=None):
        """
        Active control: smooth the raw grid reading and split target across consumers.
        With fair_distribution: adjust each consumer's target to balance actual load.
        With saturation_detection: reduce share for consumers that cannot follow target.
        Phase output is distributed across known consumer phases (A/B/C) based on
        effective participation (saturation-aware); falls back to phase A if unknown.
        """
        if not self.active_control or not values or len(values) != 3:
            return values
        raw_total = sum(parse_int(v, 0) for v in values)
        alpha = self.smooth_target_alpha

        # Apply smoothing only once per meter sample.  Multiple consumers
        # call this method with the same grid reading; the sample ID
        # (tuple of values) prevents compounding the update.
        sample_id = tuple(values)
        if self._smoothed_target is None:
            self._smoothed_target = raw_total
            self._last_smooth_sample = sample_id
        elif sample_id != self._last_smooth_sample:
            self._last_smooth_sample = sample_id
            if self.deadband > 0 and abs(raw_total) < self.deadband:
                delta = -alpha * self._smoothed_target
            else:
                catchup_alpha = alpha
                if (raw_total > 0) != (self._smoothed_target > 0):
                    catchup_alpha = min(0.5, alpha * 4)
                delta = catchup_alpha * (raw_total - self._smoothed_target)
            if self.max_smooth_step > 0:
                delta = max(
                    -self.max_smooth_step,
                    min(self.max_smooth_step, delta),
                )
            self._smoothed_target += delta

        reports = dict(self._reports_by_consumer)
        last_target = self._last_target_by_consumer.get(consumer_id)

        if consumer_id and consumer_id in reports:
            actual_self = parse_int(reports.get(consumer_id, {}).get("power", 0))
            self._update_saturation(consumer_id, last_target, actual_self)

        # Snapshot after _update_saturation may have modified the dict.
        saturation = dict(self._saturation_by_consumer)
        num_consumers = max(1, len(reports))
        eff_part = {cid: max(0.01, 1.0 - saturation.get(cid, 0.0)) for cid in reports}
        # Efficiency optimization: deprioritize excess consumers at low demand
        efficiency_adjustments = self._compute_efficiency_deprioritized(
            reports, sample_id
        )
        for cid, weight in efficiency_adjustments.items():
            if cid in eff_part:
                eff_part[cid] = weight
        # Early return for deprioritized consumers: the battery uses integral
        # control (target = current_power + grid_reading), so sending [0,0,0]
        # means "stay at current power".  Instead, send the negative of the
        # battery's reported power to drive it toward zero.
        if (
            efficiency_adjustments
            and consumer_id
            and efficiency_adjustments.get(consumer_id) == 0.0
        ):
            reported = parse_int(reports.get(consumer_id, {}).get("power", 0))
            if consumer_id:
                self._last_target_by_consumer[consumer_id] = 0
            if reported == 0:
                return [0, 0, 0]
            phase = (reports.get(consumer_id, {}).get("phase") or "A").upper()
            result = [0.0, 0.0, 0.0]
            result[{"A": 0, "B": 1, "C": 2}.get(phase, 0)] = float(-reported)
            return result
        total_effective = sum(eff_part.values())
        fair_share = (
            (self._smoothed_target / total_effective) * eff_part.get(consumer_id, 1.0)
            if consumer_id and consumer_id in reports
            else self._smoothed_target / num_consumers
        )
        if (
            not self.fair_distribution
            or consumer_id is None
            or consumer_id not in reports
            or (self.deadband > 0 and abs(raw_total) < self.deadband)
        ):
            target = fair_share
        elif consumer_id in eff_part:
            actual_self = parse_int(reports.get(consumer_id, {}).get("power", 0))
            participating = [cid for cid in reports if eff_part.get(cid, 1.0) > 0.1]
            if participating:
                actual_total = sum(
                    parse_int(reports.get(cid, {}).get("power", 0))
                    for cid in participating
                )
                actual_avg = actual_total / len(participating)
                error = actual_avg - actual_self
                err_abs = abs(error)
                if self.balance_deadband > 0 and err_abs < self.balance_deadband:
                    target = fair_share
                else:
                    gain = self.balance_gain
                    if (
                        self.error_reduce_threshold > 0
                        and err_abs < self.error_reduce_threshold
                    ):
                        gain = gain * (err_abs / self.error_reduce_threshold)
                    elif self.error_boost_threshold > 0 and self.error_boost_max > 0:
                        boost = (
                            min(err_abs / self.error_boost_threshold, 1.0)
                            * self.error_boost_max
                        )
                        gain = gain * (1.0 + boost)
                    correction = gain * error
                    if self.max_correction_per_step > 0:
                        cap = self.max_correction_per_step
                        if correction > cap:
                            correction = cap
                        elif correction < -cap:
                            correction = -cap
                    target = fair_share + correction
                    if self.max_target_step > 0:
                        lo = actual_self - self.max_target_step
                        hi = actual_self + self.max_target_step
                        target = max(lo, min(hi, target))
            else:
                target = fair_share
        # When meter and target disagree on sign, clamp to avoid fighting the
        # actual state (smoothing lag can command discharge during export or
        # charge during import, worsening overshoot)
        if (raw_total < 0 and target > 0) or (raw_total > 0 and target < 0):
            target = 0

        if consumer_id:
            self._last_target_by_consumer[consumer_id] = target

        # Distribute target across phases according to active consumer phase mapping.
        phase_effective = {"A": 0.0, "B": 0.0, "C": 0.0}
        for cid, report in reports.items():
            phase = (report.get("phase") or "A").upper()
            if phase not in phase_effective:
                phase = "A"
            phase_effective[phase] += eff_part.get(cid, 1.0)

        total_phase_effective = sum(phase_effective.values())
        if total_phase_effective <= 0:
            return [target, 0, 0]

        return [
            target * (phase_effective["A"] / total_phase_effective),
            target * (phase_effective["B"] / total_phase_effective),
            target * (phase_effective["C"] / total_phase_effective),
        ]

    def _collect_reports_by_phase(self):
        by_phase = {
            "A": {"chrg_power": 0, "dchrg_power": 0, "active": False},
            "B": {"chrg_power": 0, "dchrg_power": 0, "active": False},
            "C": {"chrg_power": 0, "dchrg_power": 0, "active": False},
        }
        reports = list(self._reports_by_consumer.items())

        for _consumer_id, report in reports:
            phase = (report.get("phase") or "A").upper()
            if phase not in by_phase:
                phase = "A"
            power = parse_int(report.get("power", 0))
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
        reports = list(self._reports_by_consumer.items())
        consumers = (
            " ".join(
                f"{cid[:8]}@{r.get('phase', '?')}:{r.get('power', 0)}"
                for cid, r in sorted(reports, key=lambda x: x[0])
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

        # Deduplication check — stamp immediately (before any await) so a
        # second rapid packet from the same addr sees the updated time.
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
        meter_value = sum(parse_int(v, 0) for v in values)
        if self.active_control and not in_inspection_mode:
            values = self._compute_smooth_target(values, consumer_id)
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
        # Close transport first to stop new datagrams from spawning tasks,
        # then cancel and await any in-flight handler tasks.
        if self._transport:
            self._transport.close()
            self._transport = None
        if self._protocol:
            for task in list(self._protocol._tasks):
                task.cancel()
            await asyncio.gather(*self._protocol._tasks, return_exceptions=True)
        self._protocol = None
        self._stopped.set()
