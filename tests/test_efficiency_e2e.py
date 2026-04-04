"""End-to-end simulation tests for the efficiency optimization feature.

These tests create a full stack: CT002 → BatterySimulators → LoadModel → PowermeterSim,
and verify that the efficiency optimization correctly concentrates power on fewer
batteries at low demand and distributes to all at high demand.
"""

from __future__ import annotations

import asyncio
import math

import pytest

from b2500_meter.ct002.ct002 import CT002
from b2500_meter.simulator.battery import BatterySimulator
from b2500_meter.simulator.load_model import Load, LoadModel
from b2500_meter.simulator.powermeter_sim import PowermeterSimulator

# Use a unique port range to avoid conflicts with parallel tests
BASE_CT_PORT = 23456
BASE_HTTP_PORT = 18080


def _percentile(samples: list[float], p: float) -> float:
    """Return the p-th percentile (0-100) of samples (nearest-rank)."""
    s = sorted(samples)
    k = max(0, min(len(s) - 1, math.ceil(len(s) * p / 100) - 1))
    return s[k]


def _find_free_ports(n: int = 2) -> list[int]:
    """Return *n* free port numbers (first UDP, rest TCP)."""
    import socket

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
        poll_intervals: list[float] | None = None,
        base_noise: float = 0.0,
        startup_delay: float = 2.0,
        startup_delays: list[float] | None = None,
        min_power_threshold: float = 5.0,
        min_power_thresholds: list[float] | None = None,
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
            battery_poll_interval = (
                poll_intervals[i] if poll_intervals is not None else poll_interval
            )
            battery_startup_delay = (
                startup_delays[i] if startup_delays is not None else startup_delay
            )
            battery_min_power_threshold = (
                min_power_thresholds[i]
                if min_power_thresholds is not None
                else min_power_threshold
            )
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
                    poll_interval=battery_poll_interval,
                    min_power_threshold=battery_min_power_threshold,
                    startup_delay=battery_startup_delay,  # Mimic real inverter warm-up from idle
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

    def active_battery_indexes(self, threshold: float = 15.0) -> list[int]:
        return [i for i, p in enumerate(self.battery_powers()) if abs(p) > threshold]

    async def wait_active(
        self, expected: int, *, threshold: float = 15.0, timeout: float = 20.0
    ) -> None:
        """Poll until exactly *expected* batteries are active, or fail."""
        poll = 0.3
        elapsed = 0.0
        while elapsed < timeout:
            if self.active_battery_count(threshold) == expected:
                return
            await asyncio.sleep(poll)
            elapsed += poll
        powers = self.battery_powers()
        raise AssertionError(
            f"Expected {expected} active batteries (threshold={threshold}), "
            f"got {self.active_battery_count(threshold)}. Powers: {powers}"
        )


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

    @pytest.mark.timeout(90)
    async def test_low_demand_concentrates_power(self):
        """At 200W with 2 batteries and threshold=150, only 1 battery should be active."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.wait_active(1)
            # Let it ramp to full power
            await h.settle(3.0)

            # The active battery should be delivering roughly the full demand
            max_power = max(abs(p) for p in h.battery_powers())
            assert max_power > 100, (
                f"Active battery power {max_power}W is too low. Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_high_demand_uses_all_batteries(self):
        """At 600W with 2 batteries and threshold=150, both should be active."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[600.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.wait_active(2, threshold=30.0)
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
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
            await h.wait_active(1, threshold=30.0)

            # Toggle big load on → high demand
            h.load_model.toggle_load(1)

            # Both should be active now (650W / 2 = 325W each > 150W)
            await h.wait_active(2, threshold=30.0)
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_disabled_feature_uses_all_batteries(self):
        """With min_efficient_power=0 (default), both batteries share load equally."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=0,
        )
        await h.start()
        try:
            await h.wait_active(2)
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_priority_rotation_switches_active_battery(self):
        """After rotation interval, the other battery joins via probe."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=7,  # Short interval for testing
            efficiency_fade_alpha=1.0,  # Instant fade so rotation isn't blocked
        )
        await h.start()
        try:
            # Poll until exactly 1 battery is active (startup + saturation swap).
            timeout = 20.0
            poll_interval = 0.3
            elapsed = 0.0
            active_before: list[int] = []
            while elapsed < timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                powers_before = h.battery_powers()
                active_before = [i for i, p in enumerate(powers_before) if abs(p) > 15]
                if len(active_before) == 1:
                    break
            assert len(active_before) == 1, (
                f"Expected 1 active before rotation. Powers: {h.battery_powers()}"
            )

            await h.settle(3.0)
            h.ct002._balancer._last_rotation -= (
                h.ct002._balancer._cfg.efficiency_rotation_interval + 1.0
            )
            timeout = 10.0
            poll_interval = 0.3
            elapsed = 0.0
            powers_after = powers_before
            standby_idx = 1 - active_before[0]
            probe_joined = False
            while elapsed < timeout:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                powers_after = h.battery_powers()
                if abs(powers_after[standby_idx]) > 15:
                    probe_joined = True
                    break
            assert probe_joined, (
                f"Rotation should start a probe handoff on the other battery within {timeout}s. "
                f"active_before={active_before}, last powers={powers_after}"
            )
            assert abs(h.grid_total()) < 160, (
                f"Grid should stay reasonably stable during timed probe rotation. "
                f"Grid={h.grid_total():.0f}W powers={h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_probe_keeps_grid_near_zero_during_slow_rotation(self):
        """During a slow probe, the previous battery keeps covering demand."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=7,
            startup_delay=6.0,
            efficiency_fade_alpha=1.0,
        )
        await h.start()
        try:
            await h.wait_active(1)
            powers_before = h.battery_powers()
            active_before = 0 if abs(powers_before[0]) > abs(powers_before[1]) else 1
            standby = 1 - active_before

            h.ct002._balancer._last_rotation -= 8

            # Allow the probe initiation transient to settle
            await asyncio.sleep(1.0)

            grid_errors: list[float] = []
            backup_powers: list[float] = []
            max_probe_power = 0.0
            for _ in range(12):
                await asyncio.sleep(0.3)
                powers = h.battery_powers()
                grid_errors.append(abs(h.grid_total()))
                backup_powers.append(abs(powers[active_before]))
                max_probe_power = max(max_probe_power, abs(powers[standby]))

            median_backup = _percentile(backup_powers, 50)
            assert median_backup > 120, (
                f"Previous battery should remain online during probe. "
                f"Median={median_backup:.0f}W Powers: {h.battery_powers()}"
            )
            assert max_probe_power < 40, (
                "Promoted battery should still be in startup delay during the probe window. "
                f"Powers: {h.battery_powers()}"
            )
            p90_grid = _percentile(grid_errors, 90)
            assert p90_grid < 70, (
                f"Grid should stay near zero during probe, p90={p90_grid:.0f}W. "
                f"Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_probe_handles_mixed_poll_intervals(self):
        """Residual backup coverage tolerates probe lag from slower polling."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=7,
            poll_intervals=[0.9, 0.3],
            startup_delays=[6.0, 6.0],
            efficiency_fade_alpha=1.0,
        )
        await h.start()
        try:
            await h.wait_active(1)
            active_before = h.active_battery_indexes()[0]
            standby = 1 - active_before

            h.ct002._balancer._last_rotation -= 8

            grid_errors: list[float] = []
            for _ in range(10):
                await asyncio.sleep(0.5)
                grid_errors.append(abs(h.grid_total()))

            assert abs(h.battery_powers()[active_before]) > 100, (
                "Previous battery should still cover most of the demand while the "
                f"promoted battery is probing. Powers: {h.battery_powers()}"
            )
            assert abs(h.battery_powers()[standby]) < 100, (
                f"Promoted battery should still be ramping slowly. Powers: {h.battery_powers()}"
            )
            p90_grid = _percentile(grid_errors, 90)
            assert p90_grid < 90, (
                f"Mixed poll intervals should not blow up grid error during probe (p90={p90_grid:.0f}W). "
                f"Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_probe_acceptance_avoids_large_export_spike(self):
        """Successful probe handoff should not temporarily double total output."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=7,
            startup_delay=2.0,
            efficiency_fade_alpha=1.0,
        )
        await h.start()
        try:
            await h.wait_active(1)
            active_before = h.active_battery_indexes()[0]
            standby = 1 - active_before
            h.ct002._balancer._last_rotation -= 8

            total_outputs: list[float] = []
            grid_errors: list[float] = []
            probe_accepted = False
            for _ in range(24):
                await asyncio.sleep(0.5)
                powers = h.battery_powers()
                total_outputs.append(sum(max(p, 0.0) for p in powers))
                grid_errors.append(abs(h.grid_total()))
                if abs(powers[standby]) > 15:
                    probe_accepted = True

            assert probe_accepted, (
                f"Expected promoted battery to join. Powers: {h.battery_powers()}"
            )
            # Use 90th percentile to tolerate transient spikes from poll-timing jitter
            p90_output = _percentile(total_outputs, 90)
            p90_grid = _percentile(grid_errors, 90)
            assert p90_output < 380, (
                f"Probe acceptance should not double output. p90 total was {p90_output:.0f}W; "
                f"powers={h.battery_powers()}"
            )
            assert p90_grid < 130, (
                f"Probe acceptance should keep grid reasonably stable; p90 error {p90_grid:.0f}W. "
                f"powers={h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_probe_respects_80w_inverter_floor(self):
        """Probe should use a meaningful command when batteries ignore tiny targets."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            probe_min_power=80,
            efficiency_rotation_interval=7,
            startup_delay=0.0,
            min_power_thresholds=[80.0, 80.0],
            efficiency_fade_alpha=1.0,
        )
        await h.start()
        try:
            await h.wait_active(1, threshold=80.0)
            await h.settle(3.0)
            active_before = h.active_battery_indexes(threshold=80.0)[0]
            standby = 1 - active_before
            h.ct002._balancer._last_rotation -= 8

            probe_joined = False
            grid_errors: list[float] = []
            for _ in range(16):
                await asyncio.sleep(0.5)
                powers = h.battery_powers()
                grid_errors.append(abs(h.grid_total()))
                if abs(powers[standby]) >= 70:
                    probe_joined = True
                    break

            assert probe_joined, (
                "Probe should use enough command to clear an 80W inverter floor. "
                f"Powers: {h.battery_powers()}"
            )
            # Use 90th percentile to tolerate transient spikes from poll-timing jitter
            p90_grid = _percentile(grid_errors, 90)
            assert p90_grid < 160, (
                f"80W probe floor should not destabilize the grid excessively; "
                f"p90={p90_grid:.0f}W. Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_probe_rejection_keeps_backup_covering_demand(self):
        """Rejected probe should not create a noticeable demand gap."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_rotation_interval=7,
            startup_delay=0.0,
            efficiency_fade_alpha=1.0,
            saturation_grace_seconds=5.0,
        )
        await h.start()
        try:
            await h.wait_active(1)
            await h.settle(3.0)
            active_before = h.active_battery_indexes()[0]
            standby = 1 - active_before
            h.batteries[standby].startup_delay = 20.0
            h.ct002._balancer._last_rotation -= 8

            grid_errors: list[float] = []
            large_gap_samples = 0
            for _ in range(16):
                await asyncio.sleep(0.5)
                grid = abs(h.grid_total())
                grid_errors.append(grid)
                if grid > 100:
                    large_gap_samples += 1

            assert abs(h.battery_powers()[standby]) < 25, (
                f"Promoted battery should still be rejected as a slow/stuck probe. Powers: {h.battery_powers()}"
            )
            assert large_gap_samples <= 1, (
                "Rejected probe should not leave the grid under-covered for multiple samples. "
                f"max_grid={max(grid_errors):.0f}W powers={h.battery_powers()}"
            )
            # Use 90th percentile to tolerate transient spikes from poll-timing jitter
            p90_grid = _percentile(grid_errors, 90)
            assert p90_grid < 70, (
                f"Rejected probe should not leave a large grid gap; p90 error {p90_grid:.0f}W. "
                f"Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_grid_converges_near_zero(self):
        """With efficiency optimization, grid import/export should still converge near zero."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.wait_active(1)
            # Additional settle for grid convergence after battery is active
            for _ in range(40):
                await asyncio.sleep(0.3)
                if abs(h.grid_total()) < 50:
                    break
            grid = abs(h.grid_total())
            assert grid < 50, (
                f"Grid should converge near zero, got {grid}W. "
                f"Battery powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_three_batteries_partial_activation(self):
        """With 3 batteries and 350W demand (threshold=150), 2 should be active."""
        h = _SimHarness(
            num_batteries=3,
            base_load=[350.0, 0.0, 0.0],
            min_efficient_power=150,
        )
        await h.start()
        try:
            await h.wait_active(2)
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
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
            await h.wait_active(1, threshold=30.0)

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
            await h.wait_active(2, threshold=30.0)
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_saturated_battery_triggers_rotation(self):
        """When the active battery is saturated, it gets swapped out quickly."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
            saturation_stall_timeout_seconds=4.0,
        )
        await h.start()
        try:
            await h.wait_active(1)
            # Identify which battery is active and saturate it
            powers = h.battery_powers()
            active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1
            other_idx = 1 - active_idx
            h.batteries[active_idx].max_charge_power = 0
            h.batteries[active_idx].max_discharge_power = 0
            # Poll for saturation detection + forced swap + ramp-up
            for _ in range(40):
                await asyncio.sleep(0.5)
                if abs(h.battery_powers()[other_idx]) > 50:
                    break
            assert abs(h.battery_powers()[other_idx]) > 50, (
                f"Expected other battery to take over. Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_initially_empty_battery_swaps_without_timed_rotation(self):
        """An empty prioritized battery should be swapped out before timed rotation."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_rotation_interval=9999,
            efficiency_saturation_threshold=0.4,
            saturation_stall_timeout_seconds=4.0,
        )
        h.batteries[0].soc = 0.0
        h.batteries[1].soc = 1.0
        await h.start()
        try:
            for _ in range(60):
                await asyncio.sleep(0.5)
                if abs(h.battery_powers()[1]) > 50:
                    break
            assert abs(h.battery_powers()[1]) > 50, (
                "Healthy battery should take over without waiting for timed rotation. "
                f"Powers: {h.battery_powers()}"
            )
            assert abs(h.battery_powers()[0]) < 20, (
                "Empty battery should remain near zero after the takeover. "
                f"Powers: {h.battery_powers()}"
            )
        finally:
            await h.stop()

    @pytest.mark.timeout(90)
    async def test_saturation_recovery_after_swap(self):
        """After forced swap, original battery recovers when constraint is lifted."""
        h = _SimHarness(
            num_batteries=2,
            base_load=[200.0, 0.0, 0.0],
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_rotation_interval=10,
            efficiency_saturation_threshold=0.4,
            saturation_decay_factor=0.8,
            saturation_stall_timeout_seconds=4.0,
        )
        await h.start()
        try:
            await h.wait_active(1)
            # Saturate the active battery
            powers = h.battery_powers()
            active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1
            h.batteries[active_idx].max_charge_power = 0
            h.batteries[active_idx].max_discharge_power = 0
            # Poll for swap — the other battery should take over
            other_idx = 1 - active_idx
            for _ in range(40):
                await asyncio.sleep(0.5)
                if abs(h.battery_powers()[other_idx]) > 50:
                    break
            assert abs(h.battery_powers()[other_idx]) > 50, (
                f"Expected other battery to take over. Powers: {h.battery_powers()}"
            )
            # Restore the original battery
            h.batteries[active_idx].max_charge_power = 800
            h.batteries[active_idx].max_discharge_power = 800
            h.ct002._balancer._last_rotation -= 11
            # Poll for the restored battery to become active again
            for _ in range(60):
                await asyncio.sleep(0.5)
                if abs(h.battery_powers()[active_idx]) > 25:
                    break
            assert abs(h.battery_powers()[active_idx]) > 25, (
                f"Restored battery should be producing. Powers: {h.battery_powers()}"
            )
            # Poll for grid to settle after restored battery ramps up
            for _ in range(20):
                await asyncio.sleep(0.5)
                if abs(h.grid_total()) < 60:
                    break
            assert abs(h.grid_total()) < 60, (
                f"Grid should be near zero after recovery. Grid: {h.grid_total():.0f}W"
            )
        finally:
            await h.stop()
