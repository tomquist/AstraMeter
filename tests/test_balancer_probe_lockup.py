"""Regression: balancer gets stuck at target=0 after probe handoff.

Originally reported against logs
``34dea19a_b2500_meter_2026-04-10T04-50-21.881Z.log``: a two-battery
system where both consumers self-report on phase ``B``, the balancer
kicks off an efficiency-rotation probe at 05:28:00, the probe completes
successfully at 05:28:15, and then the newly-active battery's target
snaps from 95 W → 0 W one tick later and is pinned at 0 W indefinitely
(visible in the log for ~1.5 hours until manual restart).  The grid
drifts ~97 W uncompensated for the entire window.

The root cause is exercised here at two levels:

1. A *unit-level* repro that drives :class:`LoadBalancer.compute_target`
   with the exact report sequence from the log (two phase-B consumers,
   scripted power outputs and meter readings) and asserts the active
   battery receives a reasonable target after the handoff.

2. A *smoother-level* repro in ``test_smoother.py`` covering the
   ``sample_id`` dedup that fires even when ``raw_total`` has changed.
"""

from __future__ import annotations

import time

from astrameter.ct002.balancer import (
    BalancerConfig,
    ConsumerMode,
    LoadBalancer,
    ProbeState,
)
from astrameter.ct002.smoother import TargetSmoother


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _make_balancer(
    clock: _FakeClock,
    smoother: TargetSmoother | None = None,
) -> LoadBalancer:
    """Match the user's configuration (defaults plus efficiency enabled).

    The ``smoother`` is injected the same way :class:`CT002` does in
    production (`ct002.py:159`).  Tests that exercise the probe
    commit/reject path **must** pass their smoother here, otherwise
    the balancer has no reference to the smoother and
    ``_commit_probe``/``_reject_probe`` can't call ``reseed()`` on it —
    the test would silently skip the reseed path the code is
    supposed to cover.
    """
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            balance_gain=0.2,
            balance_deadband=15,
            error_boost_threshold=150,
            error_boost_max=0.5,
            error_reduce_threshold=20,
            max_correction_per_step=80,
            max_target_step=0,
            deadband=20,
            min_efficient_power=50,
            probe_min_power=80,
            efficiency_rotation_interval=1800,
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
        smoother=smoother,
    )


def _reports(active_power: int, backup_power: int) -> dict:
    """Build the ``reports`` dict the balancer expects: both batteries
    are self-reporting on phase B (matching log2)."""
    return {
        "24215edb1936": {"phase": "B", "power": active_power},
        "acd929a74b20": {"phase": "B", "power": backup_power},
    }


def _tick(
    lb: LoadBalancer,
    smoother: TargetSmoother,
    reports: dict,
    grid_reading: float,
) -> tuple[list[float], list[float]]:
    """Drive one full poll cycle (both consumers) exactly like CT002 does.

    Returns ``(active_target, backup_target)`` as 3-element phase lists.
    """
    sample_id = (grid_reading, 0.0, 0.0)
    smoothed = smoother.update(grid_reading, sample_id)

    # Order mirrors the real log: active battery first, backup second.
    active_target = lb.compute_target(
        consumer_id="24215edb1936",
        consumer_mode=ConsumerMode("auto"),
        all_reports=reports,
        smoothed_target=smoothed,
        raw_total=grid_reading,
        inactive=frozenset(),
        manual=frozenset(),
        sample_id=sample_id,
    )
    backup_target = lb.compute_target(
        consumer_id="acd929a74b20",
        consumer_mode=ConsumerMode("auto"),
        all_reports=reports,
        smoothed_target=smoothed,
        raw_total=grid_reading,
        inactive=frozenset(),
        manual=frozenset(),
        sample_id=sample_id,
    )
    return active_target, backup_target


