"""Async Marstek B2500 battery simulator.

Speaks the CT002 UDP protocol, sends periodic requests to the CT002
emulator, receives per-phase power targets, and adjusts its simulated
output accordingly.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

from . import protocol

logger = logging.getLogger("b2500_sim.battery")


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

        self._current_power: float = 0.0
        self._soc: float = max(0.0, min(1.0, initial_soc))
        self._target_power: float = 0.0
        self._request_count: int = 0
        self._last_update: float = time.monotonic()
        self._startup_elapsed: float = 0.0

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

    def _update_soc(self, dt: float) -> None:
        if self.capacity_wh <= 0:
            return
        energy_wh = self._current_power * (dt / 3600.0)
        self._soc -= energy_wh / self.capacity_wh
        self._soc = max(0.0, min(1.0, self._soc))

    # -- protocol ----------------------------------------------------------

    async def _send_request(self) -> list[str] | None:
        phase_field = "0" if self._request_count < self.inspection_count else self.phase

        fields = [
            self.meter_dev_type,
            self.mac,
            self.ct_dev_type,
            self.ct_mac,
            phase_field,
            str(round(self._current_power)),
        ]
        payload = protocol.build_payload(fields)

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

        # Extract grid reading: sum of all three phase power fields (4, 5, 6).
        # Real B2500 batteries treat this as the current grid consumption and
        # adjust their output to compensate: if the grid is importing 70W, the
        # battery increases its output by 70W.  This integral behavior drives
        # the grid reading toward zero over successive cycles.
        if response_fields and phase_field != "0":
            try:
                phase_a = int(response_fields[4]) if len(response_fields) > 4 else 0
                phase_b = int(response_fields[5]) if len(response_fields) > 5 else 0
                phase_c = int(response_fields[6]) if len(response_fields) > 6 else 0
                grid_reading = phase_a + phase_b + phase_c
                self._target_power = self._current_power + grid_reading
            except (ValueError, TypeError):
                pass

        return response_fields

    # -- main loop ---------------------------------------------------------

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

            self._update_power(dt)
            self._update_soc(dt)

            await self._send_request()

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
            "target": round(self._target_power),
            "soc": round(self._soc, 4),
            "max_charge": self.max_charge_power,
            "max_discharge": self.max_discharge_power,
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
