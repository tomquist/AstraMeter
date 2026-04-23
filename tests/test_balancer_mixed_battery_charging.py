"""Reproduction for issue #338: mixed DC + AC batteries leave the AC
battery stuck in standby under solar surplus.

Scenario from the report (users c00LhaNd86 and Matze6989):
a Marstek Venus (can charge **and** discharge via AC) runs next to a
Marstek B2500 (a DC battery — discharges via AC, but cannot charge via
AC at all).  Both report to the same AstraMeter CT002 emulator fed by
a Shelly Pro 3EM.  With several hundred watts of solar surplus the
Venus stays in standby indefinitely; pointing the Marstek app directly
at the Shelly (no AstraMeter in the loop) makes the Venus charge at
~1 kW immediately.  Discharging, where the B2500 can participate, works
fine.

Root cause: :meth:`LoadBalancer.compute_target` splits the grid reading
evenly across every reporting storage unit
(``fair_share = grid_total / N`` plus a ``_balance_correction`` that
pushes each consumer toward the average of all reported powers).
Neither mechanism knows the B2500 is charge-blind, so under surplus
each battery is asked to absorb half of the real feed-in.  In the user's
setup that half falls below the Venus's inverter start-up threshold
(empirically ~300-500 W on a Venus E), so the Venus's controller keeps
deciding the command is too small to wake up — and because it never
starts, the grid stays at full surplus and the split never grows.

This test drives :class:`LoadBalancer.compute_target` directly, mirroring
the harness used by :mod:`tests.test_balancer_empty_battery_lockup`,
and asserts the observable outcome: under a 600 W surplus the Venus
never leaves standby and the grid keeps feeding back the full 600 W.
"""

from __future__ import annotations

import time

from astrameter.ct002.balancer import (
    BalancerConfig,
    ConsumerMode,
    LoadBalancer,
)


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class DCOnlyBattery:
    """B2500-style battery: discharges via AC, cannot charge via AC.

    Any negative (charge) command is clamped to 0.  Positive commands
    ramp up like a normal inverter within ``max_discharge``.
    """

    def __init__(self, mac: str, max_discharge: int = 800, ramp: float = 300.0) -> None:
        self.mac = mac
        self.max_discharge = max_discharge
        self.ramp = ramp
        self.power = 0.0

    def step(self, target_delta: float, reported_power: float) -> None:
        desired = reported_power + target_delta
        desired = max(0, min(self.max_discharge, desired))
        delta = desired - self.power
        if delta > self.ramp:
            delta = self.ramp
        elif delta < -self.ramp:
            delta = -self.ramp
        self.power += delta


class ACBatteryWithStartupThreshold:
    """Venus-style battery with a start-up threshold below which it stays idle.

    Real Marstek Venus inverters will not transition out of standby
    until the commanded change exceeds a few hundred watts — 400 W is
    a conservative mid-point of what users report.  Once activated the
    battery ramps normally; if the commanded magnitude drops back below
    ``startup_min`` for long enough the battery returns to standby.
    The commanded magnitude is derived from the CT-style ``current +
    delta`` protocol, matching :class:`astrameter.simulator.battery.BatterySimulator`.
    """

    def __init__(
        self,
        mac: str,
        *,
        max_charge: int = 2500,
        max_discharge: int = 800,
        ramp: float = 300.0,
        startup_min: float = 400.0,
    ) -> None:
        self.mac = mac
        self.max_charge = max_charge
        self.max_discharge = max_discharge
        self.ramp = ramp
        self.startup_min = startup_min
        self.power = 0.0
        self._active = False

    def step(self, target_delta: float, reported_power: float) -> None:
        desired = reported_power + target_delta
        desired = max(-self.max_charge, min(self.max_discharge, desired))
        if not self._active:
            if abs(desired) < self.startup_min:
                # Inverter stays in standby; no ramp, no power draw.
                return
            self._active = True
        delta = desired - self.power
        if delta > self.ramp:
            delta = self.ramp
        elif delta < -self.ramp:
            delta = -self.ramp
        self.power += delta
        # If the Venus is commanded back near zero for a sustained period
        # it drops back to standby — match the real inverter.
        if self._active and abs(self.power) < 20 and abs(desired) < self.startup_min:
            self._active = False
            self.power = 0.0


def _make_balancer(clock: _FakeClock) -> LoadBalancer:
    """Balancer with CT002 defaults (matching the out-of-the-box config)."""
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            balance_gain=0.2,
            balance_deadband=15,
            error_boost_threshold=150,
            error_boost_max=0.5,
            error_reduce_threshold=20,
            max_correction_per_step=80,
            # ``min_efficient_power=0`` disables efficiency deprioritization,
            # matching the out-of-the-box config the reporters are running.
            min_efficient_power=0,
            probe_min_power=80,
            efficiency_rotation_interval=900,
            efficiency_fade_alpha=0.15,
            efficiency_saturation_threshold=0.4,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=True,
        clock=clock,
    )