class TestProbeReseedsSmoother:
    """After a probe commits or rejects, the balancer must reseed any
    attached smoother so the post-handoff control loop cannot drag in
    pre-probe EMA state.
    """

    def test_probe_commit_reseeds_injected_smoother(self) -> None:
        clock = _FakeClock()
        smoother = TargetSmoother(alpha=0.5)
        lb = LoadBalancer(
            config=BalancerConfig(
                min_efficient_power=50,
                probe_min_power=20,
                efficiency_rotation_interval=9999,
            ),
            saturation_alpha=0.15,
            saturation_min_target=20,
            saturation_decay_factor=0.995,
            saturation_grace_seconds=90.0,
            saturation_stall_timeout_seconds=60.0,
            clock=clock,
            smoother=smoother,
        )
        # Seed the smoother so reseed has something to clear.
        smoother.update(50.0, (50.0,))
        assert smoother.value == 50.0

        # Inject a fake in-flight probe and commit it.
        lb._probe_state = ProbeState(  # type: ignore[attr-defined]
            candidate_id="24215edb1936",
            active_ids=("24215edb1936",),
            backup_ids=("acd929a74b20",),
            restore_active_ids=("acd929a74b20",),
            deadline=clock() + 90,
            started_at=clock(),
        )
        lb._commit_probe(  # type: ignore[attr-defined]
            reports={
                "24215edb1936": {"phase": "B", "power": 22},
                "acd929a74b20": {"phase": "B", "power": 94},
            },
            now=clock(),
            actual=22.0,
        )

        assert smoother.value is None, "smoother was not reseeded after probe commit"

    def test_probe_reject_reseeds_injected_smoother(self) -> None:
        clock = _FakeClock()
        smoother = TargetSmoother(alpha=0.5)
        lb = LoadBalancer(
            config=BalancerConfig(
                min_efficient_power=50,
                probe_min_power=20,
                efficiency_rotation_interval=9999,
            ),
            saturation_alpha=0.15,
            saturation_min_target=20,
            saturation_decay_factor=0.995,
            saturation_grace_seconds=90.0,
            saturation_stall_timeout_seconds=60.0,
            clock=clock,
            smoother=smoother,
        )
        smoother.update(75.0, (75.0,))
        assert smoother.value == 75.0

        lb._probe_state = ProbeState(  # type: ignore[attr-defined]
            candidate_id="24215edb1936",
            active_ids=("24215edb1936",),
            backup_ids=("acd929a74b20",),
            restore_active_ids=("acd929a74b20",),
            deadline=clock() + 90,
            started_at=clock(),
        )
        lb._reject_probe(now=clock(), reason="test")  # type: ignore[attr-defined]

        assert smoother.value is None, "smoother was not reseeded after probe reject"


