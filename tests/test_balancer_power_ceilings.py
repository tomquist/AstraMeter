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
from typing import Any

import pytest

from astrameter.config.logger import logger
from astrameter.ct002.balancer import (
    CEILING_RETEST_PUSH_W,
    CEILING_RETEST_SECONDS,
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


def _balancer(clock: _Clock, **cfg: Any) -> LoadBalancer:
    return LoadBalancer(
        # Pacing and damping off, so a test reads the allocation itself.
        config=BalancerConfig(
            fair_distribution=cfg.pop("fair_distribution", True),
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
    # The pool has settled with the grid at zero, so the grid pushes nothing,
    # but each capped battery's share of the 3300 W is still past its ceiling:
    # the retest pushes it there, and its holding flat confirms the ceiling.
    # Letting the ceiling lapse would pull the big battery back down.
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


# ---------------------------------------------------------------------------
# Retesting a ceiling (issue #704)
# ---------------------------------------------------------------------------

PAUSED, FREE = SMALL_1, BIG


def _pair(paused: int, free: int) -> dict[str, ConsumerReport]:
    return {
        PAUSED: ConsumerReport(device_type="VNSE3", phase="A", power=paused),
        FREE: ConsumerReport(device_type="VNSE3", phase="A", power=free),
    }


def _paused_at(clock: _Clock, power: int = 600, **cfg: Any) -> LoadBalancer:
    """A pair where PAUSED held at *power* for a few seconds while pushed (a
    battery merely pausing on its way up looks no different) and FREE kept
    moving, so only PAUSED has a ceiling.  A negative *power* is charging."""
    lb = _balancer(clock, **cfg)
    sign = 1 if power > 0 else -1
    for step in range(LEARN_ROUNDS):
        free = sign * (abs(power) + 100 + 50 * step)
        _round(lb, clock, _pair(paused=power, free=free), grid=sign * 1200)
    assert _ceiling(lb, PAUSED, sign) == abs(power)
    assert _ceiling(lb, FREE, sign) == 0.0
    return lb


def _settle(
    lb: LoadBalancer, clock: _Clock, reports: dict, seconds: float, grid: float = 0
) -> list[dict[str, float]]:
    """Poll a settled pool for *seconds*; the readings sent, round by round."""
    sent: list[dict[str, float]] = []
    end = clock.now + seconds
    while clock.now < end:
        sent.append(_round(lb, clock, reports, grid))
    return sent


def test_a_fresh_ceiling_holds_the_battery_at_it() -> None:
    # The pool has settled with the grid at zero and the pair split 600/1000.
    # Until the ceiling is due for a retest, PAUSED is left at it and FREE is
    # not pulled down toward it.
    clock = _Clock()
    lb = _paused_at(clock)
    for sent in _settle(lb, clock, _pair(600, 1000), CEILING_RETEST_SECONDS - 5):
        assert sent[PAUSED] == pytest.approx(0)
        assert sent[FREE] == pytest.approx(0)


def test_a_ceiling_due_for_a_retest_pushes_the_battery_past_it() -> None:
    # Once the ceiling is due, PAUSED is sent a small push past it, enough to
    # clear the firmware deadband and to count as pushed; FREE is untouched.
    clock = _Clock()
    lb = _paused_at(clock)
    sent = _settle(lb, clock, _pair(600, 1000), CEILING_RETEST_SECONDS + 2)[-1]
    assert sent[PAUSED] == pytest.approx(CEILING_RETEST_PUSH_W)
    assert sent[FREE] == pytest.approx(0)


def test_a_ceiling_that_no_longer_binds_is_not_retested() -> None:
    # 20 W short of its 620 W share: not worth a push, so the ceiling is left
    # to lapse instead.
    clock = _Clock()
    lb = _paused_at(clock)
    for sent in _settle(lb, clock, _pair(600, 640), CEILING_TTL_SECONDS + 5):
        assert sent[PAUSED] == pytest.approx(0)
    assert _ceiling(lb, PAUSED) == 0.0


def test_a_battery_that_merely_paused_follows_the_retest_and_shares_again() -> None:
    # Issue #704.  PAUSED moves by whatever it is sent, so it was never capped:
    # held at its ceiling it would stay at 600 W for good while FREE carried
    # the rest, but the retest moves it past the ceiling, the ceiling drops,
    # and the pair evens out.
    clock = _Clock()
    lb = _paused_at(clock)
    paused, free = 600.0, 1000.0
    for _ in range(int(CEILING_RETEST_SECONDS) + 60):
        sent = _round(lb, clock, _pair(round(paused), round(free)), grid=0)
        paused += sent[PAUSED]
        free += sent[FREE]
    assert _ceiling(lb, PAUSED) == 0.0
    assert abs(paused - free) < 100


def test_a_battery_that_holds_against_the_retest_confirms_its_ceiling() -> None:
    # A real limit: PAUSED stays at 600 W however hard it is pushed, so the
    # ceiling is confirmed the way it was learned and the push stops until the
    # next retest.
    clock = _Clock()
    lb = _paused_at(clock)
    held = _pair(600, 1000)
    assert _settle(lb, clock, held, CEILING_RETEST_SECONDS + 2)[-1][PAUSED] > 0
    pushes = 1
    while _round(lb, clock, held, grid=0)[PAUSED] > 0:
        pushes += 1
        assert pushes <= LEARN_ROUNDS
    assert pushes >= CEILING_STALL_POLLS
    assert _ceiling(lb, PAUSED) == 600
    for sent in _settle(lb, clock, held, CEILING_RETEST_SECONDS - 5):
        assert sent[PAUSED] == pytest.approx(0)


@pytest.mark.parametrize(
    ("power", "free", "grid", "cfg"),
    [
        # Fair distribution off: no balance correction runs at all.
        (600, 1000, 0, {"fair_distribution": False}),
        # Charging, with the firmware parking the grid a few watts to the
        # import side: the grid sits on the far side of the charge ceiling, so
        # deadband concentration takes every tick and hands PAUSED nothing.
        (-600, -1000, 8, {}),
    ],
    ids=["fair_distribution_off", "charging_under_concentration"],
)
def test_a_real_ceiling_survives_however_the_pool_is_steered(
    power: int, free: int, grid: float, cfg: dict[str, Any]
) -> None:
    # A capped battery under a settled pool, across several TTLs: the retest
    # must still reach it and keep its ceiling confirmed, or the ceiling lapses
    # and is relearned over and over (or, unlearned, the issue #655 stall
    # returns).
    clock = _Clock()
    lb = _paused_at(clock, power, **cfg)
    sign = 1 if power > 0 else -1
    pushed = False
    for _ in range(int(3 * CEILING_TTL_SECONDS)):
        sent = _round(lb, clock, _pair(power, free), grid)
        pushed = pushed or sent[PAUSED] * sign >= CEILING_RETEST_PUSH_W
        assert _ceiling(lb, PAUSED, sign) == abs(power)
    assert pushed


def test_no_retest_while_the_grid_asks_the_pool_for_less() -> None:
    # The load dropped: a push past the ceiling would only slow the battery's
    # wind-down.
    clock = _Clock()
    lb = _paused_at(clock)
    _settle(lb, clock, _pair(600, 1000), CEILING_RETEST_SECONDS - 5)
    for sent in _settle(lb, clock, _pair(600, 1000), 10, grid=-300):
        assert sent[PAUSED] < 0


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
