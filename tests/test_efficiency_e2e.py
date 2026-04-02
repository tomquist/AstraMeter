"""End-to-end simulation tests for the efficiency optimization feature.

These tests create a full stack: CT002 → BatterySimulators → LoadModel → PowermeterSim,
and verify that the efficiency optimization correctly concentrates power on fewer
batteries at low demand and distributes to all at high demand.
"""

import asyncio

import pytest

from b2500_meter.ct002.ct002 import CT002
from b2500_meter.simulator.battery import BatterySimulator
from b2500_meter.simulator.load_model import Load, LoadModel
from b2500_meter.simulator.powermeter_sim import PowermeterSimulator

# Use a unique port range to avoid conflicts with parallel tests
BASE_CT_PORT = 23456
BASE_HTTP_PORT = 18080


def _find_free_ports(n: int = 2) -> list[int]:
    """Return *n* free UDP port numbers."""
    import socket

    ports: list[int] = []
    socks: list[socket.socket] = []
    for _ in range(n):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:
        s.close()
    return ports


class _SimHarness:
    """Wire CT002 + batteries + powermeter sim for E2E tests."""

    def __init__(
        self,
        num_batteries: int = 2,
        base_load: list[float] | None = None,
        loads: list[Load] | None = None,
        min_efficient_power: int = 0,
        efficiency_rotation_interval: int = 900,
        poll_interval: float = 0.3,
        base_noise: float = 0.0,
        **ct_kwargs,
    ):
        ct_port, http_port = _find_free_ports(2)
        self.ct_port = ct_port
        self.http_port = http_port

        if base_load is None:
            base_load = [200.0, 0.0, 0.0]

        self.load_model = LoadModel(
            base_load=list(base_load),
            base_noise=base_noise,
            loads=[Load(ld.name, ld.power, ld.phase) for ld in (loads or [])],
        )

        ct_mac = "112233445566"
        self.batteries: list[BatterySimulator] = []
        for i in range(num_batteries):
            mac = f"02B250{i + 1:06X}"
            self.batteries.append(
                BatterySimulator(
                    mac=mac,
                    phase="A",
                    ct_mac=ct_mac,
                    ct_host="127.0.0.1",
                    ct_port=ct_port,
                    max_charge_power=800,
                    max_discharge_power=800,
                    initial_soc=0.8,
                    ramp_rate=400.0,  # Fast ramp for quicker test convergence
                    poll_interval=poll_interval,
                    min_power_threshold=5.0,  # Low threshold to observe small targets
                )
            )

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
            smooth_target_alpha=0.9,
            deadband=5,
            min_efficient_power=min_efficient_power,
            efficiency_rotation_interval=efficiency_rotation_interval,
            **ct_kwargs,
        )

        # Wire CT002 to read grid power from the powermeter sim
        async def update_readings(_addr, _fields=None, _consumer_id=None):
            grid = self.powermeter.compute_grid()
            return [grid["phase_a"], grid["phase_b"], grid["phase_c"]]

        self.ct002.before_send = update_readings

    async def start(self):
        await self.powermeter.start()
        await self.ct002.start()
        self._battery_tasks = [asyncio.create_task(b.run()) for b in self.batteries]

    async def stop(self):
        for t in self._battery_tasks:
            t.cancel()
        await asyncio.gather(*self._battery_tasks, return_exceptions=True)
        await self.ct002.stop()
        await self.powermeter.stop()

    async def settle(self, seconds: float = 3.0):
        """Let the control loop run and converge."""
        await asyncio.sleep(seconds)

    def battery_powers(self) -> list[float]:
        """Return current power of each battery."""
        return [b.current_power for b in self.batteries]

    def grid_total(self) -> float:
        grid = self.powermeter.compute_grid()
        return grid["phase_a"] + grid["phase_b"] + grid["phase_c"]

    def active_battery_count(self, threshold: float = 15.0) -> int:
        """Count batteries producing more than `threshold` watts."""
        return sum(1 for p in self.battery_powers() if abs(p) > threshold)


@pytest.fixture
async def harness():
    """Provide a stopped harness; tests configure and start it."""
    h = None
    yield lambda **kwargs: _create_harness(**kwargs)
    if h is not None:
        await h.stop()


def _create_harness(**kwargs) -> _SimHarness:
    return _SimHarness(**kwargs)