def _run_scenario(batteries, surplus_watts: float, ticks: int):
    """Drive the balancer for *ticks* seconds at 1 Hz under a fixed surplus.

    Returns ``(grid_trace, per_mac_power_trace)``.
    """
    clock = _FakeClock()
    lb = _make_balancer(clock)
    grid_trace: list[float] = []
    power_trace: dict[str, list[float]] = {b.mac: [] for b in batteries}

    for tick in range(ticks):
        reports = {b.mac: {"phase": "A", "power": round(b.power)} for b in batteries}
        # Grid = (load - solar) - battery_sum.  Here we model a clean
        # surplus-only case: zero house load, ``surplus_watts`` of solar,
        # so ``grid = -surplus_watts - sum(battery.power)``.
        grid_total = -surplus_watts - sum(b.power for b in batteries)
        grid_trace.append(grid_total)

        deltas: dict[str, float] = {}
        for b in batteries:
            phase_targets = lb.compute_target(
                consumer_id=b.mac,
                consumer_mode=ConsumerMode("auto"),
                all_reports=reports,
                grid_total=grid_total,
                inactive=frozenset(),
                manual=frozenset(),
                sample_id=(tick,),
            )
            deltas[b.mac] = phase_targets[0]

        for b in batteries:
            b.step(deltas[b.mac], reports[b.mac]["power"])
            power_trace[b.mac].append(b.power)

        clock.advance(1.0)

    return grid_trace, power_trace


def test_venus_never_wakes_up_with_b2500_present_under_surplus() -> None:
    """600 W surplus + DC-only B2500 keeps the Venus stuck in standby.

    With a 600 W surplus the balancer commands each battery to absorb
    300 W.  The B2500 can't charge, and the Venus's inverter never sees
    a command above its start-up threshold (400 W here), so it never
    activates.  Because the Venus stays at 0 W, the B2500 at 0 W, and
    the balancer can't see that the split is wrong, the grid keeps
    feeding back the full 600 W forever.  This is the exact observation
    from issue #338.
    """
    b2500 = DCOnlyBattery("b2500_01")
    venus = ACBatteryWithStartupThreshold("venus_01", startup_min=400.0)

    grid, power = _run_scenario([b2500, venus], surplus_watts=600.0, ticks=200)

    venus_tail = power["venus_01"][-30:]
    b2500_tail = power["b2500_01"][-30:]
    grid_tail = grid[-30:]

    # B2500 is physically incapable of charging — always 0 W.
    assert max(abs(p) for p in b2500_tail) < 1.0, (
        f"B2500 should stay at 0 W (DC-only), tail was {b2500_tail}"
    )

    # The Venus never leaves standby because the balancer-split command
    # (-300 W) stays below its start-up threshold (400 W).
    assert max(abs(p) for p in venus_tail) < 1.0, (
        f"Bug reproduced: Venus should be stuck at 0 W (standby), "
        f"but observed tail {venus_tail}.  If this assertion fails the "
        f"bug may be fixed — verify against issue #338 before adjusting."
    )

    # Therefore the grid keeps feeding the full 600 W surplus back.
    avg_grid = sum(grid_tail) / len(grid_tail)
    assert avg_grid < -550, (
        f"Grid should still show ~-600 W feed-in (nothing is absorbing), "
        f"average was {avg_grid:.0f} W"
    )


def test_venus_alone_absorbs_the_same_surplus_fine() -> None:
    """Control: the same Venus without the B2500 drains the surplus.

    Confirms the failure is the DC + AC mix, not a general balancer or
    threshold problem: with only the Venus in the pool, the full 600 W
    surplus lands on the Venus, clears its start-up threshold, and it
    absorbs the surplus to near-zero grid.
    """
    venus = ACBatteryWithStartupThreshold("venus_solo", startup_min=400.0)

    grid, power = _run_scenario([venus], surplus_watts=600.0, ticks=200)

    venus_tail = power["venus_solo"][-30:]
    grid_tail = grid[-30:]

    # Venus woke up and absorbed the surplus.
    assert min(venus_tail) < -500, (
        f"Venus (solo) should absorb ~-600 W, tail was {venus_tail}"
    )
    avg_grid = sum(grid_tail) / len(grid_tail)
    assert abs(avg_grid) < 30, f"Grid should converge near 0 W, got {avg_grid:.0f} W"


def test_discharging_still_works_with_mixed_batteries() -> None:
    """Control: the user notes discharging works; verify it here.

    With a positive grid import (house consuming more than solar), both
    batteries can contribute — the B2500 by discharging normally, the
    Venus likewise — so splitting the target evenly is fine.
    """
    b2500 = DCOnlyBattery("b2500_dc_dis")
    venus = ACBatteryWithStartupThreshold("venus_ac_dis", startup_min=400.0)

    # 1000 W of house consumption with no solar → grid imports 1000 W;
    # ``_run_scenario`` uses ``-surplus_watts`` so we pass a negative
    # "surplus" to simulate import.
    grid, power = _run_scenario([b2500, venus], surplus_watts=-1000.0, ticks=200)

    avg_grid = sum(grid[-30:]) / 30
    # Both batteries discharge ~500 W each — grid drained to ~0.
    assert abs(avg_grid) < 50, (
        f"Discharge across both batteries should drain grid, got {avg_grid:.0f} W"
    )
    assert sum(power["b2500_dc_dis"][-30:]) / 30 > 400, (
        "B2500 should be actively discharging"
    )
    assert sum(power["venus_ac_dis"][-30:]) / 30 > 400, (
        "Venus should also be discharging"
    )
