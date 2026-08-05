"""Control-quality verdict: does the loop hold zero, and how does it miss?

The tracker is the one balancer diagnostic aimed at a user rather than at a
maintainer, so these tests are written as the situations it has to name:
a settled loop, a limit cycle, a loop that never closes the gap, and a pack
with nothing left to give.
"""

import time

from astrameter.ct002.balancer import (
    CONTROL_QUALITY_SATURATED,
    CONTROL_QUALITY_STATES,
    CONTROL_QUALITY_WARMUP_SAMPLES,
    BalancerConfig,
    ConsumerMode,
    ControlQualityTracker,
    LoadBalancer,
)


class _FakeClock:
    """Monotonic fake clock; the EMAs are time-weighted, so dt must be real."""

    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _feed(tracker, values, *, limited=False, dt=1.0, clock=None):
    for value in values:
        if clock is not None:
            clock.advance(dt)
        tracker.update(value, steering=True, limited=limited)


class TestControlQualityTracker:
    def _make(self, band=25.0):
        clock = _FakeClock()
        return ControlQualityTracker(band=band, clock=clock), clock

    def test_idle_before_anything_is_steered(self):
        tracker, _ = self._make()
        assert tracker.snapshot().verdict == "idle"

    def test_idle_while_nothing_is_being_steered(self):
        tracker, clock = self._make()
        for _ in range(40):
            clock.advance(1.0)
            tracker.update(400.0, steering=False, limited=False)
        # A house pulling 400 W with no battery in the pool is not a control
        # failure — there is no loop to grade.
        assert tracker.snapshot().verdict == "idle"

    def test_warmup_until_enough_samples(self):
        tracker, clock = self._make()
        _feed(tracker, [0.0] * (CONTROL_QUALITY_WARMUP_SAMPLES - 1), clock=clock)
        assert tracker.snapshot().verdict == "warmup"
        _feed(tracker, [0.0], clock=clock)
        assert tracker.snapshot().verdict == "stable"

    def test_stable_when_the_grid_sits_inside_the_band(self):
        tracker, clock = self._make()
        _feed(tracker, [5.0, -8.0, 3.0, -2.0] * 10, clock=clock)
        snap = tracker.snapshot()
        assert snap.verdict == "stable"
        assert snap.score > 95.0
        assert snap.in_band_fraction > 0.9

    def test_a_busy_house_that_keeps_coming_back_is_still_stable(self):
        """Calibration guard (see CONTROL_QUALITY_STABLE_BANDS).

        A real house steps constantly and every step lands on the meter before
        any battery can answer it, so a mean pinned inside the ±25 W settling
        band only happens on a quiet house. Verified against the simulator's
        scenarios: a well-behaved install must not read as a broken loop.
        """
        tracker, clock = self._make()
        # Mostly settled, with an 800 W excursion every tenth sample — a mean
        # around 85 W, which is what the simulator's healthy scenarios produce.
        _feed(tracker, ([5.0] * 9 + [800.0]) * 20 + [5.0] * 5, clock=clock)
        snap = tracker.snapshot()
        assert snap.verdict == "stable"
        assert snap.error_ema > 25.0, "the excursions are real and are reported"
        # And a house that genuinely sits far off is not excused by the same
        # allowance.
        far, far_clock = self._make()
        _feed(far, [200.0] * 200, clock=far_clock)
        assert far.snapshot().verdict == "sluggish"

    def test_score_stays_a_gradient_across_realistic_errors(self):
        """A score that pins at 0 for every imperfect house says nothing.

        The scale is set so the range real installs produce (tens to a few
        hundred watts) maps onto distinguishable numbers.
        """
        scores = []
        for error in (10.0, 60.0, 200.0, 400.0):
            tracker, clock = self._make()
            _feed(tracker, [error] * 200, clock=clock)
            scores.append(tracker.snapshot().score)
        assert scores == sorted(scores, reverse=True)
        assert scores[0] > 95.0
        assert all(0.0 < s < 100.0 for s in scores[1:])

    def test_oscillating_when_the_error_keeps_crossing_zero(self):
        tracker, clock = self._make()
        # A textbook limit cycle: large error, alternating sign every sample.
        _feed(tracker, [250.0, -250.0] * 60, clock=clock)
        snap = tracker.snapshot()
        assert snap.verdict == "oscillating"
        assert snap.reversal_rate > 0.9
        assert snap.error_ema > 200.0

    def test_sluggish_when_the_error_stays_on_one_side(self):
        tracker, clock = self._make()
        _feed(tracker, [250.0] * 120, clock=clock)
        snap = tracker.snapshot()
        assert snap.verdict == "sluggish"
        assert snap.reversal_rate < 0.05

    def test_limited_outranks_sluggish_when_the_pack_is_spent(self):
        tracker, clock = self._make()
        _feed(tracker, [250.0] * 120, limited=True, clock=clock)
        # Same numbers as the sluggish case; the difference is whose fault it is.
        assert tracker.snapshot().verdict == "limited"

    def test_a_held_grid_reads_stable_even_with_no_headroom(self):
        tracker, clock = self._make()
        _feed(tracker, [4.0] * 40, limited=True, clock=clock)
        assert tracker.snapshot().verdict == "stable"

    def test_hunting_costs_score_but_accuracy_costs_more(self):
        hunting, hunting_clock = self._make()
        _feed(hunting, [60.0, -60.0] * 60, clock=hunting_clock)
        far_off, far_clock = self._make()
        _feed(far_off, [400.0] * 120, clock=far_clock)
        assert hunting.snapshot().score > far_off.snapshot().score

    def test_score_bottoms_out_rather_than_going_negative(self):
        tracker, clock = self._make()
        _feed(tracker, [50_000.0] * 120, clock=clock)
        assert tracker.snapshot().score == 0.0

    def test_first_sample_seeds_the_window(self):
        tracker, clock = self._make()
        clock.advance(1.0)
        tracker.update(900.0, steering=True, limited=False)
        # A cold EMA would read 0 W — "perfectly held" — for the first minute
        # of a loop that is nowhere near it.
        assert tracker.snapshot().error_ema == 900.0

    def test_band_floor_protects_a_zero_deadband_config(self):
        tracker, clock = self._make(band=0.0)
        _feed(tracker, [10.0, -10.0] * 30, clock=clock)
        snap = tracker.snapshot()
        assert snap.band == 25.0
        assert snap.verdict == "stable"

    def test_wider_deadband_widens_what_counts_as_stable(self):
        tracker, clock = self._make(band=150.0)
        _feed(tracker, [100.0] * 60, clock=clock)
        assert tracker.snapshot().verdict == "stable"

    def test_pace_independent_verdict(self):
        """A 0.45 s poller and a 3 s poller must agree about the same house."""
        fast, fast_clock = self._make()
        _feed(fast, [250.0] * 300, dt=0.45, clock=fast_clock)
        slow, slow_clock = self._make()
        _feed(slow, [250.0] * 45, dt=3.0, clock=slow_clock)
        assert fast.snapshot().verdict == slow.snapshot().verdict == "sluggish"
        assert abs(fast.snapshot().score - slow.snapshot().score) < 1.0

    def test_a_long_gap_starts_a_new_window(self):
        tracker, clock = self._make()
        _feed(tracker, [400.0] * 60, clock=clock)
        assert tracker.snapshot().verdict == "sluggish"
        clock.advance(600.0)
        tracker.update(0.0, steering=True, limited=False)
        # The old window described a house that is 10 minutes gone.
        assert tracker.snapshot().verdict == "warmup"
        assert tracker.snapshot().error_ema == 0.0

    def test_verdict_goes_idle_once_the_samples_stop(self):
        tracker, clock = self._make()
        _feed(tracker, [0.0] * 40, clock=clock)
        assert tracker.snapshot().verdict == "stable"
        clock.advance(600.0)
        # Every battery left; the last verdict must not hang around describing
        # a pool that no longer exists.
        assert tracker.snapshot().verdict == "idle"

    def test_states_cover_every_verdict_the_tracker_can_emit(self):
        assert set(CONTROL_QUALITY_STATES) == {
            "idle",
            "warmup",
            "stable",
            "oscillating",
            "sluggish",
            "limited",
        }


