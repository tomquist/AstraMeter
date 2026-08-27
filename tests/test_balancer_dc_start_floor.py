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

from astrameter.ct002.balancer import (
    DC_MIN_ACTIONABLE_OUTPUT_W,
    BalancerConfig,
    BalancerConsumerState,
    ConsumerMode,
    LoadBalancer,
    SaturationTracker,
    min_actionable_output,
)


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _tracker(clock: _FakeClock) -> SaturationTracker:
    return SaturationTracker(
        alpha=0.15,
        min_target=20,
        decay_factor=0.995,
        stall_timeout_seconds=60,
        clock=clock,
    )


def _balancer(clock: _FakeClock) -> LoadBalancer:
    return LoadBalancer(
        config=BalancerConfig(
            min_efficient_power=100.0,
            fair_distribution=True,
            balance_gain=0.2,
            balance_deadband=25,
            concentrate_deadband=60.0,
            grid_predict_trust=0.5,
            osc_damp_max=0.95,
            pace_base_step=100,
            pace_max_step=400,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90,
        saturation_stall_timeout_seconds=60,
        clock=clock,
    )


def test_min_actionable_output_only_applies_to_dc_only_batteries() -> None:
    assert min_actionable_output("HMJ-2") == DC_MIN_ACTIONABLE_OUTPUT_W
    assert min_actionable_output("HMA-1") == DC_MIN_ACTIONABLE_OUTPUT_W
    # Venus and Jupiter regulate down to their own deadband.
    assert min_actionable_output("HMG-50") == 0.0
    assert min_actionable_output("VNSE3-0") == 0.0
    assert min_actionable_output("HMN-1") == 0.0


def test_sub_floor_command_scores_no_saturation() -> None:
    """50 W asked of a B2500 is not a target it can miss -- it cannot try."""
    clock = _FakeClock()
    tracker = _tracker(clock)
    state = BalancerConsumerState()

    for _ in range(40):
        tracker.update(state, 50.0, 0.0, min_actionable_output("HMJ-2"))
        clock.advance(1.5)

    assert tracker.get(state) == 0.0


def test_command_above_the_floor_still_scores_saturation() -> None:
    """A battery that ignores a command it *could* execute is still saturated."""
    clock = _FakeClock()
    tracker = _tracker(clock)
    state = BalancerConsumerState()

    for _ in range(40):
        tracker.update(state, 200.0, 0.0, min_actionable_output("HMJ-2"))
        clock.advance(1.5)

    assert tracker.get(state) > 0.8


def test_venus_is_unaffected_by_the_dc_floor() -> None:
    """A Venus follows small targets, so a missed 50 W is real evidence."""
    clock = _FakeClock()
    tracker = _tracker(clock)
    state = BalancerConsumerState()

    for _ in range(40):
        tracker.update(state, 50.0, 0.0, min_actionable_output("VNSE3-0"))
        clock.advance(1.5)

    assert tracker.get(state) > 0.8


def test_a_unit_seen_answering_a_smaller_command_is_judged_from_there() -> None:
    """The 80 W figure is an assumption; a demonstrated response beats it.

    A B2500 pairs with whatever inverter its owner selected, and some energize
    below the nominal two-channel floor.  Once such a unit has answered a 30 W
    command, a missed 50 W one is real evidence again -- otherwise a genuinely
    empty battery of that kind would keep its share.
    """
    clock = _FakeClock()
    lb = _balancer(clock)
    state = lb._get_consumer("cccccccccccc")
    report = {"device_type": "HMJ-2", "phase": "A", "power": 0}

    assert lb._saturation_floor(state, report) == DC_MIN_ACTIONABLE_OUTPUT_W
    state.pace_responded_at = 30.0
    assert lb._saturation_floor(state, report) == 30.0
    # A big command answered says nothing about small ones: stay conservative.
    state.pace_responded_at = 250.0
    assert lb._saturation_floor(state, report) == DC_MIN_ACTIONABLE_OUTPUT_W
    # Batteries with a built-in inverter keep no floor either way.
    assert lb._saturation_floor(state, {"device_type": "VNSE3-0"}) == 0.0


def test_idle_b2500_keeps_its_share_while_under_commanded() -> None:
    """The reporter's case, driven through the balancer.

    One B2500 at its 400 W limit, one stuck at 1 W (it never got a command big
    enough to start), ~150 W importing.  The idle one has to keep getting a
    real share, so that it eventually clears its start floor -- in the
    reporter's log its largest command over two minutes was 77 W.
    """
    clock = _FakeClock()
    lb = _balancer(clock)
    mode = ConsumerMode("auto", None)
    at_limit, stuck = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
    reports = {
        at_limit: {"device_type": "HMJ-2", "phase": "A", "power": 400},
        stuck: {"device_type": "HMJ-2", "phase": "A", "power": 1},
    }

    asks = []
    for i in range(120):
        grid = 150.0 if i % 4 else 20.0  # drum pulsing, mostly importing
        for cid in (at_limit, stuck):
            out = lb.compute_target(
                cid, mode, reports, grid, frozenset(), frozenset(), (i, grid)
            )
            if cid == stuck:
                asks.append(sum(out))
            clock.advance(1.5)

    biggest = max(asks)
    assert biggest >= DC_MIN_ACTIONABLE_OUTPUT_W, (
        f"the idle B2500's largest command was {biggest:.0f} W, below the "
        f"{DC_MIN_ACTIONABLE_OUTPUT_W:.0f} W it needs to produce anything"
    )
