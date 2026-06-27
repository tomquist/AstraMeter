"""Regression: ramp pacing must not blind saturation detection (issue #522).

A full/empty battery cannot follow its command, so ramp pacing never grows its
per-poll cap and pins the reading it is sent at ``pace_base_step``.  Saturation
used to be scored from that *paced* reading (``last_target``), so whenever
``pace_base_step < min_target_for_saturation`` the detector saw an
"idle" command every poll, decayed the score, and never recognised the battery
as saturated — leaving it a fair share of a surplus it could not absorb (the
reporter's Venus E3 full on the PV-export phase, ~700 W exported instead of
charged into the healthy Venus A).

The fix scores saturation from the *unpaced* command intent
(``last_intent_reading``), so detection is independent of the pacing throttle.

This drives :class:`LoadBalancer.compute_target` directly (fake clock) with the
reporter's cross-phase layout and tuning: the full battery sits on the phase
carrying the PV surplus, so it is continuously commanded to charge while
reporting 0 W.
"""

from __future__ import annotations

import time

from astrameter.ct002.balancer import BalancerConfig, ConsumerMode, LoadBalancer

PHASE_IDX = {"A": 0, "B": 1, "C": 2}


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class _SimBattery:
    def __init__(self, mac: str, phase: str, *, full: bool) -> None:
        self.mac, self.phase, self.full, self.power = mac, phase, full, 0.0

    def step(self, delta: float, reported: float) -> None:
        desired = reported + delta
        # full battery cannot charge (clamps at 0 on the charge side)
        self.power = max(0.0 if self.full else -800.0, min(800.0, desired))


def _run(pace_base_step: float, pace_max_step: float) -> dict[str, float]:
    # PV export on phase A (where the FULL battery lives), some load on B.
    base = {"A": -1545.0, "B": 613.0, "C": 0.0}
    full = _SimBattery("18cedff579dd", "A", full=True)
    healthy = _SimBattery("bc2a3314c6bc", "B", full=False)
    batteries = [full, healthy]

    clock = _FakeClock()
    lb = LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            balance_gain=0.40,
            balance_deadband=30,
            max_correction_per_step=150,
            import_trim_w=8,
            min_efficient_power=150,
            efficiency_saturation_threshold=0.4,
            pace_base_step=pace_base_step,
            pace_max_step=pace_max_step,
        ),
        saturation_alpha=0.9,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=True,
        clock=clock,
    )

    for tick in range(600):
        reports = {
            b.mac: {"phase": b.phase, "power": round(b.power)} for b in batteries
        }
        grid = dict(base)
        for b in batteries:
            grid[b.phase] -= b.power
        grid_total = sum(grid.values())
        targets = {
            b.mac: lb.compute_target(
                consumer_id=b.mac,
                consumer_mode=ConsumerMode("auto"),
                all_reports=reports,
                grid_total=grid_total,
                inactive=frozenset(),
                manual=frozenset(),
                sample_id=(tick,),
            )
            for b in batteries
        }
        for b in batteries:
            b.step(targets[b.mac][PHASE_IDX[b.phase]], reports[b.mac]["power"])
        clock.advance(0.5)

    grid = dict(base)
    for b in batteries:
        grid[b.phase] -= b.power
    return {
        "full_sat": lb.get_saturation(full.mac),
        "healthy_charge": healthy.power,
        "grid": sum(grid.values()),
    }


def test_full_battery_saturates_when_pace_below_min_target() -> None:
    """pace_base_step (15) < min_target (20): the full battery must still
    saturate so the healthy one takes over.  Before the fix it stayed at 0."""
    r = _run(pace_base_step=15.0, pace_max_step=200.0)
    assert r["full_sat"] >= 0.4, (
        f"full battery saturation {r['full_sat']:.3f} below threshold — pacing "
        f"is still blinding the detector"
    )
    # And the healthy battery should have taken on the surplus rather than
    # leaving it exported.
    assert r["healthy_charge"] <= -700.0, r["healthy_charge"]


def test_full_battery_saturation_independent_of_pace_setting() -> None:
    """Detection must not depend on whether pacing is on or how it is tuned."""
    results = {
        pb: _run(pace_base_step=float(pb), pace_max_step=max(200.0, float(pb)))
        for pb in (0, 15, 30)
    }
    for pb, r in results.items():
        assert r["full_sat"] >= 0.4, (pb, r["full_sat"])
