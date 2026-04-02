#!/usr/bin/env python3
"""Smoke tests for post-stabilization transitions.

Exercises realistic load/solar/SOC transitions on a stabilized system
using the E2E simulation harness at high speed (time_scale=10).

Run: uv run python tests/smoke_transitions.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

# Allow importing from sibling smoke test file
sys.path.insert(0, str(Path(__file__).parent))

from smoke_efficiency_saturation import SmokeHarness, failed, passed

from b2500_meter.simulator.load_model import Load


async def scenario_1_load_step_up():
    """Scenario 1: Load step-up — 200W to 1000W."""
    print("\n== Scenario 1: Load step-up (200W → 1000W) ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[200.0, 0.0, 0.0],
        loads=[Load("microwave", 800.0, "A")],
    )
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(10)
        print(f"  Settled at 200W: {h.status()}")
        if h.active_count() != 1:
            ok = failed("settle", f"expected 1 active, got {h.active_count()}")
        else:
            passed("1 battery active at 200W")

        # Turn on microwave — total load jumps to ~1000W
        h.load_model.toggle_load(1)
        await h.wait_sim_seconds(10)
        print(f"  After load step-up: {h.status()}")

        if h.active_count() < 2:
            ok = failed("step_up", f"expected 2 active, got {h.active_count()}")
        else:
            passed("both batteries active")

        if abs(h.grid_total()) > 80:
            ok = failed("grid", f"grid not near zero: {h.grid_total():.0f}W")
        else:
            passed(f"grid near zero ({h.grid_total():.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_2_load_step_down():
    """Scenario 2: Load step-down — 600W to 200W."""
    print("\n== Scenario 2: Load step-down (600W → 200W) ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[200.0, 0.0, 0.0],
        loads=[Load("heater", 400.0, "A")],
        efficiency_rotation_interval=30,
    )
    await h.start()
    ok = True
    try:
        # Start with heater on (600W total)
        h.load_model.toggle_load(1)
        await h.wait_sim_seconds(10)
        print(f"  Settled at 600W: {h.status()}")
        if h.active_count() < 2:
            ok = failed("settle", f"expected 2 active, got {h.active_count()}")
        else:
            passed("both batteries active at 600W")

        # Turn off heater — load drops to 200W
        h.load_model.toggle_load(1)
        await h.wait_sim_seconds(15)
        print(f"  After load step-down: {h.status()}")

        if h.active_count() != 1:
            ok = failed("step_down", f"expected 1 active, got {h.active_count()}")
        else:
            passed("1 battery deprioritized")

        if abs(h.grid_total()) > 80:
            ok = failed("grid", f"grid not near zero: {h.grid_total():.0f}W")
        else:
            passed(f"grid near zero ({h.grid_total():.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_3_solar_ramp_up():
    """Scenario 3: Solar ramp-up — discharge to charge transition."""
    print("\n== Scenario 3: Solar ramp-up (discharge → charge) ==")
    h = SmokeHarness(num_batteries=2, base_load=[300.0, 0.0, 0.0])
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(10)
        print(f"  Settled at 300W load: {h.status()}")

        total_power = sum(h.battery_powers())
        if total_power < 50:
            ok = failed(
                "settle", f"batteries should be discharging: {total_power:.0f}W"
            )
        else:
            passed(f"batteries discharging ({total_power:.0f}W)")

        # Add 500W solar — net load becomes -200W (excess)
        h.load_model.set_solar(500)
        await h.wait_sim_seconds(10)
        print(f"  After solar ramp-up: {h.status()}")

        total_power = sum(h.battery_powers())
        if total_power > -50:
            ok = failed(
                "transition", f"batteries should be charging: {total_power:.0f}W"
            )
        else:
            passed(f"batteries charging ({total_power:.0f}W)")

        if abs(h.grid_total()) > 80:
            ok = failed("grid", f"grid not near zero: {h.grid_total():.0f}W")
        else:
            passed(f"grid near zero ({h.grid_total():.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_4_solar_ramp_down():
    """Scenario 4: Solar ramp-down — charge to discharge transition."""
    print("\n== Scenario 4: Solar ramp-down (charge → discharge) ==")
    h = SmokeHarness(num_batteries=2, base_load=[300.0, 0.0, 0.0])
    await h.start()
    ok = True
    try:
        # Start with solar on — net load = 300 - 500 = -200W
        h.load_model.set_solar(500)
        await h.wait_sim_seconds(10)
        print(f"  Settled with solar: {h.status()}")

        total_power = sum(h.battery_powers())
        if total_power > -50:
            ok = failed("settle", f"batteries should be charging: {total_power:.0f}W")
        else:
            passed(f"batteries charging ({total_power:.0f}W)")

        # Remove solar — net load back to 300W
        h.load_model.set_solar(0)
        await h.wait_sim_seconds(10)
        print(f"  After solar ramp-down: {h.status()}")

        total_power = sum(h.battery_powers())
        if total_power < 50:
            ok = failed(
                "transition", f"batteries should be discharging: {total_power:.0f}W"
            )
        else:
            passed(f"batteries discharging ({total_power:.0f}W)")

        if abs(h.grid_total()) > 80:
            ok = failed("grid", f"grid not near zero: {h.grid_total():.0f}W")
        else:
            passed(f"grid near zero ({h.grid_total():.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_5_soc_full():
    """Scenario 5: SOC full — battery saturates during charging, swap occurs."""
    print("\n== Scenario 5: SOC full during charging ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[-200.0, 0.0, 0.0],
        efficiency_rotation_interval=30,
        saturation_decay_factor=0.9,
    )
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(10)
        print(f"  Settled (charging): {h.status()}")
        if h.active_count() != 1:
            ok = failed("settle", f"expected 1 active, got {h.active_count()}")
        else:
            passed("1 battery active (charging)")

        # Identify active battery and force its SOC to full
        powers = h.battery_powers()
        active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1
        other_idx = 1 - active_idx
        print(f"  Setting battery {active_idx} SOC to 1.0 (full)")
        h.batteries[active_idx].soc = 1.0

        await h.wait_sim_seconds(15)
        print(f"  After SOC full: {h.status()}")

        if abs(h.battery_powers()[other_idx]) < 50:
            ok = failed("swap", "other battery didn't take over charging")
        else:
            passed(f"other battery took over ({h.battery_powers()[other_idx]:.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_6_soc_empty():
    """Scenario 6: SOC empty — battery saturates during discharging, swap occurs."""
    print("\n== Scenario 6: SOC empty during discharging ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[200.0, 0.0, 0.0],
        efficiency_rotation_interval=30,
        saturation_decay_factor=0.9,
    )
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(10)
        print(f"  Settled (discharging): {h.status()}")
        if h.active_count() != 1:
            ok = failed("settle", f"expected 1 active, got {h.active_count()}")
        else:
            passed("1 battery active (discharging)")

        # Identify active battery and drain it
        powers = h.battery_powers()
        active_idx = 0 if abs(powers[0]) > abs(powers[1]) else 1
        other_idx = 1 - active_idx
        print(f"  Setting battery {active_idx} SOC to 0.0 (empty)")
        h.batteries[active_idx].soc = 0.0

        await h.wait_sim_seconds(15)
        print(f"  After SOC empty: {h.status()}")

        if abs(h.battery_powers()[other_idx]) < 50:
            ok = failed("swap", "other battery didn't take over discharging")
        else:
            passed(f"other battery took over ({h.battery_powers()[other_idx]:.0f}W)")

    finally:
        await h.stop()
    return ok


async def scenario_7_multi_phase_load_shift():
    """Scenario 7: Multi-phase load shift — new load on phase B activates second battery."""
    print("\n== Scenario 7: Multi-phase load shift ==")
    h = SmokeHarness(
        num_batteries=2,
        base_load=[300.0, 0.0, 0.0],
        loads=[Load("oven", 300.0, "B")],
    )
    # Assign batteries to different phases before starting
    h.batteries[0].phase = "A"
    h.batteries[1].phase = "B"
    await h.start()
    ok = True
    try:
        await h.wait_sim_seconds(10)
        print(f"  Settled (300W on A): {h.status()}")

        total_power = sum(abs(p) for p in h.battery_powers())
        if total_power < 100:
            ok = failed(
                "settle", f"batteries should be serving 300W: {total_power:.0f}W"
            )
        else:
            passed(f"batteries serving load ({total_power:.0f}W)")

        # Turn on oven on phase B — total load becomes 600W
        h.load_model.toggle_load(1)
        await h.wait_sim_seconds(10)
        print(f"  After phase B load: {h.status()}")

        if h.active_count() < 2:
            ok = failed("shift", f"expected 2 active, got {h.active_count()}")
        else:
            passed("both batteries active across phases")

        if abs(h.grid_total()) > 80:
            ok = failed("grid", f"grid not near zero: {h.grid_total():.0f}W")
        else:
            passed(f"grid near zero ({h.grid_total():.0f}W)")

    finally:
        await h.stop()
    return ok


async def main():
    print("=" * 60)
    print("Post-Stabilization Transition Smoke Tests")
    print("Time scale: 10x (simulated time runs 10x faster)")
    print("=" * 60)

    scenarios = [
        scenario_1_load_step_up,
        scenario_2_load_step_down,
        scenario_3_solar_ramp_up,
        scenario_4_solar_ramp_down,
        scenario_5_soc_full,
        scenario_6_soc_empty,
        scenario_7_multi_phase_load_shift,
    ]

    results = []
    t0 = time.time()
    for scenario in scenarios:
        try:
            ok = await scenario()
            results.append((scenario.__doc__.strip().split(":")[0], ok))
        except Exception as e:
            print(f"  ✗ EXCEPTION: {e}")
            import traceback

            traceback.print_exc()
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
