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


class _MixedHarness:
    """A Venus (HMG-50) and a B2500 (HMJ), both on phase A. Defaults to **relay
    mode** (``ACTIVE_CONTROL = False``) — each battery steers itself off the raw
    grid the CT forwards. They use different control laws, so this exercises
    whether the two together still drive the shared grid to zero. The B2500 can
    optionally carry PV (``b2500_pv``) at a given SoC to exercise passthrough."""

    def __init__(
        self,
        active_control: bool = False,
        b2500_pv: float = 0.0,
        b2500_soc: float = 0.5,
    ) -> None:
        self.clock = HarnessClock()
        free_udp, http_port = find_free_ports(2)
        ct_mac = "112233445566"
        self.load_model = LoadModel(base_load=[0.0, 0.0, 0.0], base_noise=0.0, loads=[])
        self.powermeter = PowermeterSimulator(
            batteries=[], load_model=self.load_model, host="127.0.0.1", port=http_port
        )

        def mk(mac: str, dev: str, initial_soc: float = 0.5, **kw) -> BatterySimulator:
            return BatterySimulator(
                mac=mac,
                phase="A",
                ct_mac=ct_mac,
                ct_host="127.0.0.1",
                ct_port=free_udp,
                meter_dev_type=dev,
                max_charge_power=800,
                max_discharge_power=800,
                initial_soc=initial_soc,
                ramp_rate=400.0,
                poll_interval=0.3,
                min_power_threshold=5.0,
                startup_delay=0.0,
                inspection_count=0,
                **kw,
            )

        self.venus = mk("02B250000001", "HMG-50")
        self.b2500 = mk(
            "02B250000002",
            "HMJ-2",
            initial_soc=b2500_soc,
            max_dc_input=int(b2500_pv),
            dc_input_power=b2500_pv,
        )
        self.powermeter.batteries.extend([self.venus, self.b2500])
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

    async def settle(self, n: int = 150) -> None:
        for _ in range(n):
            for b in (self.venus, self.b2500):
                await b.step(b.poll_interval)
            self.clock.advance(self.venus.poll_interval)

    def grid(self) -> float:
        g = self.powermeter.compute_grid()
        return g["phase_a"] + g["phase_b"] + g["phase_c"]


async def test_venus_and_b2500_null_grid_in_relay_mode() -> None:
    """In relay mode a Venus + a B2500 each steer themselves off the raw grid;
    after **every** grid change they together drive the shared grid back to ~0
    (imports, exports, and back to no load)."""
    h = _MixedHarness()
    await h.start()
    try:
        for load in (400.0, 800.0, -300.0, 200.0, 0.0):
            h.load_model.base_load = [load, 0.0, 0.0]
            await h.settle()
            assert abs(h.grid()) < 25, (
                f"grid not nulled at load={load}: grid={h.grid():.1f}, "
                f"venus={h.venus.current_power:.0f}, b2500={h.b2500.current_power:.0f}"
            )
    finally:
        await h.stop()


async def test_mixed_surplus_only_venus_absorbs() -> None:
    """On a grid surplus only the Venus can charge (absorb it); the B2500 has no
    AC input and **never charges**. Together they null the grid up to the Venus's
    charge capacity; a surplus beyond it is physically exported, not absorbed."""
    h = _MixedHarness()
    await h.start()
    try:
        # Moderate surplus, within the Venus's charge capacity: grid nulls. The
        # Venus absorbs it (charges); the B2500 never charges, though it may hold
        # a small discharge in the circulating equilibrium (always >= 0).
        h.load_model.base_load = [-400.0, 0.0, 0.0]
        await h.settle(200)
        assert h.b2500.current_power >= -1  # never charges from AC
        assert h.venus.current_power < -250  # Venus absorbs the surplus
        assert abs(h.grid()) < 30  # grid nulled

        # Surplus beyond the absorb capacity: the Venus caps near its charge
        # limit, the B2500 still never charges, and the excess remains on the
        # grid as export — a physical limit (nothing here can absorb it).
        h.load_model.base_load = [-1000.0, 0.0, 0.0]
        await h.settle(200)
        assert h.b2500.current_power >= -1  # still never charges
        assert h.venus.current_power < -650  # charging near its limit
        assert h.grid() < -150  # residual export, cannot be nulled
    finally:
        await h.stop()


@pytest.mark.parametrize("active_control", [True, False])
async def test_b2500_full_soc_passthrough_absorbed_by_venus(
    active_control: bool,
) -> None:
    """A full B2500 with surplus PV passes it through to its DC output (it can't
    store it). That output settles at the PV level — the steering can't curtail
    below the passthrough — and a co-resident Venus absorbs the exported surplus,
    so the grid still nulls. (The DC analog of the issue #376 passthrough.)"""
    # 500 W PV, full SoC; only 100 W local load, so ~400 W of PV is surplus.
    h = _MixedHarness(active_control=active_control, b2500_pv=500.0, b2500_soc=1.0)
    h.load_model.base_load = [100.0, 0.0, 0.0]
    await h.start()
    try:
        await h.settle(300)
        # B2500 outputs ~its PV (passthrough floor); it never sinks below it and
        # has not wound up far above it (no integrator runaway).
        assert 470 <= h.b2500.current_power <= 560
        assert h.venus.current_power < -300  # Venus absorbs the exported surplus
        assert abs(h.grid()) < 30  # grid nulled despite the passthrough
    finally:
        await h.stop()