class TestEfficiencyE2E:
    """End-to-end tests for efficiency optimization with simulated batteries."""

    @pytest.mark.timeout(30)
    async def test_low_demand_concentrates_power(self):
        """At 200W with 2 batteries and threshold=150, only 1 battery should be active."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.settle(5.0)

            powers = h.battery_powers()
            active = h.active_battery_count(threshold=15.0)

            # Exactly 1 battery should be doing meaningful work
            assert active == 1, (
                f"Expected 1 active battery at 200W demand with threshold=150, "
                f"got {active}. Powers: {powers}"
            )

            # The active battery should be delivering roughly the full demand
            max_power = max(abs(p) for p in powers)
            assert max_power > 100, (
                f"Active battery power {max_power}W is too low. Powers: {powers}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(30)
    async def test_high_demand_uses_all_batteries(self):
        """At 600W with 2 batteries and threshold=150, both should be active."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[600.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.settle(5.0)

            active = h.active_battery_count(threshold=30.0)
            assert active == 2, (
                f"Expected 2 active batteries at 600W demand, "
                f"got {active}. Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(30)
    async def test_demand_increase_activates_second_battery(self):
        """When demand rises from low to high, second battery activates."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[150.0, 0.0, 0.0],
            min_efficient_power=150,
            loads=[Load("BigLoad", 500.0, "A")],
        )
        await h.start()
        try:
            # Low demand: only 1 battery active
            await h.settle(5.0)
            assert h.active_battery_count(threshold=30.0) == 1, (
                f"Low demand: expected 1 active. Powers: {h.battery_powers()}"
            )

            # Toggle big load on → high demand
            h.load_model.toggle_load(1)
            await h.settle(5.0)

            # Both should be active now (650W / 2 = 325W each > 150W)
            assert h.active_battery_count(threshold=30.0) == 2, (
                f"High demand: expected 2 active. Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(30)
    async def test_disabled_feature_uses_all_batteries(self):
        """With min_efficient_power=0 (default), both batteries share load equally."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=0,
        )
        await h.start()
        try:
            await h.settle(8.0)

            active = h.active_battery_count(threshold=15.0)
            assert active == 2, (
                f"With feature disabled, expected 2 active at 200W. "
                f"Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(45)
    async def test_priority_rotation_switches_active_battery(self):
        """After rotation interval, a different battery becomes active."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=5,  # Short interval for testing
            efficiency_fade_alpha=1.0,  # Instant fade so rotation isn't blocked
        )
        await h.start()
        try:
            # Let it settle and identify which battery is active
            await h.settle(4.0)
            powers_before = h.battery_powers()
            active_before = [i for i, p in enumerate(powers_before) if abs(p) > 15]
            assert len(active_before) == 1, (
                f"Expected 1 active before rotation. Powers: {powers_before}"
            )

            # Wait for rotation + settling
            await h.settle(8.0)
            powers_after = h.battery_powers()
            active_after = [i for i, p in enumerate(powers_after) if abs(p) > 15]
            assert len(active_after) == 1, (
                f"Expected 1 active after rotation. Powers: {powers_after}"
            )

            # The active battery should have changed
            assert active_before != active_after, (
                f"Expected rotation: active_before={active_before}, "
                f"active_after={active_after}. "
                f"Powers before={powers_before}, after={powers_after}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(30)
    async def test_grid_converges_near_zero(self):
        """With efficiency optimization, grid import/export should still converge near zero."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.settle(6.0)

            grid = abs(h.grid_total())
            assert grid < 50, (
                f"Grid should converge near zero, got {grid}W. "
                f"Battery powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(30)
    async def test_three_batteries_partial_activation(self):
        """With 3 batteries and 350W demand (threshold=150), 2 should be active."""
        h = _SimHarness(
            num_batteries=3,
            base_load=[350.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.settle(6.0)

            active = h.active_battery_count(threshold=15.0)
            assert active == 2, (
                f"Expected 2 active batteries at 350W with 3 available. "
                f"Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(30)
    async def test_smooth_transition_no_overshoot(self):
        """During demand increase, no single battery should overshoot excessively."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            loads=[Load("BigLoad", 500.0, "A")],
        )
        await h.start()
        try:
            # Low demand: 1 active battery.
            await h.settle(5.0)
            assert h.active_battery_count(threshold=30.0) == 1, (
                f"Low demand: expected 1 active. Powers: {h.battery_powers()}"
            )

            # Toggle load on → high demand (700W).
            h.load_model.toggle_load(1)

            # Sample powers during the transition to check for overshoot.
            max_power_seen = 0.0
            for _ in range(10):
                await asyncio.sleep(0.5)
                for p in h.battery_powers():
                    max_power_seen = max(max_power_seen, abs(p))

            # No single battery should spike above 600W during the
            # transition from 1→2 active batteries at 700W total demand.
            assert max_power_seen < 600, (
                f"Overshoot detected: max battery power {max_power_seen:.0f}W "
                f"during transition. Final powers: {h.battery_powers()}"
            )

            # After settling, both batteries active and grid near zero.
            await h.settle(3.0)
            assert h.active_battery_count(threshold=30.0) == 2, (
                f"High demand: expected 2 active. Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()
