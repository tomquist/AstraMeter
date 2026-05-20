"""Regression for issue #377: small residual grid import with one empty battery.

User report: two B2500 on phase A. When one battery is empty (or
otherwise pinned at 0 W discharge) and the other is healthy, the grid
holds at a small persistent offset (~4-6 W) instead of converging to 0.
The healthy battery sits stuck at one value while the empty one keeps
being told to deliver the residual it cannot deliver.

Cause (from analysis on this branch): the balance correction in
``_balance_correction`` sees the imbalance between the two batteries'
phase-A power (e.g. 36 W vs 0 W), tries to equalize them, and the
sign-disagreement clamp at ``balancer.py:979`` kills the negative-side
correction on the healthy battery — dumping the entire residual onto
the empty one. The empty battery's target (~ residual / N ≈ 5 W) sits
below ``MIN_TARGET_FOR_SATURATION`` (default 20 W), so the saturation
detector takes the decay branch on every tick and never flags the empty
battery. ``eff_part`` stays at 1.0 and the imbalance never gets routed
to the healthy battery that could actually clear it.

This test drives :class:`LoadBalancer.compute_target` via the same
``_FakeClock`` / ``SimBattery`` harness used by
``test_balancer_empty_battery_lockup.py``, with a small steady residual
load that the able battery is capable of covering on its own.
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


class SimBattery:
    """Same minimal inverter sim as the empty-battery lockup test."""

    def __init__(
        self,
        mac: str,
        *,
        is_empty: bool,
        initial_power: float = 0.0,
    ) -> None:
        self.mac = mac
        self.is_empty = is_empty
        self.max_discharge = 800
        self.ramp = 300
        self.power = float(initial_power)

    def step(self, target_delta: float, reported_power: float) -> None:
        desired = reported_power + target_delta
        if self.is_empty:
            desired = 0
        desired = max(0, min(self.max_discharge, desired))
        delta = desired - self.power
        if delta > self.ramp:
            delta = self.ramp
        elif delta < -self.ramp:
            delta = -self.ramp
        self.power += delta


def _make_balancer(clock: _FakeClock) -> LoadBalancer:
    """Match the user's effective configuration in issue #377.

    The user has FAIR_DISTRIBUTION=True, ACTIVE_CONTROL=True,
    SATURATION_DETECTION=True, EFFICIENCY_ROTATION_INTERVAL=600,
    and does NOT set MIN_EFFICIENT_POWER — so efficiency-based
    deprioritization is off; saturation is the only mechanism that
    could rescue this case.
    """
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            balance_gain=0.2,
            balance_deadband=15,
            min_efficient_power=0,
            probe_min_power=80,
            efficiency_rotation_interval=600,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=True,
        clock=clock,
    )


def test_small_residual_converges_with_one_empty_battery() -> None:
    # Alphabetical ordering places the empty battery first (matches the
    # MAC ordering in the issue log: 1480ccfa < 18cedf98).
    empty = SimBattery("aabb00000001", is_empty=True)
    active = SimBattery("ccdd00000002", is_empty=False, initial_power=36.0)
    batteries = [empty, active]

    clock = _FakeClock()
    lb = _make_balancer(clock)

    # Steady ~41 W phase-A load — when the able battery is at 36 W the
    # grid sits at +5 W, exactly the situation from the issue log.
    phase_a_load = 41.0

    grid_readings: list[float] = []
    active_powers: list[float] = []

    for tick in range(900):  # 15 minutes of 1 Hz polling
        reports = {b.mac: {"phase": "A", "power": round(b.power)} for b in batteries}
        grid_total = phase_a_load - sum(b.power for b in batteries)
        grid_readings.append(grid_total)
        active_powers.append(active.power)

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

        clock.advance(1.0)

    # After settling, grid should average close to 0 (the able battery
    # has plenty of headroom to cover the 41 W load on its own).
    tail = grid_readings[-120:]
    avg = sum(tail) / len(tail)

    assert abs(avg) < 2.0, (
        f"Grid stuck at avg={avg:.2f} W over final 120 ticks "
        f"(expected ~0 W). Active battery final power: "
        f"{active_powers[-1]:.1f} W. Last 5 grid readings: "
        f"{grid_readings[-5:]}"
    )
