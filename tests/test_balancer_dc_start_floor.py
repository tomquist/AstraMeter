"""Regression: a sub-floor command must not score a B2500 as saturated (#624).

A B2500's two DC output channels are a hard on/off below ~40 W each, so the
unit cannot answer any command below ~80 W at all.  Saturation used to be
scored against such a command anyway: the battery was asked for 50 W,
delivered nothing, and was recorded as "cannot follow its target".  That cut
its share of every later correction to roughly a tenth, which put the next
command even further below the floor -- so it sat at 0 W for good while the
house kept importing, and only a manual target could get it out.

Decoded from the reporter's debug log (discussion #625, 2026-08-27 07:58-08:01):
two B2500 left running, one pinned at its 400 W single-output limit, the grid
importing 140 W on average, and the second unit -- with all its headroom free --
asked for 12 W on average and never once for the ~80 W it needs to start.
"""

from __future__ import annotations

import time

import pytest

from astrameter.ct002.balancer import (
    DC_MIN_ACTIONABLE_OUTPUT_W,
    BalancerConfig,
    BalancerConsumerState,
    ConsumerMode,
    ConsumerReport,
    LoadBalancer,
    SaturationTracker,
    min_actionable_output,
    saturation_floor,
)

B2500 = ConsumerReport(device_type="HMJ-2", phase="A", power=0)


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _score_after(target: float, min_actionable: float, polls: int = 40) -> float:
    """Drive a tracker with a battery that reports nothing, and return its score."""
    clock = _FakeClock()
    tracker = SaturationTracker(
        alpha=0.15,
        min_target=20,
        decay_factor=0.995,
        stall_timeout_seconds=60,
        clock=clock,
    )
    state = BalancerConsumerState()
    for _ in range(polls):
        tracker.update(state, target, 0.0, min_actionable)
        clock.advance(1.5)
    return tracker.get(state)


def test_dc_only_batteries_carry_a_start_floor() -> None:
    assert min_actionable_output("HMJ-2") == DC_MIN_ACTIONABLE_OUTPUT_W
    # A Venus regulates down to its own deadband, so it has none.
    assert min_actionable_output("VNSE3-0") == 0.0


@pytest.mark.parametrize(
    ("target", "floor", "scored"),
    [
        # Below what a B2500 can execute: it cannot try, so it cannot fail.
        (50.0, DC_MIN_ACTIONABLE_OUTPUT_W, False),
        # Above it: ignoring a command it could have executed is real evidence.
        (200.0, DC_MIN_ACTIONABLE_OUTPUT_W, True),
        # A battery with a built-in inverter follows 50 W, so missing it counts.
        (50.0, 0.0, True),
    ],
)
def test_saturation_is_scored_only_above_the_start_floor(
    target: float, floor: float, scored: bool
) -> None:
    score = _score_after(target, floor)
    assert (score > 0.8) if scored else (score == 0.0)


def test_the_floor_prefers_evidence_over_the_nominal_figure() -> None:
    """The 80 W figure is an assumption; the paired inverter decides.

    A configured MIN_DC_OUTPUT is the owner telling us what their unit needs,
    and a command it was seen to answer is proof.  Without either, a missed
    50 W would be blamed on a battery that never had a chance -- and for an
    owner whose inverter starts lower, a genuinely empty unit would keep its
    share.
    """
    state = BalancerConsumerState()

    assert saturation_floor(state, B2500, 0.0) == DC_MIN_ACTIONABLE_OUTPUT_W
    # A configured floor raises the gate above our figure ...
    assert saturation_floor(state, B2500, 150.0) == 150.0
    # ... but cannot lower it below what the hardware can do: MIN_DC_OUTPUT is
    # where we park the unit, not a claim about what it can start on (#600).
    assert saturation_floor(state, B2500, 30.0) == DC_MIN_ACTIONABLE_OUTPUT_W
    # A smaller command this unit answered lowers the model's half of it.
    state.pace_responded_at = 30.0
    assert saturation_floor(state, B2500, 0.0) == 30.0
    # ... and the configured floor still wins where it is the higher of the two.
    assert saturation_floor(state, B2500, 50.0) == 50.0
    # A large command answered says nothing about small ones: stay conservative.
    state.pace_responded_at = 250.0
    assert saturation_floor(state, B2500, 0.0) == DC_MIN_ACTIONABLE_OUTPUT_W
    # A per-device MIN_DC_OUTPUT override applies to any battery, including a
    # family with no nominal floor: that unit is being held above its deadband,
    # and the gate has to follow, or it is judged against a command it is never
    # sent (flagged by CodeRabbit on #629).
    assert (
        saturation_floor(state, ConsumerReport(device_type="VNSE3-0"), 150.0) == 150.0
    )
    # With no override, a battery with a built-in inverter keeps no floor.
    assert saturation_floor(state, ConsumerReport(device_type="VNSE3-0"), 0.0) == 0.0