class TestProbeHandoffLockup:
    def test_active_battery_keeps_covering_demand_after_probe_handoff(self) -> None:
        """After a probe-based rotation, the new active battery must
        continue to track the real grid demand — not collapse to
        ``target = 0`` as observed in the user's log.

        The scenario replays the sequence from log2:
            * ``acd929a74b20`` is covering ~94 W of load, grid is balanced.
            * Rotation fires → probe promotes ``24215edb1936``.
            * Probe completes with ``24215edb1936`` at 22 W.
            * Backup is told to ramp to zero.
            * Physical ramp of the new active battery is slow: it stays
              at 22 W for several ticks while the backup drops to 0 W.
            * At this point the grid is *~72 W uncompensated* — the
              balancer must respond by increasing the active battery's
              target, not zero it.
        """
        clock = _FakeClock()
        smoother = TargetSmoother(alpha=0.9, deadband=20.0)
        # Wire the smoother into the balancer the same way CT002 does
        # in production, so ``_commit_probe`` will reseed it and the
        # test actually exercises the production reseed path.
        lb = _make_balancer(clock, smoother=smoother)

        # --- Warm-up: drive to a single-active steady state --------------
        # Prime: seed both consumers on phase B with a 94 W load on the
        # load model and ``acd929a74b20`` already producing 94 W (so the
        # grid reads zero and the balancer is not trying to correct
        # anything while the priority list settles).
        for _ in range(10):
            reports = _reports(active_power=0, backup_power=94)
            _tick(lb, smoother, reports, grid_reading=0.0)
            clock.advance(3.0)
            # Feed a unique ``sample_id`` each tick so
            # ``TargetSmoother.update()`` isn't deduped against the
            # previous _tick call.  The ``raw_total`` is deliberately
            # the *true* zero reading (not ``1e-6 * clock()`` — that
            # would be ~1700 because ``_FakeClock`` starts at
            # ``time.time()``, which would hugely contaminate the EMA).
            smoother.update(0.0, (clock(),))

        # Strict exclusivity check: after warm-up, the balancer has
        # populated the priority list from ``sorted(current_pool)``
        # (`balancer.py:867`), so ``24215edb1936`` (alphabetically
        # first) sits at slot 0 and ``acd929a74b20`` is the sole
        # deprioritized consumer.  The earlier assertion used an
        # ``or`` expression that was tautologically true because
        # ``acd929a74b20`` is always in ``_priority`` regardless of
        # which slot it's in — this stricter form actually catches
        # regressions where the warm-up leaves the pool in an
        # unexpected state.
        assert lb._priority == ["24215edb1936", "acd929a74b20"], (
            f"Unexpected priority after warm-up: {lb._priority}"
        )
        assert lb._deprioritized == {"acd929a74b20"}, (
            f"Unexpected deprioritized after warm-up: {lb._deprioritized}"
        )
        # Sanity: the smoother pollution fix above must keep the EMA
        # at true zero during a zero-grid warm-up.
        assert smoother.value == 0.0, (
            f"Warm-up contaminated the smoother: {smoother.value}"
        )

        # --- Force rotation (equivalent to the 05:28:00 probe start) -----
        lb.force_rotation({"24215edb1936", "acd929a74b20"})

        # --- Probe ramp: 24215edb1936 climbs 0 → 22 W over ~5 ticks ------
        probe_powers = [0, 5, 10, 15, 20, 22]
        for p in probe_powers:
            reports = _reports(active_power=p, backup_power=94)
            # Grid during probe: total battery = p + 94, load still 94,
            # so grid = 94 - (p + 94) = -p (slight export while probe ramps).
            _tick(lb, smoother, reports, grid_reading=float(-p))
            clock.advance(3.0)

        # --- Post-probe fade: backup collapses, active stays stuck at 22 W
        # because the physical battery can't respond to fast-moving
        # targets any quicker than the probe did.
        for tick_index in range(40):
            # The backup ramps 94 → 0 over the first few ticks, then
            # stays at 0 for the rest of the window (matching the log).
            if tick_index == 0:
                backup_power = 50
            elif tick_index == 1:
                backup_power = 10
            else:
                backup_power = 0

            # Active battery stays pinned at 22 W (it never received a
            # target that would tell it to ramp up, or it did and failed
            # to follow).  Real grid at this point:
            #   load(94) - total_battery(22 + backup) → 72 W .. 94 W
            active_power = 22
            grid = 94.0 - (active_power + backup_power)

            reports = _reports(active_power=active_power, backup_power=backup_power)
            active_target, backup_target = _tick(
                lb, smoother, reports, grid_reading=grid
            )
            clock.advance(3.0)

            # Phase B target for the active battery.
            b_target = active_target[1]
            # ``smoother.value`` can be ``None`` on the tick immediately
            # following a probe commit (the balancer reseeds its
            # injected smoother in ``_commit_probe``).  Use a sentinel
            # so the trace print survives that transient.
            smoothed_str = (
                f"{smoother.value:6.1f}" if smoother.value is not None else "  None"
            )
            print(
                f"t={tick_index:02d} active_power={active_power:3d} "
                f"backup={backup_power:3d} grid={grid:6.1f} "
                f"smoothed={smoothed_str} "
                f"active_tgt={active_target} backup_tgt={backup_target}"
            )

            # After a handful of ticks (let the fade + smoother settle),
            # the active battery *must* be commanded to cover the real
            # demand.  Allow wide slack so the assertion only catches a
            # genuine lockup, not sub-watt oscillation.
            if tick_index >= 10:
                assert b_target > 40.0, (
                    f"Active battery is stuck at target={b_target:.1f} W on "
                    f"phase B after {tick_index} post-probe ticks, even "
                    f"though grid reads {grid:.1f} W import.  "
                    f"This is the lockup regression."
                )
