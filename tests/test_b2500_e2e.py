"""End-to-end: a B2500 (HMJ) ``BatterySimulator`` steers its DC output through
the real CT002 emulator to null the grid, and never charges from AC on a surplus.

Drives the Python ``BatterySimulator`` (DC-output hysteresis steering) against
the in-process CT002 emulator + powermeter, closing the loop through the wire.
This is the integration counterpart to the unit/golden tests in
``b2500_steering_test.py``: it catches loop-level regressions (e.g. a proportional
setpoint that droops instead of nulling the grid).
"""

from __future__ import annotations

import pytest
from _ct002_e2e_backend import HarnessClock, find_free_ports

from astrameter.ct002.ct002 import CT002
from astrameter.simulator.battery import BatterySimulator
from astrameter.simulator.load_model import LoadModel
from astrameter.simulator.powermeter_sim import PowermeterSimulator


class _B2500Harness:
    """One B2500 battery on phase A against a phase-A load, via the Python CT002."""

    def __init__(self, load_a: float, active_control: bool = True) -> None:
        self.clock = HarnessClock()
        free_udp, http_port = find_free_ports(2)
        ct_mac = "112233445566"
        self.powermeter = PowermeterSimulator(
            batteries=[],
            load_model=LoadModel(
                base_load=[load_a, 0.0, 0.0], base_noise=0.0, loads=[]
            ),
            host="127.0.0.1",
            port=http_port,
        )
        self.battery = BatterySimulator(
            mac="02B250000001",
            phase="A",
            ct_mac=ct_mac,
            ct_host="127.0.0.1",
            ct_port=free_udp,
            meter_dev_type="HMJ-2",  # B2500 family → DC-output steering
            max_charge_power=800,
            max_discharge_power=800,
            initial_soc=0.8,
            ramp_rate=400.0,
            poll_interval=0.3,
            min_power_threshold=5.0,
            startup_delay=0.0,
            inspection_count=0,
        )
        self.powermeter.batteries.append(self.battery)
        self.ct002 = CT002(
            udp_port=free_udp,
            ct_mac=ct_mac,
            active_control=active_control,
            fair_distribution=True,
            min_efficient_power=0,
            clock=self.clock,
            reset_fn=None,
            consumer_ttl=100000,
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
            await self.battery.step(self.battery.poll_interval)
            self.clock.advance(self.battery.poll_interval)

    def grid(self) -> float:
        g = self.powermeter.compute_grid()
        return g["phase_a"] + g["phase_b"] + g["phase_c"]


@pytest.mark.parametrize("active_control", [True, False])
async def test_b2500_nulls_grid_end_to_end(active_control: bool) -> None:
    """A B2500 ramps its DC output up to offset the import, driving the grid into
    the deadband — in both active-control and relay mode."""
    h = _B2500Harness(load_a=300.0, active_control=active_control)
    await h.start()
    try:
        await h.step(120)
        assert h.battery.current_power > 250  # discharging to ~offset the load
        assert abs(h.grid()) < 30  # grid nulled, not parked at ~47%
    finally:
        await h.stop()


async def test_b2500_does_not_charge_on_surplus_end_to_end() -> None:
    """With no AC input, a B2500 idles on a grid surplus rather than charging."""
    h = _B2500Harness(load_a=-400.0)  # 400 W of export
    await h.start()
    try:
        await h.step(120)
        assert 0 <= h.battery.current_power <= 30  # idle, never negative
    finally:
        await h.stop()
