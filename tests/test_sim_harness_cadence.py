"""The e2e harness must poll each battery at its own cadence.

``_SimHarness.step()`` used to poll every battery exactly once and then
advance the clock by the *slowest* battery's interval.  With mixed intervals
that silently throttled every fast battery to the slow one's rate, so
``test_probe_handles_mixed_poll_intervals`` -- the one test in the suite that
sets them -- ran both units at 0.9 s and never exercised what its name says.

It also fed the plant the wrong ``dt``: each battery is stepped with its own
``poll_interval`` as the physics step, so a 0.3 s battery advanced 0.3 s of
ramp and SoC per 0.9 s of simulated time, three times too slowly.

These tests drive :class:`PollScheduler` directly with stand-in batteries, so
they pin the schedule itself rather than any controller behaviour downstream
of it.
"""

from __future__ import annotations

import math
from itertools import pairwise

import pytest
from _ct002_e2e_backend import HarnessClock, PollScheduler


class _FakeBattery:
    """Records the simulated time at which each of its polls was delivered."""

    def __init__(self, poll_interval: float) -> None:
        self.poll_interval = poll_interval
        self.poll_times: list[float] = []


# HarnessClock stores simulated time as an absolute epoch float, where one
# ulp is already ~4e-7 s, and the schedule advances it once per poll. Drift is
# therefore bounded by (advances x ulp) -- ~3e-6 s over the runs here, and
# ~2e-5 s over 200 steps. It is inherent to the clock, not to the schedule, and
# is six orders of magnitude below the intervals being asserted on.
_CLOCK_TOL = 1e-4


def _run(intervals: list[float], steps: int):
    clock = HarnessClock()
    start = clock()
    batteries = [_FakeBattery(i) for i in intervals]

    async def step_one(b: _FakeBattery) -> None:
        b.poll_times.append(clock() - start)

    return clock, start, batteries, PollScheduler(batteries, clock, step_one)


@pytest.mark.parametrize(
    "intervals",
    [
        [0.3, 0.3],  # uniform: must be exactly what the old harness did
        [3.0, 3.0],
        [0.9, 0.3],  # the ratio test_probe_handles_mixed_poll_intervals uses
        [1.0, 0.25, 0.5],
        [0.7, 0.3],  # non-integral ratio: polls straddle the step boundary
    ],
)
async def test_each_battery_polls_at_its_own_interval(intervals: list[float]) -> None:
    steps = 10
    clock, start, batteries, sched = _run(intervals, steps)
    await sched.step(steps)

    window = max(intervals) * steps
    # The clock advances by the slowest interval per step, whatever the mix.
    assert clock() - start == pytest.approx(window, abs=_CLOCK_TOL)

    for b in batteries:
        # Polls land at 0, i, 2i, ... strictly inside [0, window): one at the
        # start, and none on the closing boundary (that one opens the step
        # after the last).
        expected = math.floor((window - 1e-9) / b.poll_interval) + 1
        assert len(b.poll_times) == expected, (
            f"battery on {b.poll_interval}s polled {len(b.poll_times)}x "
            f"in {window}s, expected {expected}"
        )
        for k, t in enumerate(b.poll_times):
            assert t == pytest.approx(k * b.poll_interval, abs=_CLOCK_TOL)


async def test_uniform_intervals_are_one_poll_each_per_step() -> None:
    """The regression guard for everything the fix must NOT change.

    Every other e2e test in the suite runs a uniform cadence and its timing
    assertions are written against one poll each per step, so this has to stay
    exactly as it was.
    """
    clock, start, batteries, sched = _run([0.3, 0.3], 1)
    for step in range(1, 6):
        await sched.step()
        assert [len(b.poll_times) for b in batteries] == [step, step]
        assert clock() - start == pytest.approx(step * 0.3, abs=_CLOCK_TOL)


async def test_a_faster_battery_polls_proportionally_more() -> None:
    """The property the old harness got wrong, stated as a ratio."""
    _, _, batteries, sched = _run([0.9, 0.3], 20)
    await sched.step(20)
    slow, fast = batteries
    assert len(fast.poll_times) == 3 * len(slow.poll_times)


async def test_a_manual_clock_jump_does_not_desynchronise_the_schedule() -> None:
    """``h.clock.advance(N)`` outside a step must leave the cadence intact.

    ``test_probe_handles_mixed_poll_intervals`` jumps the clock by hand to
    reach an efficiency-rotation deadline without polling through it, so the
    scheduler has to survive its clock moving underneath it. It does, because
    ``_to_next`` holds time *remaining* rather than an absolute timestamp: the
    jump skips exactly the polls it is meant to skip, and each battery resumes
    on its own interval afterwards.

    Pinned so a later "fix" that rebases ``_to_next`` against the clock -- and
    would silently deliver a burst of catch-up polls, or shift a battery off
    its interval -- has to argue with a test.
    """
    intervals = [0.9, 0.3]
    clock, start, batteries, sched = _run(intervals, 10)
    await sched.step(10)
    polled_before = [len(b.poll_times) for b in batteries]

    clock.advance(8.0)  # the manual jump, bypassing the scheduler entirely
    jumped_to = clock() - start

    await sched.step(5)

    for b, before in zip(batteries, polled_before, strict=True):
        after = b.poll_times[before:]
        # No catch-up burst: the jump's 8 s produce no polls at all.
        window = 5 * max(intervals)
        expected = math.floor((window - 1e-9) / b.poll_interval) + 1
        assert len(after) == expected, (
            f"battery on {b.poll_interval}s delivered {len(after)} polls after "
            f"the jump, expected {expected} -- a jump must not replay the "
            f"polls it skipped"
        )
        # Resumes immediately (both are at a cycle boundary after 10 steps of
        # an integral ratio) and then holds its own interval exactly.
        assert after[0] == pytest.approx(jumped_to, abs=_CLOCK_TOL)
        for earlier, later in pairwise(after):
            assert later - earlier == pytest.approx(b.poll_interval, abs=_CLOCK_TOL)


async def test_a_manual_jump_preserves_cycle_phase() -> None:
    """A battery caught mid-cycle by a jump keeps its phase, not a reset.

    With a non-integral ratio a step can end with the fast battery partway
    through its interval. It should resume that interval where it left off --
    a real device's poll timer does not restart because the controller's clock
    moved.
    """
    clock, start, batteries, sched = _run([0.7, 0.3], 10)
    await sched.step(10)
    fast = batteries[1]
    assert sched._to_next[1] == pytest.approx(0.2, abs=_CLOCK_TOL), (
        "expected the 0.3s battery to end this run mid-cycle"
    )
    polled_before = len(fast.poll_times)

    clock.advance(8.0)
    jumped_to = clock() - start
    await sched.step(5)

    after = fast.poll_times[polled_before:]
    assert after[0] == pytest.approx(jumped_to + 0.2, abs=_CLOCK_TOL)
