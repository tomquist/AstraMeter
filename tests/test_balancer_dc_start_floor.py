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
    LoadBalancer,
    SaturationTracker,
    min_actionable_output,
    saturation_floor,
)

B2500 = {"device_type": "HMJ-2", "phase": "A", "power": 0}


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
    # An owner who configured a floor outranks our figure, in both directions.
    assert saturation_floor(state, B2500, 30.0) == 30.0
    assert saturation_floor(state, B2500, 150.0) == 150.0
    # Otherwise a smaller command this unit answered lowers it.
    state.pace_responded_at = 30.0
    assert saturation_floor(state, B2500, 0.0) == 30.0
    # A large command answered says nothing about small ones: stay conservative.
    state.pace_responded_at = 250.0
    assert saturation_floor(state, B2500, 0.0) == DC_MIN_ACTIONABLE_OUTPUT_W
    # Batteries with a built-in inverter keep no floor either way.
    assert saturation_floor(state, {"device_type": "VNSE3-0"}, 150.0) == 0.0


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
        b2500: {"device_type": "HMJ-2", "phase": "A", "power": 1},
        venus: {"device_type": "VNSE3-0", "phase": "A", "power": 1},
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
