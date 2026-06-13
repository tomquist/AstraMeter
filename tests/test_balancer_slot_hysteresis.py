"""Regression: efficiency slot count must not thrash on meter noise.

Reported in issue #469: with three batteries and ``MIN_EFFICIENT_POWER=150``,
a ~300 W base load sits exactly at the 2-slot boundary
(``int(abs_target / min_efficient_power)`` flips between 1 and 2), and the
slot count had no hysteresis of its own — ``EFFICIENCY_HYSTERESIS_FACTOR``
only guarded the enter/exit-limiting boolean.  Every ±10 W meter-noise tick
toggled a battery between active and deprioritized, keeping the fade EMA
permanently mid-transition and the whole pool hunting (the
``mixed_venus_b2500/eff`` evaluation scenario never settled).

The fix gates slot-count *growth* behind the same 20% margin as exiting
limiting entirely: demand must reach ``k * min_efficient_power * 1.2`` to
activate a k-th unit, while shrinking stays immediate (mirroring how entering
limiting is immediate).

This drives :class:`LoadBalancer.compute_target` directly with a scripted
demand (same style as ``tests/test_balancer_empty_battery_lockup.py``) and
asserts the deprioritized set holds still under boundary noise but still
grows/shrinks on real demand changes.
"""

from __future__ import annotations

import time

from astrameter.ct002.balancer import (
    BalancerConfig,
    ConsumerMode,
    LoadBalancer,
)

MACS = ["aabb00000001", "aabb00000002", "aabb00000003"]
MIN_EFFICIENT_POWER = 150.0


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _make_balancer(clock: _FakeClock) -> LoadBalancer:
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            min_efficient_power=MIN_EFFICIENT_POWER,
            # Keep rotation and saturation swaps out of the picture so the
            # test isolates the slot-count arithmetic.
            efficiency_rotation_interval=1_000_000,
            efficiency_saturation_threshold=0.0,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=False,
        clock=clock,
    )


def _tick(lb: LoadBalancer, demand: float, carried: float, tick: int) -> None:
    """One balancer pass: the first battery carries *carried* W of *demand*."""
    reports = {
        MACS[0]: {"phase": "A", "power": round(carried)},
        MACS[1]: {"phase": "A", "power": 0},
        MACS[2]: {"phase": "A", "power": 0},
    }
    grid_total = demand - carried
    for mac in MACS:
        lb.compute_target(
            consumer_id=mac,
            consumer_mode=ConsumerMode("auto"),
            all_reports=reports,
            grid_total=grid_total,
            inactive=frozenset(),
            manual=frozenset(),
            sample_id=(tick,),
        )


def test_slot_count_holds_through_boundary_noise_but_tracks_real_steps() -> None:
    clock = _FakeClock()
    lb = _make_balancer(clock)
    tick = 0

    def run(demand: float, ticks: int = 1) -> None:
        nonlocal tick
        for _ in range(ticks):
            # carried=295 holds the first battery's reported output fixed while
            # only `demand` (grid_total) varies, so the test isolates the
            # slot-count response to boundary *noise* vs real demand steps —
            # the report doesn't drift and confound the comparison.
            _tick(lb, demand, carried=295.0, tick=tick)
            tick += 1
            clock.advance(1.0)

    # Settle into limiting at one active slot (~300 W demand, 150 W floor).
    run(295.0)
    assert len(lb._deprioritized) == 2

    # Meter noise straddling the 2-slot boundary (2 x 150 = 300 W) must not
    # move the slot count: growth now needs 2 x 150 x 1.2 = 360 W.  Before
    # the fix every 308 W tick activated a second unit and every 292 W tick
    # deprioritized it again.
    for _ in range(50):
        run(308.0)
        assert len(lb._deprioritized) == 2, "slot count grew on boundary noise"
        run(292.0)
        assert len(lb._deprioritized) == 2

    # A real demand step past the 20% margin still grows the active set.
    run(365.0)
    assert len(lb._deprioritized) == 1

    # Holding just below the growth margin keeps the grown count (no flap
    # on the way back down either: shrink needs demand below 2 x 150).
    run(310.0, ticks=5)
    assert len(lb._deprioritized) == 1

    # Dropping below 2 x 150 shrinks immediately.
    run(290.0)
    assert len(lb._deprioritized) == 2

    # Demand high enough to exit limiting entirely activates everyone
    # (per-consumer 550/3 > 150 x 1.2).
    run(550.0)
    assert len(lb._deprioritized) == 0
