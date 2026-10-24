"""Async Marstek battery simulator.

Speaks the CT002 UDP protocol, sends periodic requests to the CT002
emulator, receives per-phase power targets, and adjusts its simulated
output accordingly.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from astrameter.ct002 import protocol
from astrameter.ct002.balancer import device_capabilities

from .b2500_steering import MIN_CHANNEL_OUTPUT_W, B2500SteeringController
from .firmware_steering import FirmwareSteeringController
from .phase_detection import (
    DetectorInputs,
    PhaseDetector,
    make_phase_detector,
    meter_code_for,
)
from .venus_integer_steering import (
    DEADBAND_HMG50_W,
    DEADBAND_W,
    PARK_ALONE_HMG50,
    PARK_ALONE_VENUS,
    VenusIntegerSteeringController,
)

logger = logging.getLogger("astra_sim.battery")

# Phase letters a battery can sit on, mapped to the index of its power field in
# ``protocol.RESPONSE_LABELS`` (``A_phase_power`` at 4).
PHASE_FIELD_INDEX: dict[str, int] = {"A": 4, "B": 5, "C": 6}

# Phase bytes a battery can store, as ``rechrg_seq`` numbers them.
STORED_PHASES: dict[str, int] = {"A": 1, "B": 2, "C": 3, "D": 4}

# The CT MAC a battery with no CT bound sends; it then accepts any reply.
WILDCARD_CT_MAC = "000000000000"

# Consecutive failed polls after which the meter task marks the meter offline:
# six failures per reconnect cycle, four cycles (HMG-50 0x08023cae-0x08023cc6).
METER_OFFLINE_AFTER_FAILURES = 24

# The Venus D's power setter (VNSD-0 v151 0x0800a968), which detection and
# steering both go through, adds two PV rules the Venus E 3.0's (VNSE3-0
# 0x0800a48c) does not have. Charging gives way to PV (0x08008bfc): once a PV
# input delivers more than 10 W, AC charging is cut to what the charge limit,
# itself capped at 2500 W, leaves after PV. Surplus feed-in ("余电上网中",
# 0x08009ab0): with the battery full, PV present and a setting on, the output
# never drops below the PV power plus 10 W.
VENUS_D_PV_PRESENT_W = 10
VENUS_D_CHARGE_CAP_W = 2500
VENUS_D_FEED_IN_MARGIN_W = 10
# Feed-in starts at 100 % and holds down to 95 % (0x08009b08 / 0x08009b18).
VENUS_D_FEED_IN_ON_PCT = 100
VENUS_D_FEED_IN_HOLD_PCT = 95


class BatterySimulator:
    def __init__(
        self,
        mac: str,
        phase: str,
        ct_mac: str,
        ct_host: str = "127.0.0.1",
        ct_port: int = 12345,
        meter_dev_type: str = "HMG-50",
        ct_dev_type: str = "HME-4",
        max_charge_power: int = 800,
        max_discharge_power: int = 800,
        capacity_wh: float = 2560.0,
        initial_soc: float = 0.5,
        ramp_rate: float = 200.0,
        poll_interval: float = 1.0,
        min_power_threshold: float = 20.0,
        startup_delay: float = 2.0,
        inspection_count: int = 1,
        time_scale: float = 1.0,
        power_update_delay_ticks: int = 0,
        max_dc_input: int = 0,
        dc_input_power: float = 0.0,
        participates: bool = True,
        initial_power: float = 0.0,
        min_channel_output: int = MIN_CHANNEL_OUTPUT_W,
        phase_detection: bool = False,
        stored_phase: str | None = None,
        sum_all_phases: bool = True,
        surplus_feed_in: bool = False,
    ) -> None:
        if phase not in PHASE_FIELD_INDEX:
            raise ValueError(
                f"Invalid phase {phase!r}, must be one of {list(PHASE_FIELD_INDEX)}"
            )
        if stored_phase is not None and stored_phase not in STORED_PHASES:
            raise ValueError(
                f"Invalid stored_phase {stored_phase!r}, "
                f"must be one of {list(STORED_PHASES)}"
            )

        self.mac = mac.upper()
        self.phase = phase
        self.ct_mac = ct_mac
        self.ct_host = ct_host
        self.ct_port = ct_port
        self.meter_dev_type = meter_dev_type
        self.ct_dev_type = ct_dev_type
        self.max_charge_power = max_charge_power
        self.max_discharge_power = max_discharge_power
        self.capacity_wh = capacity_wh
        self.ramp_rate = ramp_rate
        self.poll_interval = poll_interval
        self.min_power_threshold = min_power_threshold
        self.startup_delay = max(0.0, startup_delay)
        self.inspection_count = inspection_count
        self.time_scale = max(0.1, time_scale)
        self.power_update_delay_ticks = max(0, int(power_update_delay_ticks))
        self.max_dc_input = max(0, int(max_dc_input))
        self.participates = participates

        self._current_power: float = 0.0
        self._soc: float = max(0.0, min(1.0, initial_soc))
        self._target_power: float = 0.0
        self._requested_target: float = 0.0
        self._request_count: int = 0
        self._last_update: float = time.monotonic()
        self._startup_elapsed: float = 0.0
        self._step_index: int = 0
        self._pending_power_targets: list[tuple[int, float]] = []
        self._dc_input_power: float = 0.0
        self.dc_input_power = dc_input_power  # reuse setter clamp
        # In-flight polls started by :meth:`run`.  Held only so the event loop
        # keeps a strong reference to them (asyncio tasks are weakly
        # referenced and would otherwise be collectable mid-flight).
        self._poll_tasks: set[asyncio.Task] = set()

        # Firmware phase detection (see :mod:`phase_detection`). Off, the battery
        # reports ``inspection_count`` polls as phase ``0`` and then *phase*, as
        # before. On, *phase* is only where the battery is wired — what the
        # simulated meter sees — and the phase it reports and steers on is the
        # one it detects (or *stored_phase*, a phase it already has stored). The
        # battery then also handles CT replies the way the firmware does: it
        # rejects a reply naming another CT type or (once bound) another CT
        # MAC, takes no new reading from one repeating ``info_idx``, holds 0 W
        # while the meter is offline, and steers only once it has a phase.
        self._detector: PhaseDetector | None = (
            make_phase_detector(
                self.meter_dev_type,
                STORED_PHASES[stored_phase] if stored_phase else 0,
            )
            if phase_detection
            else None
        )
        # The "sum all phases" setting (``dchrg_t``; HMG-50 config +0x71,
        # VNSE3-0 0x200174f0+0x67): the factory default 1 steers on the total,
        # 0 on the battery's own phase. Only read with phase detection on.
        self.sum_all_phases = sum_all_phases
        # Venus D only, with phase detection on: the app setting that allows
        # surplus feed-in (which one is not known; 0x2001769c+0x16).
        self.surplus_feed_in = surplus_feed_in
        self._feed_in_active = False
        # BMS gate flags the detector checks (inferred meaning, see
        # :mod:`phase_detection`).
        self.charge_allowed = True
        self.discharge_allowed = True
        self._fw_time = 0.0
        self._fw_next_tick = 0.0
        self._readings: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._meter_online = False
        self._failed_polls = 0
        self._last_info_idx = 0
        # HMG-50: parsed from each accepted reply's CT type; Venus: configured.
        self._meter_code = (
            0
            if self._is_hmg50_type()
            else meter_code_for(self.meter_dev_type, self.ct_dev_type)
        )

        # Self-consumption control law, run on the grid value read back from the
        # CT. This ramp controller is the fallback: the Venus/HMG-50 and the
        # DC-coupled B2500 controllers built below take over for those families,
        # leaving Jupiter (HMN/HMM/JPLS) and any unrecognised device type here.
        self._steering = FirmwareSteeringController()

        # The B2500 family (HMA/HMJ/HMK) is DC-coupled (see :mod:`b2500_steering`):
        # two DC output channels, each its own regulator running every cycle, so
        # the combined output slews at twice a single channel's rate.
        caps = device_capabilities(self.meter_dev_type)
        self._is_dc_output = (
            caps.has_dc_input
            and not caps.has_builtin_inverter
            and not caps.has_ac_input
        )
        # One device-level controller: the firmware's integrator is device-wide
        # and the split across the two outputs happens below it. The unit's
        # minimum (``pmin``) is twice the per-channel floor callers pass in, and
        # it is a *clamp on the command*, not a gate on the response — a B2500
        # cannot form a command below it at all (``0`` removes the floor).
        self._b2500 = (
            B2500SteeringController(
                p=max(1, self.max_discharge_power),
                pmin=2 * min_channel_output,
            )
            if self._is_dc_output
            else None
        )

        # The AC-coupled Venus units that aren't an HMG-50 (VNSA-0, VNSD-0,
        # VNSE3-0) all run one law: an integer proportional integrator behind an
        # input-conditioning gate (see :mod:`venus_integer_steering`). All three
        # firmwares were read end to end — integrator and gate alike, and the
        # gate is the same routine in each. Its setpoint convention is the
        # opposite of the HMG-50 ramp controller's — positive = discharge,
        # negative = charge — so ``hi`` is the discharge limit and ``lo`` the
        # (negative) charge limit, and the simulator target is the setpoint
        # *unnegated*.
        # The HMG-50 carries *both* laws and picks between them on a model code
        # it parses out of the meter's greeting; only code 1 — the "no model
        # suffix" fallback — takes the float gain-table ramp. AstraMeter
        # announces ``HME-4``, so a real HMG-50 driven by it runs the same
        # integer integrator as the other Venus units, with a wider rest
        # deadband and a wider single-unit park.
        _dt = self.meter_dev_type.upper()
        self._is_hmg50 = _dt.startswith("HMG")
        self._is_venus_integer = (
            _dt.startswith("VNS") or self._is_hmg50
        ) and not self._is_dc_output
        self._venus_steering = (
            VenusIntegerSteeringController(
                park_alone=PARK_ALONE_HMG50 if self._is_hmg50 else PARK_ALONE_VENUS,
                deadband=DEADBAND_HMG50_W if self._is_hmg50 else DEADBAND_W,
            )
            if self._is_venus_integer
            else None
        )

        # Optionally start already in motion (net output W; positive = discharge,
        # negative = charge) instead of cold-starting from rest. The firmware
        # input deadband holds a sub-deadband command only while a unit is at
        # rest, so a from-rest-only model can't represent a system already
        # running when a disturbance arrives. The relevant steering controller's
        # setpoint is seeded so its ramp law holds the seeded output instead of
        # winding back to zero on the first cycle.
        # A DC-coupled B2500 has no AC input and cannot charge, so a negative
        # seed would start it in a state it can never physically be in and
        # would simulate charging until the first CT reply displaced it. Clamp
        # the seed itself, not just the controller's command.
        if initial_power and self._b2500 is not None:
            initial_power = max(0, initial_power)
        if initial_power:
            self._current_power = float(initial_power)
            self._target_power = float(initial_power)
            self._requested_target = float(initial_power)
            if self._venus_steering is not None:
                self._venus_steering.setpoint = round(initial_power)
            elif self._b2500 is not None:
                # The B2500's integrator accumulates onto its own command, so it
                # has to start from the seeded output — and it only integrates
                # while it is actually producing.
                self._b2500.seed(round(initial_power))
            else:
                # Ramp controller: target = -setpoint, so seed the inverse.
                self._steering.setpoint = -float(initial_power)

    # -- public read-only properties ---------------------------------------

    @property
    def current_power(self) -> float:
        return self._current_power

    @current_power.setter
    def current_power(self, value: float) -> None:
        self._current_power = value

    @property
    def soc(self) -> float:
        return self._soc

    @soc.setter
    def soc(self, value: float) -> None:
        self._soc = max(0.0, min(1.0, value))

    @property
    def target_power(self) -> float:
        return self._target_power

    @property
    def detector(self) -> PhaseDetector | None:
        """The firmware phase detector, when phase detection is on."""
        return self._detector

    @property
    def reported_phase(self) -> str:
        """The phase character this battery puts in its next request."""
        if self._detector is not None:
            return self._detector.phase_char
        return "0" if self._request_count < self.inspection_count else self.phase

    @property
    def meter_online(self) -> bool:
        return self._meter_online

    def start_phase_detection(self) -> None:
        """Re-run phase detection, as a CT-MAC or meter-type change or the
        app's ``seq_check`` does. The output drops to 0 at once."""
        if self._detector is None:
            raise RuntimeError("phase detection is not enabled for this battery")
        self._detector.start()
        self._apply_ct_derived_target(self._detector.zero_w)

    def _is_hmg50_type(self) -> bool:
        return self.meter_dev_type.upper().startswith("HMG")

    def _is_venus_d(self) -> bool:
        return self.meter_dev_type.upper().startswith("VNSD")

    def _venus_d_output(self, command: float) -> float:
        """Apply the Venus D's PV rules to a power command (see
        ``VENUS_D_*``). The firmware sums four PV inputs and tests each one;
        the simulator has a single DC input and treats it as their sum."""
        pv = int(self._dc_input_power)
        if command < 0 and pv > VENUS_D_PV_PRESENT_W:
            room = pv - min(self.max_charge_power, VENUS_D_CHARGE_CAP_W)
            command = 0.0 if room >= 0 else max(command, float(room))
        soc_pct = int(self._soc * 100)
        if not (self.surplus_feed_in and pv > 0) or soc_pct < VENUS_D_FEED_IN_HOLD_PCT:
            self._feed_in_active = False
        elif soc_pct >= VENUS_D_FEED_IN_ON_PCT:
            self._feed_in_active = True
        if self._feed_in_active:
            command = max(command, float(pv + VENUS_D_FEED_IN_MARGIN_W))
        return command

    @property
    def dc_input_power(self) -> float:
        return self._dc_input_power

    @dc_input_power.setter
    def dc_input_power(self, value: float) -> None:
        self._dc_input_power = max(0.0, min(float(self.max_dc_input), float(value)))

    def _apply_ct_derived_target(self, new_target: float) -> None:
        """Record CT request immediately; apply to physics after *power_update_delay_ticks*."""
        if self._detector is not None and self._is_venus_d():
            new_target = self._venus_d_output(new_target)
        self._requested_target = new_target
        if self.power_update_delay_ticks <= 0:
            self._target_power = new_target
            return
        apply_at = self._step_index + self.power_update_delay_ticks
        self._pending_power_targets.append((apply_at, new_target))

    def _drain_pending_power_targets(self) -> None:
        if self.power_update_delay_ticks <= 0:
            return
        remaining: list[tuple[int, float]] = []
        for apply_at, target in self._pending_power_targets:
            if apply_at <= self._step_index:
                self._target_power = target
            else:
                remaining.append((apply_at, target))
        self._pending_power_targets = remaining

    # -- physics -----------------------------------------------------------

    def _update_power(self, dt: float) -> None:
        target = self._target_power

        if abs(target) < self.min_power_threshold:
            target = 0.0

        # SOC saturation
        if self._soc >= 1.0 and target < 0:
            target = 0.0
        if self._soc <= 0.0 and target > 0:
            target = 0.0

        # Startup delay: when resuming from idle the real inverter needs
        # a few seconds before it begins ramping.  During this window the
        # battery stays at ~0 W, which is the behaviour that previously
        # triggered false saturation detection.
        if self.startup_delay > 0:
            idle = abs(self._current_power) < self.min_power_threshold
            want_power = abs(target) >= self.min_power_threshold
            if idle and want_power:
                self._startup_elapsed += dt
                if self._startup_elapsed < self.startup_delay:
                    self._apply_dc_passthrough()
                    return  # stay at current (near-zero) power
            else:
                self._startup_elapsed = 0.0

        # Ramp toward target
        diff = target - self._current_power
        max_step = self.ramp_rate * dt
        if abs(diff) > max_step:
            diff = max_step if diff > 0 else -max_step
        self._current_power += diff

        # Clamp to limits
        self._current_power = max(
            -self.max_charge_power,
            min(self.max_discharge_power, self._current_power),
        )

        # When SoC is saturated and DC input is present, the inverter
        # passes the unabsorbed PV through to AC even if the AC target
        # asks for charging.  Mirrors Marstek Venus D behaviour.
        self._apply_dc_passthrough()

    def _apply_dc_passthrough(self) -> None:
        if self._soc < 1.0 or self._dc_input_power <= 0:
            return
        # Push at least the DC input through to AC as positive output.
        self._current_power = max(self._current_power, self._dc_input_power)

    def _update_soc(self, dt: float) -> None:
        if self.capacity_wh <= 0:
            return
        # AC energy first (positive current_power drains, negative charges)
        energy_wh = self._current_power * (dt / 3600.0)
        self._soc -= energy_wh / self.capacity_wh
        # DC input charges the cells in parallel (when not already full).
        if self._dc_input_power > 0:
            dc_energy_wh = self._dc_input_power * (dt / 3600.0)
            self._soc += dc_energy_wh / self.capacity_wh
        self._soc = max(0.0, min(1.0, self._soc))

    # -- protocol ----------------------------------------------------------

    def _request_fields(self) -> list[str]:
        """Build the CT002 request fields for this poll."""
        phase_field = self.reported_phase
        fields = [
            self.meter_dev_type,
            self.mac,
            self.ct_dev_type,
            self.ct_mac,
            phase_field,
            str(round(self._current_power)),
        ]
        if not self.participates:
            # Opt out of CT aggregation via the optional 7th "participate"
            # field. Participating batteries omit it (matches Venus, which
            # sends only 6 fields).
            fields.append("0")
        return fields

    async def _send_request(self) -> list[str] | None:
        request_fields = self._request_fields()
        phase_field = request_fields[4]
        # Claim the sequence number now, not after the reply lands. `run()`
        # fires polls without awaiting them, so counting on receipt would let
        # concurrent polls read the same value and repeat the inspection phase
        # — and a poll that is never answered (dedupe drop, lost datagram)
        # would leave the battery stuck in inspection forever. A real device
        # counts the polls it sent.
        self._request_count += 1
        payload = protocol.build_payload(request_fields)

        loop = asyncio.get_running_loop()
        transport = None
        try:
            transport, proto = await asyncio.wait_for(
                loop.create_datagram_endpoint(
                    lambda: _UDPClient(),
                    remote_addr=(self.ct_host, self.ct_port),
                ),
                timeout=2.0,
            )
            transport.sendto(payload)
            data = await asyncio.wait_for(proto.received, timeout=2.0)
        except (asyncio.TimeoutError, TimeoutError, OSError) as exc:
            logger.debug("Battery %s: request failed: %s", self.mac, exc)
            self._note_failed_poll()
            return None
        finally:
            if transport is not None:
                transport.close()

        response_fields, err = protocol.parse_request(data)
        if response_fields is None:
            logger.debug("Battery %s: bad response: %s", self.mac, err)
            self._note_failed_poll()
            return None

        if self._detector is not None:
            self._receive_firmware(response_fields)
            return response_fields

        # Hand parsed response off to the deterministic helper so it can
        # also be unit-tested without UDP I/O.
        if response_fields and phase_field != "0":
            self._handle_ct_response(response_fields)

        return response_fields

    # -- firmware reply handling (phase detection on) ------------------------

    def _note_failed_poll(self) -> None:
        if self._detector is None:
            return
        self._failed_polls += 1
        if self._failed_polls >= METER_OFFLINE_AFTER_FAILURES:
            self._meter_online = False

    def _accept_reply(self, fields: list[str]) -> int:
        """Classify a reply as the HMG-50 parser does (0x08026464).

        ``0``: rejected — another CT type than the one requested (0x08026718),
        or another CT MAC than the bound one (0x08026b24). ``2``: accepted but
        ``info_idx`` repeats the previous reply's and is non-zero
        (0x08026ba6), so no new reading. ``1``: a new reading.
        """
        if len(fields) < 14 or fields[0] != self.ct_dev_type:
            return 0
        if self.ct_mac not in ("", WILDCARD_CT_MAC) and fields[1] != self.ct_mac:
            return 0
        try:
            info_idx = int(fields[13]) & 0xFF
        except ValueError:
            info_idx = 0
        repeated = info_idx == self._last_info_idx
        self._last_info_idx = info_idx
        if repeated and info_idx != 0:
            return 2
        return 1

    def _receive_firmware(self, fields: list[str]) -> None:
        """Take one CT reply the way the firmware does, then run the loop pass
        its arrival wakes."""
        verdict = self._accept_reply(fields)
        if verdict == 0:
            self._note_failed_poll()
            return
        self._failed_polls = 0
        self._meter_online = True
        if verdict == 2:
            return
        if self._is_hmg50_type():
            self._meter_code = meter_code_for(self.meter_dev_type, fields[0])

        def field(idx: int) -> int:
            try:
                return int(fields[idx])
            except (IndexError, ValueError):
                return 0

        self._readings = (field(4), field(5), field(6), field(7))
        self._detector_pass(self._fw_time)
        self._fw_next_tick = self._fw_time + self._loop_period()
        assert self._detector is not None
        if self._detector.steering:
            self._steer_firmware(fields)

    def _steer_firmware(self, fields: list[str]) -> None:
        """Steer on the reading the firmware selects (HMG-50 0x080232c8,
        VNSE3-0 0x0802e954).

        With "sum all phases" set — the factory default, ``dchrg_t`` = 1 — the
        battery steers on ``total_power`` and divides it by the sum of all four
        ``*_chrg_nb`` counts (bucket select, HMG-50 0x080055ea). Without it, it
        steers on its own phase's power field and count, or the total and
        ``ABC_chrg_nb`` for phase D.
        """
        assert self._detector is not None
        phase = self._detector.phase

        def field(idx: int) -> int:
            try:
                return int(fields[idx])
            except (IndexError, ValueError):
                return 0

        if self.sum_all_phases:
            self._handle_ct_response(
                fields,
                grid_reading=field(7),
                phase_count=sum(field(i) for i in range(8, 12)),
            )
            return
        self._handle_ct_response(
            fields,
            grid_reading=field(3 + phase if phase < 4 else 7),
            phase_count=field(7 + phase if phase < 4 else 11),
        )

    def _loop_period(self) -> float:
        assert self._detector is not None
        return self._detector.loop_period_s

    def _detector_pass(self, now: float) -> None:
        assert self._detector is not None
        result = self._detector.tick(
            DetectorInputs(
                now=now,
                readings=self._readings,
                soc_pct=int(self._soc * 100),
                meter_online=self._meter_online,
                meter_code=self._meter_code,
                window_w=(
                    float(self.max_discharge_power)
                    if self.discharge_allowed or not self._is_hmg50_type()
                    else 0.0
                ),
                charge_allowed=self.charge_allowed,
                discharge_allowed=self.discharge_allowed,
            )
        )
        if result.reset_setpoint and self._venus_steering is not None:
            self._venus_steering.setpoint = 0
        if result.command is not None:
            self._apply_ct_derived_target(result.command)

    def _run_detector_loop(self, dt: float) -> None:
        """The loop passes that fall inside the next *dt* seconds: one per
        queue-wait timeout when no CT reply wakes it earlier."""
        if self._detector is None:
            return
        end = self._fw_time + dt
        period = self._loop_period()
        while self._fw_next_tick <= end:
            self._detector_pass(self._fw_next_tick)
            self._fw_next_tick += period
        self._fw_time = end

    def _handle_ct_response(
        self,
        response_fields: list[str],
        *,
        grid_reading: int | None = None,
        phase_count: int | None = None,
    ) -> None:
        """Derive the new AC target from the grid value read back from the CT.

        The grid value (sum of the per-phase power fields, positive = importing)
        is fed to this battery's steering controller. Every AC-coupled unit —
        the HMG-50 (Venus C) included, since it takes its float ramp only for a
        meter model code AstraMeter does not present — runs
        :class:`VenusIntegerSteeringController`, whose setpoint is already in
        the simulator's sign (positive = discharge) and is applied directly;
        the HMG-50 differs only in its rest deadband and single-unit park. A
        DC-coupled B2500 instead runs :class:`B2500SteeringController` on its
        DC output (see :meth:`_steer_b2500_output`).
        :class:`FirmwareSteeringController` — the ramp law for the code-1 path
        — is the fallback for everything else: Jupiter (``HMN``/``HMM``/
        ``JPLS``), which is AC-coupled but not a Venus, and any device type
        :func:`device_capabilities` does not recognise.

        Cross-battery share-split: a real battery divides the grid value by the
        number of batteries reported on its phase (the ``*_chrg_nb`` count), so
        several batteries on one phase each take their share rather than all
        chasing the full residual. This matters in relay mode / against a real
        CT; AstraMeter's active-control emulator distributes per-battery targets
        itself and reports a count of 1, so the split is a no-op there.

        *grid_reading* and *phase_count* override the values read from the
        reply, for a caller that selects them itself (firmware phase detection
        steers on its detected phase's bucket, not the wired one).
        """

        def field(idx: int) -> int:
            try:
                return int(response_fields[idx])
            except (IndexError, ValueError, TypeError):
                return 0

        if grid_reading is None:
            grid_reading = field(4) + field(5) + field(6)

        if self._b2500 is not None:
            self._steer_b2500_output(grid_reading)
            return

        # *_chrg_nb for this battery's phase (fields 9/10/11 → indices 8/9/10).
        if phase_count is None:
            phase_count = field(8 + "ABC".index(self.phase))

        if self._venus_steering is not None:
            # Venus A / D / E: integer integrator behind its own gate, positive
            # setpoint = discharge. Both the gate and the integrator's per-step
            # branch key off the unit's own measured output rather than the CT
            # value (see :mod:`venus_integer_steering`), and the ``*_chrg_nb``
            # count both splits the reading and widens the park to ±15 W.
            venus_setpoint = self._venus_steering.step(
                grid_reading,
                float(self.max_discharge_power),
                -float(self.max_charge_power),
                out=self._current_power,
                device_count=phase_count,
            )
            self._apply_ct_derived_target(float(venus_setpoint))
            return

        # Charge / discharge limits in the ramp controller's convention
        # (setpoint positive = charge), read live like the Venus path above so
        # a runtime change to either cap takes effect.
        setpoint = self._steering.step(
            grid_reading,
            float(self.max_charge_power),
            -float(self.max_discharge_power),
            device_count=phase_count,
            out=self._current_power,
        )
        # Controller: +charge → simulator: +discharge.
        self._apply_ct_derived_target(-setpoint)

    def _steer_b2500_output(self, grid_reading: int) -> None:
        """Steer a DC-coupled B2500's DC output toward nulling the grid.

        The B2500 only discharges its DC output (no AC input, never charges from
        AC). The firmware integrates the grid reading into a device-wide command
        clamped to ``[pmin, p]``, then splits it across the two outputs; see
        :mod:`b2500_steering`.
        """
        assert self._b2500 is not None
        envelope = self.max_discharge_power
        # At full SoC the PV passes straight through to the output and cannot be
        # curtailed below the DC input (the pack is full, the PV has nowhere else
        # to go). The steering can't drive the output under that floor, so don't
        # let it try — otherwise it fights the passthrough override and the
        # output oscillates instead of settling at the PV level.
        floor = (
            round(self._dc_input_power)
            if self._soc >= 1.0 and self._dc_input_power > 0
            else 0
        )
        out = self._b2500.step(
            grid_reading,
            max(0, round(self._current_power)),
            self.poll_interval,
            max_power=envelope,
        )
        self._apply_ct_derived_target(float(max(out, floor)))

    # -- main loop ---------------------------------------------------------

    def _advance(self, dt: float) -> None:
        """Advance the simulated plant by *dt* seconds (no I/O)."""
        self._step_index += 1
        self._drain_pending_power_targets()
        self._run_detector_loop(dt)
        self._update_power(dt)
        self._update_soc(dt)

    async def step(self, dt: float | None = None) -> list[str] | None:
        """Execute one simulation iteration with explicit *dt*.

        When *dt* is ``None`` it defaults to :attr:`poll_interval`.
        Unlike :meth:`run`, this does **not** sleep or touch
        ``_last_update`` — it is designed for deterministic test
        stepping, so it awaits the reply rather than detaching it.
        """
        if dt is None:
            dt = self.poll_interval
        self._advance(dt)
        return await self._send_request()

    def _spawn_poll(self) -> None:
        """Send one poll without letting the reply gate the next one."""
        task = asyncio.create_task(self._send_request())
        self._poll_tasks.add(task)
        task.add_done_callback(self._poll_tasks.discard)

    async def run(self) -> None:
        logger.info(
            "Battery %s started (phase=%s, soc=%.0f%%)",
            self.mac,
            self.phase,
            self._soc * 100,
        )
        self._last_update = time.monotonic()
        next_poll = self._last_update
        try:
            while True:
                now = time.monotonic()
                dt = (now - self._last_update) * self.time_scale
                self._last_update = now

                self._advance(dt)
                # The firmware sends on its own cadence and applies whatever
                # comes back — it does not hold the next poll until the CT
                # answers.  Awaiting the reply here would make an unanswered
                # poll (a dedupe drop, a lost datagram) push the whole schedule
                # out by the receive timeout, which the real device never does.
                self._spawn_poll()

                jitter = random.uniform(-0.5, 0.5)
                next_poll += max(0.05, (self.poll_interval + jitter) / self.time_scale)
                # Absolute deadline, so a slow or timed-out exchange cannot
                # accumulate drift into the polling schedule.
                await asyncio.sleep(max(0.0, next_poll - time.monotonic()))
        finally:
            # cancel() only *requests* cancellation; without the await the
            # detached polls never reach their `finally` and their transports
            # leak past shutdown.
            tasks = list(self._poll_tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "mac": self.mac,
            "phase": self.phase,
            "reported_phase": self.reported_phase,
            "power": round(self._current_power),
            "target": round(self._requested_target),
            "applied_target": round(self._target_power),
            "power_update_delay_ticks": self.power_update_delay_ticks,
            "soc": round(self._soc, 4),
            "max_charge": self.max_charge_power,
            "max_discharge": self.max_discharge_power,
            "max_dc_input": self.max_dc_input,
            "dc_input": round(self._dc_input_power),
        }


class _UDPClient(asyncio.DatagramProtocol):
    """Minimal asyncio datagram protocol for a single request/response."""

    def __init__(self) -> None:
        self.received: asyncio.Future[bytes] = (
            asyncio.get_running_loop().create_future()
        )

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if not self.received.done():
            self.received.set_result(data)

    def error_received(self, exc: Exception) -> None:
        if not self.received.done():
            self.received.set_exception(exc)
