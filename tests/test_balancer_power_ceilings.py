"""Batteries with different output limits share the load (issue #655).

A Venus limited to 800 W discharge next to a 2500 W one: under a 3 kW load the
two limited units sit at 800 W however hard they are pushed.  The balancer used
to keep handing them an even slice of the grid error and pulled the 2500 W unit
down toward the pool average, so the pool stalled a few hundred watts short of
the load.  It now learns each battery's ceiling from that behaviour, hands the
limited units' slice to the others, and stops equalizing past a ceiling.
"""

from __future__ import annotations

import logging

import pytest

from astrameter.config.logger import logger
from astrameter.ct002.balancer import (
    CEILING_STALL_POLLS,
    CEILING_STALL_SECONDS,
    CEILING_TTL_SECONDS,
    BalancerConfig,
    ConsumerMode,
    ConsumerReport,
    LoadBalancer,
    capped_weighted_share,
)

AUTO = ConsumerMode("auto")
SMALL_1, SMALL_2, BIG = "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc"
# Rounds (one poll per battery, a second apart) to learn a ceiling: the first
# poll has sent nothing yet, and the run must span CEILING_STALL_SECONDS.
LEARN_ROUNDS = 1 + max(CEILING_STALL_POLLS, int(CEILING_STALL_SECONDS) + 1)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _balancer(clock: _Clock, **cfg: float) -> LoadBalancer:
    return LoadBalancer(
        # Pacing and damping off, so a test reads the allocation itself.
        config=BalancerConfig(
            fair_distribution=True,
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


def _reports(small: int, big: int) -> dict[str, ConsumerReport]:
    return {
        SMALL_1: ConsumerReport(device_type="VNSE3", phase="A", power=small),
        SMALL_2: ConsumerReport(device_type="VNSE3", phase="A", power=small),
        BIG: ConsumerReport(device_type="VNSE3", phase="A", power=big),
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


# ---------------------------------------------------------------------------
# Water-filling
# ---------------------------------------------------------------------------


def test_capped_share_hands_what_a_ceiling_blocks_to_the_others() -> None:
    weights = {"a": 1.0, "b": 1.0, "c": 1.0}
    ceilings = {"a": 800.0, "b": 800.0}
    ids = ["a", "b", "c"]
    assert capped_weighted_share(3000, weights, ids, ceilings, "a") == 800
    assert capped_weighted_share(3000, weights, ids, ceilings, "c") == 1400


def test_capped_share_is_the_plain_share_below_every_ceiling() -> None:
    weights = {"a": 1.0, "b": 1.0, "c": 1.0}
    ids = ["a", "b", "c"]
    ceilings = {"a": 800.0, "b": 800.0}
    for cid in ids:
        assert capped_weighted_share(1800, weights, ids, ceilings, cid) == 600


def test_capped_share_applies_the_ceiling_of_the_pool_direction_only() -> None:
    # The ceilings passed in are for the direction of the total; a charging
    # pool is split by magnitude against them, with the sign carried through.
    weights = {"a": 1.0, "b": 1.0}
    ceilings = {"a": 500.0}
    assert capped_weighted_share(-2000, weights, ["a", "b"], ceilings, "a") == -500
    assert capped_weighted_share(-2000, weights, ["a", "b"], ceilings, "b") == -1500


def test_capped_share_repeats_until_nobody_is_over() -> None:
    # "b" only goes over once "a"'s excess lands on the others.
    weights = {"a": 1.0, "b": 1.0, "c": 1.0}
    ceilings = {"a": 100.0, "b": 1100.0}
    ids = ["a", "b", "c"]
    assert capped_weighted_share(3000, weights, ids, ceilings, "b") == 1100
    assert capped_weighted_share(3000, weights, ids, ceilings, "c") == 1800


def test_capped_share_honours_weights() -> None:
    weights = {"a": 1.0, "b": 3.0}
    assert capped_weighted_share(2000, weights, ["a", "b"], {"b": 1000.0}, "a") == 1000


# ---------------------------------------------------------------------------
# Learning a ceiling
# ---------------------------------------------------------------------------


def _ceiling(lb: LoadBalancer, cid: str, sign: int = 1) -> float:
    return lb._consumers[cid].ceiling(sign)


def test_a_battery_held_flat_while_pushed_gets_a_ceiling() -> None:
    clock = _Clock()
    lb = _balancer(clock)
    reports = _reports(small=800, big=900)
    for _ in range(LEARN_ROUNDS - 1):
        _round(lb, clock, reports, grid=1200)
        assert _ceiling(lb, SMALL_1) == 0.0
    _round(lb, clock, reports, grid=1200)
    assert _ceiling(lb, SMALL_1) == 800
    assert _ceiling(lb, SMALL_1, sign=-1) == 0.0


def test_a_battery_that_moves_gets_no_ceiling() -> None:
    clock = _Clock()
    lb = _balancer(clock)
    for step in range(3 * LEARN_ROUNDS):
        _round(lb, clock, _reports(small=400 + 20 * step, big=900), grid=1200)
    assert _ceiling(lb, SMALL_1) == 0.0


def test_a_battery_not_asked_for_more_gets_no_ceiling() -> None:
    # Flat at 800 W with the grid at zero is a battery that is done, not one
    # that is stuck.
    clock = _Clock()
    lb = _balancer(clock, balance_deadband=25)
    for _ in range(3 * LEARN_ROUNDS):
        _round(lb, clock, _reports(small=800, big=800), grid=0)
    assert _ceiling(lb, SMALL_1) == 0.0


def test_a_flat_run_must_also_last_long_enough() -> None:
    # A fast poller packs the polls into under the stall window.
    clock = _Clock()
    lb = _balancer(clock)
    reports = _reports(small=800, big=900)
    for _ in range(3 * LEARN_ROUNDS):
        for cid in reports:
            _poll(lb, cid, reports, 1200)
        clock.now += 0.2
    assert _ceiling(lb, SMALL_1) == 0.0


def test_output_past_the_ceiling_drops_it() -> None:
    clock = _Clock()
    lb = _balancer(clock)
    for _ in range(LEARN_ROUNDS):
        _round(lb, clock, _reports(small=800, big=900), grid=1200)
    assert _ceiling(lb, SMALL_1) == 800
    _round(lb, clock, _reports(small=850, big=900), grid=1200)
    assert _ceiling(lb, SMALL_1) == 0.0


def test_an_unconfirmed_ceiling_expires() -> None:
    clock = _Clock()
    lb = _balancer(clock)
    for _ in range(LEARN_ROUNDS):
        _round(lb, clock, _reports(small=800, big=900), grid=1200)
    assert _ceiling(lb, SMALL_1) == 800
    # Demand gone: the battery winds down and is never pushed at its ceiling.
    clock.now += CEILING_TTL_SECONDS + 1
    _round(lb, clock, _reports(small=300, big=300), grid=0)
    assert _ceiling(lb, SMALL_1) == 0.0


def test_a_ceiling_that_still_binds_does_not_expire() -> None:
    # The pool has settled with the grid at zero, so nothing pushes the capped
    # batteries, but each one's plain share of the 3300 W is still past its
    # ceiling.  Letting the ceiling lapse would pull the big battery back down.
    clock = _Clock()
    lb = _balancer(clock)
    for _ in range(LEARN_ROUNDS):
        _round(lb, clock, _reports(small=800, big=900), grid=1200)
    settled = _reports(small=800, big=1700)
    for _ in range(int(2 * CEILING_TTL_SECONDS / 60)):
        clock.now += 60.0
        sent = _round(lb, clock, settled, grid=0)
    assert _ceiling(lb, SMALL_1) == _ceiling(lb, SMALL_2) == 800
    assert sent[BIG] == pytest.approx(0)


def test_learning_and_dropping_a_ceiling_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    lb = _balancer(clock)
    with caplog.at_level(logging.INFO, logger=logger.name):
        for _ in range(LEARN_ROUNDS):
            _round(lb, clock, _reports(small=800, big=900), grid=1200)
        _round(lb, clock, _reports(small=850, big=900), grid=1200)
    messages = caplog.messages
    assert any(
        f"{SMALL_1} holds at 800 W discharge though asked for more" in m
        for m in messages
    )
    assert any(f"{SMALL_1} discharge limit of 800 W exceeded" in m for m in messages)


# ---------------------------------------------------------------------------
# Allocation once a ceiling is known
# ---------------------------------------------------------------------------


def _learned(clock: _Clock) -> LoadBalancer:
    lb = _balancer(clock)
    for _ in range(LEARN_ROUNDS):
        _round(lb, clock, _reports(small=800, big=900), grid=1200)
    assert _ceiling(lb, SMALL_1) == _ceiling(lb, SMALL_2) == 800
    return lb


def test_the_unlimited_battery_takes_the_whole_error() -> None:
    clock = _Clock()
    lb = _learned(clock)
    sent = _round(lb, clock, _reports(small=800, big=1400), grid=600)
    # All 600 W of import goes to the battery that can act on it, and it is
    # not pulled back toward the 1000 W pool average.
    assert sent[BIG] == pytest.approx(600)


def test_without_a_ceiling_the_unlimited_battery_is_held_back() -> None:
    # The behaviour issue #655 reported: an even slice minus a pull toward the
    # pool average leaves the big battery a fraction of the error.
    clock = _Clock()
    lb = _balancer(clock)
    sent = _round(lb, clock, _reports(small=800, big=1400), grid=600)
    assert sent[BIG] < 150


def test_a_pinned_battery_is_still_pushed() -> None:
    # Its nominal slice is what confirms the ceiling, and what exposes one the
    # user has since raised.
    clock = _Clock()
    lb = _learned(clock)
    sent = _round(lb, clock, _reports(small=800, big=1400), grid=600)
    assert sent[SMALL_1] == pytest.approx(200)
    assert _ceiling(lb, SMALL_1) == 800


def test_below_its_ceiling_a_battery_shares_as_before() -> None:
    # Demand for 1800 W on a pool that knows the ceilings: 600 W each is under
    # every limit, so equalization runs as usual.
    clock = _Clock()
    lb = _learned(clock)
    sent = _round(lb, clock, _reports(small=800, big=200), grid=0)
    assert sent[BIG] > 0
    assert sent[SMALL_1] < 0


def test_deadband_concentration_skips_a_pinned_battery() -> None:
    # The pinned battery is the most active one on the phase once the big one
    # has backed off; handing it a small correction would go nowhere.
    clock = _Clock()
    lb = _learned(clock)
    reports = {
        SMALL_1: ConsumerReport(device_type="VNSE3", phase="A", power=800),
        BIG: ConsumerReport(device_type="VNSE3", phase="A", power=790),
    }
    lb.remove_consumer(SMALL_2)
    sent = {cid: _poll(lb, cid, reports, 40) for cid in reports}
    assert sent[BIG] == pytest.approx(40)
