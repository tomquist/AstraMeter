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

from astrameter.ct002.balancer import device_capabilities

from . import protocol
from .b2500_steering import B2500SteeringController
from .firmware_steering import FirmwareSteeringController
from .venus_d_steering import VenusDSteeringController

logger = logging.getLogger("astra_sim.battery")


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
    ) -> None:
        if phase not in protocol.PHASE_FIELD_INDEX:
            raise ValueError(
                f"Invalid phase {phase!r}, must be one of "
                f"{list(protocol.PHASE_FIELD_INDEX)}"
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

        # Self-consumption control law. Most Marstek batteries (Venus class) run
        # the firmware ramp controller on the grid value read back from the CT;
        # ``hi``/``lo`` are the charge / discharge limits in its convention
        # (setpoint positive = charge, negative = discharge).
        self._steering = FirmwareSteeringController()
        self._steer_hi = float(self.max_charge_power)
        self._steer_lo = -float(self.max_discharge_power)

        # The B2500 family (HMA/HMJ/HMK) is DC-coupled with no built-in inverter
        # and no AC input: it steers its DC output into an external microinverter
        # with a different integer hysteresis law instead of the Venus ramp. The
        # unit has two DC output channels, each its own regulator running every
        # cycle; the grid-derived demand is split between them, so the combined
        # output slews at twice a single channel's rate (~34 vs ~17 W/cycle).
        caps = device_capabilities(self.meter_dev_type)
        self._is_dc_output = (
            caps.has_dc_input
            and not caps.has_builtin_inverter
            and not caps.has_ac_input
        )
        # Two channels, each capped at half the unit's discharge envelope.
        _ch_max = max(1, self.max_discharge_power // 2)
        self._b2500_channels = (
            [
                B2500SteeringController(max_output=_ch_max),
                B2500SteeringController(max_output=_ch_max),
            ]
            if self._is_dc_output
            else []
        )

        # Venus D (VNSD-0) is AC-coupled like the rest of the Venus class but
        # runs a different self-consumption loop: an integer proportional
        # integrator rather than the float ramp (see :mod:`venus_d_steering`).
        # Its setpoint convention is the opposite of the ramp controller's —
        # positive = discharge, negative = charge — so ``hi`` is the discharge
        # limit and ``lo`` the (negative) charge limit, and the simulator target
        # is the setpoint *unnegated*.
        self._is_venus_d = (
            self.meter_dev_type.upper().startswith("VNSD") and not self._is_dc_output
        )
        self._venus_d_steering = (
            VenusDSteeringController() if self._is_venus_d else None
        )

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
    def dc_input_power(self) -> float:
        return self._dc_input_power

    @dc_input_power.setter
    def dc_input_power(self, value: float) -> None:
        self._dc_input_power = max(0.0, min(float(self.max_dc_input), float(value)))

    def _apply_ct_derived_target(self, new_target: float) -> None:
        """Record CT request immediately; apply to physics after *power_update_delay_ticks*."""
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
        phase_field = "0" if self._request_count < self.inspection_count else self.phase
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
        except (TimeoutError, OSError) as exc:
            logger.debug("Battery %s: request failed: %s", self.mac, exc)
            return None
        finally:
            if transport is not None:
                transport.close()

        self._request_count += 1

        response_fields, err = protocol.parse_message(data)
        if err:
            logger.debug("Battery %s: bad response: %s", self.mac, err)
            return None

        # Hand parsed response off to the deterministic helper so it can
        # also be unit-tested without UDP I/O.
        if response_fields and phase_field != "0":
            self._handle_ct_response(response_fields)

        return response_fields

    def _handle_ct_response(self, response_fields: list[str]) -> None:
        """Derive the new AC target from the grid value read back from the CT.

        The grid value (sum of the per-phase power fields, positive = importing)
        is fed to this battery's steering controller. Venus-class batteries run
        :class:`FirmwareSteeringController` (a ramp law with input-conditioning
        gates), whose sign is the inverse of the simulator's (setpoint positive =
        charge), so the simulator target is the negated setpoint. A DC-coupled
        B2500 instead runs :class:`B2500SteeringController` on its DC output (see
        :meth:`_steer_b2500_output`). A Venus D (VNSD-0) runs
        :class:`VenusDSteeringController`, an integer integrator whose setpoint is
        already in the simulator's sign (positive = discharge), applied directly.

        Cross-battery share-split: a real battery divides the grid value by the
        number of batteries reported on its phase (the ``*_chrg_nb`` count), so
        several batteries on one phase each take their share rather than all
        chasing the full residual. This matters in relay mode / against a real
        CT; AstraMeter's active-control emulator distributes per-battery targets
        itself and reports a count of 1, so the split is a no-op there.
        """

        def field(idx: int) -> int:
            try:
                return int(response_fields[idx])
            except (IndexError, ValueError, TypeError):
                return 0

        grid_reading = field(4) + field(5) + field(6)

        if self._b2500_channels:
            self._steer_b2500_output(grid_reading)
            return

        if self._venus_d_steering is not None:
            # Venus D: integer integrator, positive setpoint = discharge. Its own
            # grid reading (used only for the per-step branch) tracks the CT
            # value in this closed loop. ±15 W deadband in combined (phase D)
            # mode, ±11 W otherwise.
            vd_setpoint = self._venus_d_steering.step(
                grid_reading,
                float(self.max_discharge_power),
                -float(self.max_charge_power),
                measured_grid=grid_reading,
                phase_count=2 if self.phase == "D" else 1,
            )
            self._apply_ct_derived_target(float(vd_setpoint))
            return

        # *_chrg_nb for this battery's phase (fields 9/10/11 → indices 8/9/10).
        phase_count = field(8 + "ABC".index(self.phase))

        setpoint = self._steering.step(
            grid_reading,
            self._steer_hi,
            self._steer_lo,
            device_count=phase_count,
            out=self._current_power,
        )
        # Controller: +charge → simulator: +discharge.
        self._apply_ct_derived_target(-setpoint)

    def _steer_b2500_output(self, grid_reading: int) -> None:
        """Steer a DC-coupled B2500's two output channels toward nulling the grid.

        The B2500 can only discharge its DC output into an external microinverter
        to offset the grid — it has no AC input and never charges from AC. The
        setpoint is **incremental** (current output plus 90% of the residual
        grid), so the loop integrates the grid toward zero rather than parking at
        a fraction of the load; it is floored at 0 (a surplus winds the output
        down to idle), capped at the discharge envelope, follows the B2500's
        integer hysteresis regulator rather than the Venus ramp, and is split
        evenly across the two output channels (each regulating against its own
        ~half of the measured output), so the combined output slews at ~twice a
        single channel's rate.
        """
        cur = max(0, round(self._current_power))
        target = cur + grid_reading * 9 // 10  # incremental: 90% of the residual
        target = max(0, min(target, self.max_discharge_power))
        # At full SoC the PV passes straight through to the output and cannot be
        # curtailed below the DC input (the pack is full, the PV has nowhere else
        # to go). The steering can't drive the output under that floor, so don't
        # let it try — otherwise it fights the passthrough override and the
        # output oscillates instead of settling at the PV level.
        if self._soc >= 1.0 and self._dc_input_power > 0:
            target = max(target, round(self._dc_input_power))
        per_channel = target // 2
        own = cur // 2  # each channel's ~half of the measured output
        out = sum(ch.regulate(per_channel, own) for ch in self._b2500_channels)
        self._apply_ct_derived_target(float(out))

    # -- main loop ---------------------------------------------------------

    async def step(self, dt: float | None = None) -> list[str] | None:
        """Execute one simulation iteration with explicit *dt*.

        When *dt* is ``None`` it defaults to :attr:`poll_interval`.
        Unlike :meth:`run`, this does **not** sleep or touch
        ``_last_update`` — it is designed for deterministic test
        stepping.
        """
        if dt is None:
            dt = self.poll_interval
        self._step_index += 1
        self._drain_pending_power_targets()
        self._update_power(dt)
        self._update_soc(dt)
        return await self._send_request()

    async def run(self) -> None:
        logger.info(
            "Battery %s started (phase=%s, soc=%.0f%%)",
            self.mac,
            self.phase,
            self._soc * 100,
        )
        self._last_update = time.monotonic()
        while True:
            now = time.monotonic()
            dt = (now - self._last_update) * self.time_scale
            self._last_update = now

            await self.step(dt)

            jitter = random.uniform(-0.5, 0.5)
            await asyncio.sleep(
                max(0.05, (self.poll_interval + jitter) / self.time_scale)
            )

    # -- serialisation -----------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "mac": self.mac,
            "phase": self.phase,
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