def test_compute_target_supplies_each_consumer_its_floor() -> None:
    """The wiring, not just the tracker.

    ``compute_target`` has to hand the tracker the floor for the battery it is
    scoring; dropping that argument restores #624 while every tracker-level
    test above stays green, so pin it here.  A Venus in the same pool must keep
    a floor of 0 -- the floor is per consumer, not per pool.
    """
    clock = _FakeClock()
    lb = LoadBalancer(
        config=BalancerConfig(min_efficient_power=0.0, fair_distribution=True),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90,
        saturation_stall_timeout_seconds=60,
        clock=clock,
    )
    b2500, venus = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
    reports = {
        b2500: ConsumerReport(device_type="HMJ-2", phase="A", power=1),
        venus: ConsumerReport(device_type="VNSE3-0", phase="A", power=1),
    }
    seen: dict[str, float] = {}
    real_update = lb._saturation.update
    mode = ConsumerMode("auto", None)
    for cid in (b2500, venus):
        captured: list[float] = []
        lb._saturation.update = (  # type: ignore[method-assign]
            lambda state, lt, actual, floor, _c=captured: _c.append(floor)
        )
        # Two polls: the first records an intent, the second scores against it.
        for i in range(2):
            lb.compute_target(
                cid, mode, reports, 60.0, frozenset(), frozenset(), (i, 60.0)
            )
            clock.advance(1.5)
        lb._saturation.update = real_update  # type: ignore[method-assign]
        seen[cid] = captured[-1] if captured else -1.0

    assert seen[b2500] == DC_MIN_ACTIONABLE_OUTPUT_W
    assert seen[venus] == 0.0


def test_a_configured_floor_below_the_start_floor_does_not_lock_a_unit_out() -> None:
    """Regression for issue #600.

    Two B2500 on one phase, ``MIN_DC_OUTPUT = 20`` -- below the ~80 W the
    family needs to energize a channel pair.  The second unit is parked at that
    20 W floor, cannot execute it, and used to be scored for the miss: its
    saturation pinned at 1.0, its share of every correction cut to a hundredth,
    and the first unit left carrying the whole house.  The reporter's recorder
    trace shows exactly that -- 230 W against 4 W, saturation 0% against 89%,
    and ``reported + last_target == 20`` on two thirds of the replies.

    The floor MIN_DC_OUTPUT parks a unit at is not a statement that the unit
    can start there, so it must not lower the gate below what the hardware can
    do.  Here that is the difference between the second unit taking half the
    load and sitting at 0 W for good.
    """
    house, start_floor = 460.0, DC_MIN_ACTIONABLE_OUTPUT_W
    clock = _FakeClock()
    lb = LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            min_efficient_power=0.0,
            min_dc_output=20.0,
            balance_deadband=25,
            max_correction_per_step=80,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90,
        saturation_stall_timeout_seconds=180,
        clock=clock,
    )
    mode = ConsumerMode("auto", None)
    # A carries the house; B idles, as in the reporter's trace.
    power = {"aaaaaaaaaaaa": 230.0, "bbbbbbbbbbbb": 4.0}
    for step in range(400):
        reports = {
            cid: ConsumerReport(device_type="HMJ-2", phase="A", power=round(w))
            for cid, w in power.items()
        }
        grid = house - sum(power.values())
        commands = {
            cid: sum(
                lb.compute_target(
                    cid, mode, reports, grid, frozenset(), frozenset(), (step, grid)
                )
            )
            for cid in power
        }
        for cid, delta in commands.items():
            # A B2500 channel pair is a hard on/off: below its start floor the
            # unit stays in standby, above it the setpoint is executed.
            want = power[cid] + delta
            power[cid] = 0.0 if want < start_floor else min(want, 800.0)
        clock.advance(1.0)

    assert power["bbbbbbbbbbbb"] > start_floor
    assert lb.get_saturation("bbbbbbbbbbbb") < 0.5
    # And the pair actually shares the house rather than one unit carrying it.
    assert abs(power["aaaaaaaaaaaa"] - power["bbbbbbbbbbbb"]) < house / 2