class TestControlQualityInBalancer:
    """The tracker as the balancer drives it, through real target computes."""

    def _make(self, **cfg):
        clock = _FakeClock()
        balancer = LoadBalancer(
            config=BalancerConfig(**cfg),
            saturation_alpha=0.15,
            saturation_min_target=20,
            saturation_decay_factor=0.995,
            saturation_grace_seconds=90.0,
            saturation_stall_timeout_seconds=60.0,
            saturation_enabled=False,
            clock=clock,
        )
        return balancer, clock

    def _poll(self, balancer, clock, grid, *, reported=0.0, device_type="HMG-50"):
        clock.advance(1.0)
        reports = {"a": {"device_type": device_type, "phase": "A", "power": reported}}
        balancer.compute_target(
            "a",
            ConsumerMode("auto"),
            reports,
            grid,
            frozenset(),
            frozenset(),
            (grid, 0, 0),
        )

    def test_snapshot_carries_the_verdict(self):
        balancer, clock = self._make()
        for _ in range(40):
            self._poll(balancer, clock, 0.0)
        assert balancer.status_snapshot().control_quality.verdict == "stable"
        assert balancer.control_quality().verdict == "stable"

    def test_a_repeated_reading_still_counts(self):
        """A perfectly settled loop repeats its meter reading.

        The import trim skips repeated samples (it must not integrate a blind
        bias), but the quality verdict has to keep grading them — otherwise
        the answer goes stale exactly when it is "stable".
        """
        balancer, clock = self._make()
        for _ in range(120):
            clock.advance(1.0)
            reports = {"a": {"device_type": "HMG-50", "phase": "A", "power": 300.0}}
            balancer.compute_target(
                "a", ConsumerMode("auto"), reports, 0.0, frozenset(), frozenset(), ()
            )
        snap = balancer.control_quality()
        assert snap.verdict == "stable"
        assert snap.samples >= 100

    def test_surplus_with_no_ac_chargeable_battery_reads_as_limited(self):
        # A DC-only pack (B2500) under surplus physically cannot absorb; the
        # export that remains is not the controller mis-steering.
        balancer, clock = self._make()
        for _ in range(60):
            self._poll(balancer, clock, -400.0, device_type="HMA-1")
        assert balancer.control_quality().verdict == "limited"

    def test_a_saturated_pool_reads_as_limited(self):
        balancer, clock = self._make()
        for _ in range(60):
            self._poll(balancer, clock, 400.0)
        assert balancer.control_quality().verdict == "sluggish"
        # Empty battery: it is commanded hard but produces nothing.
        balancer._get_consumer("a").saturation_score = CONTROL_QUALITY_SATURATED
        self._poll(balancer, clock, 400.0)
        assert balancer.control_quality().verdict == "limited"

    def test_one_healthy_battery_keeps_the_pool_accountable(self):
        balancer, clock = self._make()

        def poll_pair():
            clock.advance(1.0)
            reports = {
                "a": {"device_type": "HMG-50", "phase": "A", "power": 0.0},
                "b": {"device_type": "HMG-50", "phase": "A", "power": 0.0},
            }
            balancer.compute_target(
                "a",
                ConsumerMode("auto"),
                reports,
                400.0,
                frozenset(),
                frozenset(),
                (400.0, 0, 0),
            )

        for _ in range(60):
            poll_pair()
        balancer._get_consumer("a").saturation_score = 1.0
        balancer._get_consumer("b").saturation_score = 0.0
        poll_pair()
        # One battery still has headroom, so the pool is not excused.
        assert balancer.control_quality().verdict == "sluggish"
