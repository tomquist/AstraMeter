"""Regression test for GitHub issue #376.

A Venus D-like battery (full SoC, PV passthrough → AC) on phase A reports
positive ``power`` to the CT002 emulator while a Venus E-like battery on
phase C is charging.  Real Marstek firmware reads ``*_dchrg_power`` from
the CT002 response and idles a charging battery when another phase shows
discharge — that's the user-visible bug: Venus E stops charging the
moment Venus D enables "feed excess to grid."

This test wires a deterministic CT002 + two simulated batteries + a
powermeter showing strong household export.  It verifies that AstraMeter
keeps instructing Venus E to charge across many ticks (i.e. it does not
broadcast Venus D's passthrough as a discharge signal).
"""

from __future__ import annotations

import socket
import time

from astrameter.ct002.ct002 import CT002
from astrameter.simulator.battery import BatterySimulator
from astrameter.simulator.load_model import LoadModel
from astrameter.simulator.powermeter_sim import PowermeterSimulator


class _FakeClock:
    def __init__(self) -> None:
        self._now = time.time()

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _find_free_ports(n: int) -> list[int]:
    types = [socket.SOCK_DGRAM] + [socket.SOCK_STREAM] * (n - 1)
    ports: list[int] = []
    socks: list[socket.socket] = []
    for i in range(n):
        s = socket.socket(socket.AF_INET, types[i])
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:
        s.close()
    return ports


class _Issue376Harness:
    """Two-battery harness: Venus D-like (PV passthrough) + Venus E-like.

    - Venus D on phase A, SoC=1.0, ``max_dc_input=500``, dc_input_power=500.
    - Venus E on phase C, SoC=0.5, charging.
    - Powermeter shows strong household export on all phases.
    - Both batteries enable ``idle_on_cross_phase_discharge`` so the
      simulator mirrors the Marstek firmware reaction this issue is about.
    """

    def __init__(self) -> None:
        ct_port, http_port = _find_free_ports(2)
        self.ct_port = ct_port
        self.http_port = http_port
        self.clock = _FakeClock()

        # Strong export on all phases (mirrors the Tasmota readings in the
        # log attached to issue #376: l1=-7480, l2=-7820, l3=-3933).
        # Use base_load < 0 since LoadModel adds base_load and battery output
        # to derive grid contribution; negative base_load means the meter
        # sees export even before batteries kick in.
        self.load_model = LoadModel(
            base_load=[-7000.0, -7800.0, -3900.0],
            base_noise=0.0,
            loads=[],
        )

        ct_mac = "112233445566"
        self.venus_d = BatterySimulator(
            mac="02B250000001",
            phase="A",
            ct_mac=ct_mac,
            ct_host="127.0.0.1",
            ct_port=ct_port,
            max_charge_power=800,
            max_discharge_power=800,
            initial_soc=1.0,
            ramp_rate=400.0,
            poll_interval=0.3,
            min_power_threshold=5.0,
            startup_delay=0.0,
            inspection_count=0,
            max_dc_input=500,
            dc_input_power=500.0,
            idle_on_cross_phase_discharge=True,
        )
        self.venus_e = BatterySimulator(
            mac="02B250000002",
            phase="C",
            ct_mac=ct_mac,
            ct_host="127.0.0.1",
            ct_port=ct_port,
            max_charge_power=2500,
            max_discharge_power=2500,
            initial_soc=0.5,
            ramp_rate=400.0,
            poll_interval=0.3,
            min_power_threshold=5.0,
            startup_delay=0.0,
            inspection_count=0,
            idle_on_cross_phase_discharge=True,
        )
        self.batteries = [self.venus_d, self.venus_e]

        self.powermeter = PowermeterSimulator(
            batteries=self.batteries,
            load_model=self.load_model,
            host="127.0.0.1",
            port=http_port,
        )

        self.ct002 = CT002(
            udp_port=ct_port,
            ct_mac=ct_mac,
            active_control=True,
            fair_distribution=True,
            min_efficient_power=0,
            clock=self.clock,
            reset_fn=None,
        )

        async def update_readings(_addr, _fields=None, _consumer_id=None):
            grid = self.powermeter.compute_grid()
            return [grid["phase_a"], grid["phase_b"], grid["phase_c"]]

        self.ct002.before_send = update_readings

    async def start(self) -> None:
        await self.powermeter.start()
        await self.ct002.start()

    async def stop(self) -> None:
        await self.ct002.stop()
        await self.powermeter.stop()

    async def step(self, n: int = 1) -> None:
        for _ in range(n):
            max_dt = max(b.poll_interval for b in self.batteries)
            for b in self.batteries:
                await b.step(b.poll_interval)
            self.clock.advance(max_dt)


async def test_venus_e_keeps_charging_during_venus_d_pv_passthrough() -> None:
    h = _Issue376Harness()
    await h.start()
    try:
        # Warm-up: let inspection mode complete (n=0 so this is fast) and
        # let the balancer settle.
        await h.step(40)

        # Track Venus E's power over the final 10 ticks.
        venus_e_powers: list[float] = []
        for _ in range(10):
            await h.step(1)
            venus_e_powers.append(h.venus_e.current_power)

        avg_venus_e = sum(venus_e_powers) / len(venus_e_powers)

        # 1. Primary assertion: Venus E is still charging hard, not idle.
        #    Before the fix the firmware-mimic flag drives this to ~0
        #    once Venus D's passthrough lands in A_dchrg_power.
        assert avg_venus_e < -500.0, (
            f"Venus E should be charging (current_power << 0) despite Venus D's "
            f"PV passthrough; got avg={avg_venus_e:.0f}, samples={venus_e_powers}"
        )

        # 2. A_dchrg_power in CT002 state must be 0 — Venus D's positive
        #    output must not be broadcast as a discharge signal.
        by_phase = h.ct002._collect_reports_by_phase()
        assert by_phase["A"]["dchrg_power"] == 0, (
            f"A_dchrg_power should be 0 (Venus D was instructed to charge); "
            f"got {by_phase}"
        )

        # 3. Sanity: Venus D *was* instructed to charge.
        venus_d_consumer = h.ct002._consumers.get(h.venus_d.mac.lower())
        assert venus_d_consumer is not None
        assert venus_d_consumer.last_instructed_power < 0.0, (
            f"Venus D should have been instructed to charge "
            f"(negative target on phase A); got "
            f"last_instructed_power={venus_d_consumer.last_instructed_power}"
        )

        # 4. Sanity: Venus D is in fact passing PV through to AC.
        assert h.venus_d.current_power > 0, (
            f"Venus D should be doing PV passthrough; got "
            f"current_power={h.venus_d.current_power}"
        )
    finally:
        await h.stop()
