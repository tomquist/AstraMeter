#!/usr/bin/env python3
"""Smoke tests for efficiency saturation-aware rotation.

Uses the E2E simulation harness at high speed (time_scale=10) to exercise
realistic scenarios involving battery constraints, saturation detection,
forced rotation, and recovery.

Run: uv run python tests/smoke_efficiency_saturation.py
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import time

from b2500_meter.ct002.ct002 import CT002
from b2500_meter.simulator.battery import BatterySimulator
from b2500_meter.simulator.load_model import Load, LoadModel
from b2500_meter.simulator.powermeter_sim import PowermeterSimulator

TIME_SCALE = 10  # 10x speed


def _find_free_ports(
    n: int = 2,
    types: list[int] | None = None,
) -> list[int]:
    if types is None:
        # Default: first port UDP (CT002), rest TCP (HTTP)
        types = [socket.SOCK_DGRAM] + [socket.SOCK_STREAM] * (n - 1)
    ports: list[int] = []
    socks: list[socket.socket] = []
    for i in range(n):
        s = socket.socket(socket.AF_INET, types[i % len(types)])
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:
        s.close()
    return ports


class SmokeHarness:
    """Lightweight E2E harness for smoke testing."""

    def __init__(
        self,
        num_batteries=2,
        base_load=None,
        loads=None,
        min_efficient_power=150,
        efficiency_rotation_interval=900,
        efficiency_saturation_threshold=0.4,
        saturation_decay_factor=0.995,
        time_scale=TIME_SCALE,
        **ct_kwargs,
    ):
        ct_port, http_port = _find_free_ports(2)
        self.ct_port = ct_port
        if base_load is None:
            base_load = [200.0, 0.0, 0.0]

        self.load_model = LoadModel(
            base_load=list(base_load),
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
                    ramp_rate=400.0,
                    poll_interval=1.0,
                    min_power_threshold=5.0,
                    time_scale=time_scale,
                )
            )

        self.powermeter = PowermeterSimulator(
            batteries=self.batteries,
            load_model=self.load_model,
            host="127.0.0.1",
            port=http_port,
        )

        if time_scale <= 0:
            raise ValueError("time_scale must be > 0")

        # Scale CT002 time-dependent parameters.
        # CT002 clamps efficiency_rotation_interval to a 10s floor, so
        # apply the same floor here to avoid silent clamping.
        scaled_rotation = max(efficiency_rotation_interval / time_scale, 10)

        self.ct002 = CT002(
            udp_port=ct_port,
            ct_mac=ct_mac,
            active_control=True,
            fair_distribution=True,
            smooth_target_alpha=0.9,
            deadband=5,
            min_efficient_power=min_efficient_power,
            efficiency_rotation_interval=scaled_rotation,
            efficiency_saturation_threshold=efficiency_saturation_threshold,
            saturation_decay_factor=saturation_decay_factor,
            consumer_ttl=120 / time_scale,
            **ct_kwargs,
        )

        async def update_readings(_addr, _fields=None, _consumer_id=None):
            grid = self.powermeter.compute_grid()
            return [grid["phase_a"], grid["phase_b"], grid["phase_c"]]

        self.ct002.before_send = update_readings
        self.time_scale = time_scale

    async def start(self):
        self._tasks: list[asyncio.Task] = []
        try:
            await self.powermeter.start()
            await self.ct002.start()
            self._tasks = [asyncio.create_task(b.run()) for b in self.batteries]
        except BaseException:
            # Roll back anything already started
            for t in self._tasks:
                t.cancel()
            if self._tasks:
                await asyncio.gather(*self._tasks, return_exceptions=True)
            with contextlib.suppress(
                asyncio.TimeoutError, asyncio.CancelledError, OSError
            ):
                await asyncio.wait_for(self.ct002.stop(), timeout=3.0)
            with contextlib.suppress(
                asyncio.TimeoutError, asyncio.CancelledError, OSError
            ):
                await asyncio.wait_for(self.powermeter.stop(), timeout=3.0)
            raise

    async def stop(self):
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, OSError):
            await asyncio.wait_for(self.ct002.stop(), timeout=3.0)
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError, OSError):
            await asyncio.wait_for(self.powermeter.stop(), timeout=3.0)

    async def wait_sim_seconds(self, sim_seconds: float):
        """Wait for sim_seconds of simulated time."""
        await asyncio.sleep(sim_seconds / self.time_scale)

    def battery_powers(self) -> list[float]:
        return [b.current_power for b in self.batteries]

    def grid_total(self) -> float:
        grid = self.powermeter.compute_grid()
        return grid["phase_a"] + grid["phase_b"] + grid["phase_c"]

    def active_count(self, threshold=25.0) -> int:
        return sum(1 for p in self.battery_powers() if abs(p) > threshold)

    def status(self) -> str:
        powers = self.battery_powers()
        sat = self.ct002._saturation_by_consumer
        depr = self.ct002._efficiency_deprioritized
        grid = self.grid_total()
        parts = [f"grid={grid:.0f}W"]
        for i, b in enumerate(self.batteries):
            mac_short = b.mac[-4:]
            s = sat.get(b.mac.lower(), 0.0)
            d = "DEPR" if b.mac.lower() in depr else "ACT "
            parts.append(f"{mac_short}:{d} {powers[i]:+7.1f}W sat={s:.2f}")
        return " | ".join(parts)


def passed(name):
    print(f"  ✓ {name}")


def failed(name, msg):
    print(f"  ✗ {name}: {msg}")
    return False


async def scenario_1_basic_swap():
    """Scenario 1: Battery constrained to 0W gets swapped out."""
    print("\n== Scenario 1: Basic forced swap ==")
    h = SmokeHarness(num_batteries=2, base_load=[200.0, 0.0, 0.0])
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(10)
        print(f"  Settled: {h.status()}")
        if h.active_count() != 1:
            ok = failed("settle", f"expected 1 active, got {h.active_count()}")
        else:
            passed("1 battery active at 200W")

        # Constrain active battery
        powers = h.battery_powers()
        active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1
        print(f"  Constraining battery {active_idx} to 0W")
        h.batteries[active_idx].max_charge_power = 0
        h.batteries[active_idx].max_discharge_power = 0

        await h.wait_sim_seconds(10)
        print(f"  After constraint: {h.status()}")

        other_idx = 1 - active_idx
        if abs(h.battery_powers()[other_idx]) < 50:
            ok = failed("swap", "other battery didn't take over")
        else:
            passed("other battery took over")

        if abs(h.grid_total()) > 80:
            ok = failed("grid", f"grid not near zero: {h.grid_total():.0f}W")
        else:
            passed(f"grid near zero ({h.grid_total():.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_2_recovery():
    """Scenario 2: Constrained battery recovers after limit is lifted."""
    print("\n== Scenario 2: Recovery after constraint lifted ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[200.0, 0.0, 0.0],
        efficiency_rotation_interval=30,
        saturation_decay_factor=0.9,
    )
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(5)
        powers = h.battery_powers()
        active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1

        print(f"  Constraining battery {active_idx}")
        h.batteries[active_idx].max_charge_power = 0
        h.batteries[active_idx].max_discharge_power = 0
        await h.wait_sim_seconds(10)
        print(f"  After constraint: {h.status()}")

        print(f"  Restoring battery {active_idx}")
        h.batteries[active_idx].max_charge_power = 800
        h.batteries[active_idx].max_discharge_power = 800

        # Wait for rotation interval + ramp-up
        await h.wait_sim_seconds(40)
        print(f"  After recovery: {h.status()}")

        if abs(h.grid_total()) > 80:
            ok = failed("recovery", f"grid not near zero: {h.grid_total():.0f}W")
        else:
            passed(f"grid recovered ({h.grid_total():.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_3_no_pingpong():
    """Scenario 3: Timed rotation doesn't cause ping-pong."""
    print("\n== Scenario 3: No ping-pong on timed rotation ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[200.0, 0.0, 0.0],
        efficiency_rotation_interval=15,
    )
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(5)
        print(f"  Settled: {h.status()}")

        # Track swaps over 2 rotation intervals
        swap_count = 0
        last_depr = set(h.ct002._efficiency_deprioritized)
        for i in range(20):
            await h.wait_sim_seconds(3)
            depr = set(h.ct002._efficiency_deprioritized)
            if depr != last_depr:
                swap_count += 1
                last_depr = depr
            if i % 5 == 0:
                print(f"  t={i * 3}s: {h.status()}")

        print(f"  Swap count over 60s sim: {swap_count}")
        # With healthy batteries (no saturation), expect minimal swaps from
        # timed rotation only; 6 is a conservative upper bound.
        if swap_count > 6:
            ok = failed("pingpong", f"too many swaps ({swap_count})")
        else:
            passed(f"reasonable swap count ({swap_count})")

        if abs(h.grid_total()) > 80:
            ok = failed("grid", f"grid not near zero: {h.grid_total():.0f}W")
        else:
            passed(f"grid near zero ({h.grid_total():.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_4_three_batteries():
    """Scenario 4: 3 batteries, one constrained — 2 remaining share load."""
    print("\n== Scenario 4: 3 batteries, one constrained ==")
    h = SmokeHarness(
        num_batteries=3,
        base_load=[400.0, 0.0, 0.0],
        min_efficient_power=150,
    )
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(5)
        print(f"  Settled: {h.status()}")
        # 400W / 3 = 133 < 150 → 2 active, 1 deprioritized
        if h.active_count() != 2:
            # Could be all 3 if demand is above hysteresis
            print(f"  Note: {h.active_count()} active (may vary with demand estimate)")

        # Constrain one active battery
        powers = h.battery_powers()
        sorted_idx = sorted(range(3), key=lambda i: abs(powers[i]), reverse=True)
        constrained = sorted_idx[0]  # highest power
        print(f"  Constraining battery {constrained} ({powers[constrained]:.0f}W)")
        h.batteries[constrained].max_charge_power = 0
        h.batteries[constrained].max_discharge_power = 0

        await h.wait_sim_seconds(15)
        print(f"  After constraint: {h.status()}")

        # System should still serve load with remaining batteries
        if abs(h.grid_total()) > 100:
            ok = failed("grid", f"grid too high: {h.grid_total():.0f}W")
        else:
            passed(f"grid acceptable ({h.grid_total():.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_5_load_change_during_constraint():
    """Scenario 5: Load changes while a battery is constrained."""
    print("\n== Scenario 5: Load change during constraint ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[200.0, 0.0, 0.0],
        loads=[Load("heavy", 400.0, "A")],
    )
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(5)
        print(f"  Settled at 200W: {h.status()}")

        # Constrain active battery
        powers = h.battery_powers()
        active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1
        h.batteries[active_idx].max_charge_power = 0
        h.batteries[active_idx].max_discharge_power = 0
        await h.wait_sim_seconds(10)
        print(f"  After constraint: {h.status()}")

        # Now increase load to 600W (1-based index)
        h.load_model.toggle_load(1)
        await h.wait_sim_seconds(10)
        print(f"  After load increase to 600W: {h.status()}")

        # With one battery at max 800W and the other at 0, the active
        # battery should be handling as much as possible
        other_idx = 1 - active_idx
        if abs(h.battery_powers()[other_idx]) < 100:
            ok = failed("load_change", "active battery not handling load")
        else:
            passed(
                f"active battery handling load ({h.battery_powers()[other_idx]:.0f}W)"
            )

    finally:
        await h.stop()
    return ok


async def scenario_6_both_constrained():
    """Scenario 6: Both batteries constrained — graceful degradation."""
    print("\n== Scenario 6: Both batteries constrained ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[200.0, 0.0, 0.0],
        saturation_decay_factor=0.9,
    )
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(5)
        print(f"  Settled: {h.status()}")

        # Constrain both batteries
        for b in h.batteries:
            b.max_charge_power = 0
            b.max_discharge_power = 0
        await h.wait_sim_seconds(10)
        print(f"  Both constrained: {h.status()}")

        # Both should be at 0, grid at full load — no crash
        total_power = sum(abs(p) for p in h.battery_powers())
        if total_power > 20:
            ok = failed(
                "degradation", f"batteries shouldn't be producing: {total_power:.0f}W"
            )
        else:
            passed("both batteries at 0W, no crash")

        # Restore one — wait for saturation to decay and system to recover
        h.batteries[0].max_charge_power = 800
        h.batteries[0].max_discharge_power = 800
        await h.wait_sim_seconds(15)
        print(f"  After restoring battery 0: {h.status()}")

        if abs(h.battery_powers()[0]) < 50:
            ok = failed("partial_restore", "restored battery not producing")
        else:
            passed(f"restored battery producing ({h.battery_powers()[0]:.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_7_feature_disabled():
    """Scenario 7: threshold=0 — feature disabled, no forced swaps."""
    print("\n== Scenario 7: Feature disabled (threshold=0) ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[200.0, 0.0, 0.0],
        efficiency_saturation_threshold=0.0,
        efficiency_rotation_interval=9999,
    )
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(5)
        powers = h.battery_powers()
        active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1
        first_depr = set(h.ct002._efficiency_deprioritized)
        print(f"  Settled: {h.status()}")

        # Constrain active
        h.batteries[active_idx].max_charge_power = 0
        h.batteries[active_idx].max_discharge_power = 0
        await h.wait_sim_seconds(15)
        print(f"  After constraint (no swap expected): {h.status()}")

        # Should NOT have swapped since feature is disabled
        if h.ct002._efficiency_deprioritized != first_depr:
            ok = failed("disabled", "swap happened despite threshold=0")
        else:
            passed("no forced swap with feature disabled")

    finally:
        await h.stop()
    return ok


async def scenario_8_charging_direction():
    """Scenario 8: Charging (solar excess) — swap works for negative targets."""
    print("\n== Scenario 8: Charging direction (solar excess) ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[-200.0, 0.0, 0.0],  # Net export = charging
    )
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(10)
        print(f"  Settled: {h.status()}")
        if h.active_count() != 1:
            ok = failed("settle", f"expected 1 active, got {h.active_count()}")
        else:
            passed("1 battery active (charging)")

        # Constrain active battery — can't charge
        powers = h.battery_powers()
        active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1
        print(f"  Constraining battery {active_idx} max_charge=0")
        h.batteries[active_idx].max_charge_power = 0

        await h.wait_sim_seconds(10)
        print(f"  After constraint: {h.status()}")

        other_idx = 1 - active_idx
        if abs(h.battery_powers()[other_idx]) < 50:
            ok = failed("swap", "other battery didn't take over charging")
        else:
            passed(
                f"other battery took over charging ({h.battery_powers()[other_idx]:.0f}W)"
            )

    finally:
        await h.stop()
    return ok


async def main():
    print("=" * 60)
    print("Efficiency Saturation Smoke Tests")
    print(f"Time scale: {TIME_SCALE}x (simulated time runs {TIME_SCALE}x faster)")
    print("=" * 60)

    scenarios = [
        scenario_1_basic_swap,
        scenario_2_recovery,
        scenario_3_no_pingpong,
        scenario_4_three_batteries,
        scenario_5_load_change_during_constraint,
        scenario_6_both_constrained,
        scenario_7_feature_disabled,
        scenario_8_charging_direction,
    ]

    results = []
    t0 = time.time()
    for scenario in scenarios:
        try:
            ok = await scenario()
            results.append((scenario.__doc__.strip().split(":")[0], ok))
        except Exception as e:
            print(f"  ✗ EXCEPTION: {e}")
            results.append((scenario.__doc__.strip().split(":")[0], False))

    elapsed = time.time() - t0
    print("\n" + "=" * 60)
    print(f"Results ({elapsed:.1f}s wall time):")
    all_ok = True
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}")
        if not ok:
            all_ok = False

    print("=" * 60)
    if all_ok:
        print("All scenarios passed!")
    else:
        print("Some scenarios FAILED!")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
