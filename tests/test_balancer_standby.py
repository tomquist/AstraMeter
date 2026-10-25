"""A lighter-weighted battery idles at 0 W until it is needed (discussion #741).

With distribution weights such as 10 / 0.1 one battery is the priority and the
other only takes what it cannot: the share the light one is handed is a few
watts, which the firmware ignores, so balancing used to leave it stalled
wherever it stopped (64 W charging in the report) and pulled it back so slowly
on a swing from load to surplus that it charged the priority battery for ten
seconds.  It now idles at exactly 0 W while its share is under the balance
deadband, and rejoins once the priority battery is at a ceiling or stops
following.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrameter.ct002.balancer import (
    CEILING_STALL_POLLS,
    CEILING_STALL_SECONDS,
    STANDBY_RELEASE_SATURATION,
    BalancerConfig,
    ConsumerMode,
    ConsumerReport,
    LoadBalancer,
)

AUTO = ConsumerMode("auto")
PRIO, OVERFLOW = "aaaaaaaaaaaa", "bbbbbbbbbbbb"
LEARN_ROUNDS = 1 + max(CEILING_STALL_POLLS, int(CEILING_STALL_SECONDS) + 1)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _balancer(clock: _Clock, **cfg: Any) -> LoadBalancer:
    return LoadBalancer(
        # Pacing and damping off, so a test reads the allocation itself.
        config=BalancerConfig(
            pace_base_step=0,
            osc_damp_max=0,
            grid_predict_trust=0,
            import_trim_w=0,
            **cfg,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90,
        saturation_stall_timeout_seconds=60,
        clock=clock,
    )


def _reports(
    prio: int,
    overflow: int,
    *,
    weights: tuple[float, float] = (10.0, 0.1),
    overflow_type: str = "VNSE3",
) -> dict[str, ConsumerReport]:
    return {
        PRIO: ConsumerReport(
            device_type="VNSE3", phase="A", power=prio, weight=weights[0]
        ),
        OVERFLOW: ConsumerReport(
            device_type=overflow_type, phase="A", power=overflow, weight=weights[1]
        ),
    }


def _poll(lb: LoadBalancer, cid: str, reports: dict, grid: float) -> float:
    """One poll of *cid*; the scalar reading it is sent."""
    return sum(
        lb.compute_target(
            cid, AUTO, reports, grid, frozenset(), frozenset(), (grid, cid)
        )
    )


def _round(lb: LoadBalancer, clock: _Clock, reports: dict, grid: float) -> dict:
    out = {cid: _poll(lb, cid, reports, grid) for cid in reports}
    clock.now += 1.0
    return out


def test_stalled_overflow_is_sent_to_zero() -> None:
    # The report: 1252 W into the priority unit, 64 W stuck in the other.
    clock = _Clock()
    lb = _balancer(clock)
    sent = _round(lb, clock, _reports(-1252, -64), -10)
    # A reading of +64 W is what lands a battery charging 64 W on 0 W.
    assert sent[OVERFLOW] == pytest.approx(64)


def test_discharging_overflow_drops_at_once_when_load_turns_to_surplus() -> None:
    # The overflow was covering the load past the priority unit's limit; the
    # load drops away and the house exports.  It must stop on the next poll
    # rather than be wound down while the priority unit charges from it.
    clock = _Clock()
    lb = _balancer(clock)
    sent = _round(lb, clock, _reports(800, 900), -1450)
    assert sent[OVERFLOW] == pytest.approx(-900)


def test_overflow_takes_what_the_priority_battery_cannot() -> None:
    clock = _Clock()
    lb = _balancer(clock)
    # The priority unit sits at its 2500 W charge limit however hard it is
    # pushed: once that is learned, the rest of the surplus is the overflow's.
    for _ in range(LEARN_ROUNDS):
        sent = _round(lb, clock, _reports(-2500, 0), -1000)
    assert lb._consumers[PRIO].ceiling(-1) == pytest.approx(2500)
    sent = _round(lb, clock, _reports(-2500, 0), -1000)
    assert sent[OVERFLOW] < -500


def test_overflow_rejoins_when_the_priority_battery_stops_following() -> None:
    # The priority unit is full: asked to charge, it stays at 0 W.
    clock = _Clock()
    lb = _balancer(clock)
    sent = _round(lb, clock, _reports(0, 0), -800)
    assert sent[OVERFLOW] == 0
    for _ in range(20):
        sent = _round(lb, clock, _reports(0, 0), -800)
        if sent[OVERFLOW] < 0:
            break
    assert lb.get_saturation(PRIO) >= STANDBY_RELEASE_SATURATION
    assert sent[OVERFLOW] < 0


def test_equal_weights_never_idle() -> None:
    # A small load on an evenly split pool keeps today's behaviour: no battery
    # is sent to 0 W for being under the deadband.
    clock = _Clock()
    lb = _balancer(clock, concentrate_deadband=0)
    sent = _round(lb, clock, _reports(10, 10, weights=(1.0, 1.0)), 20)
    assert sent[OVERFLOW] > 0


def test_dc_only_light_battery_is_not_idled() -> None:
    # A B2500 cannot be steered to 0 W, so it keeps balancing.
    clock = _Clock()
    lb = _balancer(clock, concentrate_deadband=0)
    sent = _round(lb, clock, _reports(400, 60, overflow_type="HMA-2"), 30)
    assert sent[OVERFLOW] != pytest.approx(-60)


def test_no_standby_without_fair_distribution() -> None:
    clock = _Clock()
    lb = _balancer(clock, fair_distribution=False)
    sent = _round(lb, clock, _reports(-1252, -64), -10)
    assert sent[OVERFLOW] != pytest.approx(64)


def test_only_a_heavier_battery_that_stopped_following_releases_standby() -> None:
    # A parked battery (weight 0) still carrying a saturation score from before
    # it was parked cannot take a share, so it must not hold the overflow out
    # of standby.
    parked = "cccccccccccc"
    clock = _Clock()
    lb = _balancer(clock)
    reports = _reports(-1252, -64)
    reports[parked] = ConsumerReport(device_type="VNSE3", phase="A", power=0, weight=0)
    _round(lb, clock, reports, -10)
    lb._consumers[parked].saturation_score = 1.0
    sent = _round(lb, clock, reports, -10)
    assert sent[OVERFLOW] == pytest.approx(64)
