"""Regression: balancer cycles forever between two empty batteries while
healthy ones sit permanently deprioritized.

Reported in issue #230 (user kiss81): four phase-A consumers, sustained
~800 W load, and ``MIN_EFFICIENT_POWER`` high enough that only one
active slot is used.  The two alphabetically-first consumers are "empty"
(inverter caps output at 0 W regardless of the target) and the two
alphabetically-later consumers are "full" (follow their target).

Before the fix, ``_reject_probe`` reinserted the just-rejected candidate
near the front of the deprioritized section, so ``_maybe_force_swap_saturated``
kept re-picking the same battery.  At the same time the rejection
updated ``_last_rotation``, which suppressed scheduled rotation for a
full ``efficiency_rotation_interval``.  The result: the two empty
batteries swapped back and forth forever and the full batteries never
got probed.

This test drives :class:`LoadBalancer.compute_target` directly via a
:class:`_FakeClock` harness (same style as
``tests/test_balancer_probe_lockup.py``) and asserts that at least one
of the two full batteries appears in the active set for >= 50% of the
final 600 ticks.
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
    """Minimal inverter simulation: ramps ``power`` toward ``desired``.

    If ``is_empty`` is ``True`` the inverter caps output at 0 W regardless
    of the commanded target — this is the "empty" battery from the report.
    """

    def __init__(self, mac: str, *, is_empty: bool) -> None:
        self.mac = mac
        self.is_empty = is_empty
        self.max_discharge = 800
        self.ramp = 300
        self.power = 0.0

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
    """Balancer tuned for kiss81's reproduction conditions."""
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            balance_gain=0.2,
            balance_deadband=15,
            min_efficient_power=750,
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


def test_full_batteries_eventually_get_active_slot() -> None:
    # Alphabetical order places the two empty ones at positions 0 and 1
    # and the two full ones at positions 2 and 3.
    empty_macs = ["aabb00000001", "aabb00000002"]
    full_macs = ["ccdd00000003", "ccdd00000004"]
    batteries = [SimBattery(mac, is_empty=True) for mac in empty_macs] + [
        SimBattery(mac, is_empty=False) for mac in full_macs
    ]

    clock = _FakeClock()
    lb = _make_balancer(clock)

    phase_a_load = 800.0
    active_membership: list[set[str]] = []

    for tick in range(1800):
        reports = {b.mac: {"phase": "A", "power": round(b.power)} for b in batteries}
        grid_total = phase_a_load - sum(b.power for b in batteries)

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

        slots = max(1, len(lb._priority) - len(lb._deprioritized))
        active_membership.append(set(lb._priority[:slots]))

        clock.advance(1.0)

    full_set = set(full_macs)
    tail = active_membership[-600:]
    hits = sum(1 for active in tail if active & full_set)
    ratio = hits / len(tail)

    assert ratio >= 0.5, (
        f"Full batteries were active for only {ratio:.1%} of the final "
        f"600 ticks (threshold 50%). Before the fix this ratio is ~0%; "
        f"with the fix it should be near 100%."
    )
