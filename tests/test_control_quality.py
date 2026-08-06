"""Control-quality verdict: is the loop holding the grid at zero?

The tracker is the one balancer diagnostic aimed at a user rather than at a
maintainer, so these tests are written as the situations it has to name: a
settled loop, one that is off target, and a pack with nothing left to give.
The cases that pin *what it refuses to claim* matter just as much — see
``ControlQualityVerdict`` for why no verdict names a cause.
"""

import time

import pytest

from astrameter.ct002.balancer import (
    CONTROL_QUALITY_SATURATED,
    CONTROL_QUALITY_STATES,
    CONTROL_QUALITY_WARMUP_SECONDS,
    BalancerConfig,
    ConsumerMode,
    ControlQualityTracker,
    LoadBalancer,
)
from astrameter.ct002.ct002 import _control_quality_evidence


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

    def test_warmup_until_enough_has_been_observed(self):
        tracker, clock = self._make()
        _feed(tracker, [0.0] * 9, clock=clock)
        assert tracker.snapshot().verdict == "warmup"
        _feed(tracker, [0.0], clock=clock)
        assert tracker.snapshot().verdict == "stable"

    def test_warmup_is_a_duration_not_a_sample_count(self):
        """A CT is polled once per battery, so samples arrive N times faster
        with N batteries. Counting them would let a six-battery pool publish a
        verdict off well under a second of observation, while a single battery
        on a 3 s cadence waited half a minute for the same call."""
        fast, fast_clock = self._make()
        # Six batteries at 0.45 s: 30 samples, but only ~2 s of house.
        _feed(fast, [0.0] * 30, dt=0.075, clock=fast_clock)
        assert fast.snapshot().verdict == "warmup"
        assert fast.snapshot().score is None
        # A single slow poller reaches a verdict on the same amount of house.
        slow, slow_clock = self._make()
        _feed(slow, [0.0] * 4, dt=3.0, clock=slow_clock)
        assert slow.snapshot().verdict == "stable"
        assert CONTROL_QUALITY_WARMUP_SECONDS == 10.0

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
        assert far.snapshot().verdict == "off_target"

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

    def test_off_target_whether_the_error_crosses_zero_or_not(self):
        """The verdict describes the grid; it does not guess at a cause.

        A limit cycle and a one-sided offset are both reported as
        ``off_target``, because nothing available here tells them apart
        reliably (see ``ControlQualityVerdict``) and their fixes are
        opposites. The crossing rate is published beside the verdict so the
        difference is still visible.
        """
        hunting, hunting_clock = self._make()
        _feed(hunting, [250.0, -250.0] * 60, clock=hunting_clock)
        parked, parked_clock = self._make()
        _feed(parked, [250.0] * 120, clock=parked_clock)
        assert hunting.snapshot().verdict == "off_target"
        assert parked.snapshot().verdict == "off_target"
        # ...and the evidence separates them even though the verdict doesn't.
        assert hunting.snapshot().crossings_per_second > 0.4
        assert parked.snapshot().crossings_per_second == 0.0

    def test_crossing_rate_is_per_second_not_per_sample(self):
        """The rate must describe the house, not the installation.

        A CT is polled once per battery, so a per-sample reversal *fraction*
        converges to 2*dt/T: the same physical limit cycle would read three
        times lower with three batteries in the pool than with one, and no
        real hunt would ever clear a fixed threshold at 1 Hz.
        """
        rates = []
        for dt in (0.33, 1.0, 3.0):
            tracker, clock = self._make()
            period = 30.0
            for i in range(int(1200 / dt)):
                clock.advance(dt)
                phase = (i * dt) % period
                tracker.update(
                    250.0 if phase < period / 2 else -250.0,
                    steering=True,
                    limited=False,
                )
            rates.append(tracker.snapshot().crossings_per_second)
        # Two crossings per 30 s period, whatever the poll cadence.
        for rate in rates:
            assert abs(rate - 2.0 / 30.0) < 0.01, rates

    def test_a_jittery_meter_is_not_counted_as_crossings(self):
        """Small-amplitude dither crosses zero constantly and means nothing.

        Counting it would score the noisiest installation worst rather than
        the worst-steered one.
        """
        tracker, clock = self._make()
        _feed(tracker, [60.0, -60.0] * 100, clock=clock)
        assert tracker.snapshot().crossings_per_second == 0.0

    def test_limited_needs_to_hold_for_the_window_it_excuses(self):
        tracker, clock = self._make()
        _feed(tracker, [250.0] * 120, limited=True, clock=clock)
        # Same numbers as the off-target case; the difference is whose fault.
        assert tracker.snapshot().verdict == "limited"

    def test_one_saturated_sample_does_not_excuse_a_whole_window(self):
        """The error figure averages ~50 s, so the fault claim must too.

        Otherwise a single saturated poll retroactively excuses a minute of
        accumulated error, and the verdict flickers between two states whose
        documented remedies are opposites.
        """
        tracker, clock = self._make()
        _feed(tracker, [250.0] * 120, limited=False, clock=clock)
        _feed(tracker, [250.0], limited=True, clock=clock)
        assert tracker.snapshot().verdict == "off_target"

    def test_a_held_grid_reads_stable_even_with_no_headroom(self):
        tracker, clock = self._make()
        _feed(tracker, [4.0] * 40, limited=True, clock=clock)
        assert tracker.snapshot().verdict == "stable"

    def test_the_score_has_no_value_until_it_has_evidence(self):
        """Absent, not perfect.

        The EMAs start at zero, which reads as a flawlessly held grid — so a
        fresh window would publish 100, and a "score below X" automation would
        clear itself every time the batteries dropped out for a minute.
        """
        tracker, clock = self._make()
        assert tracker.snapshot().score is None, "fresh tracker"
        _feed(tracker, [400.0] * 60, clock=clock)
        scored = tracker.snapshot().score
        assert scored is not None and scored < 50.0
        # A gap resets the window; the score must go absent again rather than
        # jumping back to a perfect 100.
        clock.advance(120.0)
        tracker.update(400.0, steering=True, limited=False)
        assert tracker.snapshot().verdict == "warmup"
        assert tracker.snapshot().score is None
        # And once nothing is being steered at all.
        clock.advance(600.0)
        assert tracker.snapshot().verdict == "idle"
        assert tracker.snapshot().score is None

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
        assert fast.snapshot().verdict == slow.snapshot().verdict == "off_target"
        assert abs(fast.snapshot().score - slow.snapshot().score) < 1.0

    def test_a_long_gap_starts_a_new_window(self):
        tracker, clock = self._make()
        _feed(tracker, [400.0] * 60, clock=clock)
        assert tracker.snapshot().verdict == "off_target"
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
            "off_target",
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

    def test_a_discharging_dc_battery_still_has_room_for_a_surplus(self):
        """A B2500 cannot charge from AC, but it absorbs a surplus by
        discharging *less* — it only runs out of room at its MIN_DC_OUTPUT
        floor. Excusing every surplus on device type alone reported a
        symmetric hunt (half the samples in surplus, battery mid-range,
        saturation 0) as a full pack with nothing left to give: the exact
        fault the verdict exists to surface, labelled as nothing to fix.
        """
        balancer, clock = self._make()
        for i in range(400):
            clock.advance(1.0)
            grid = 400.0 if i % 2 == 0 else -400.0
            reports = {"a": {"device_type": "HMA-1", "phase": "A", "power": 300.0}}
            balancer.compute_target(
                "a",
                ConsumerMode("auto"),
                reports,
                grid,
                frozenset(),
                frozenset(),
                (grid, 0, 0),
            )
        assert balancer.control_quality().verdict == "off_target"

    def test_a_dc_battery_at_its_floor_really_is_out_of_room(self):
        balancer, clock = self._make(min_dc_output=50.0)
        for _ in range(120):
            # Already down at the floor: it cannot reduce any further.
            self._poll(balancer, clock, -400.0, reported=50.0, device_type="HMA-1")
        assert balancer.control_quality().verdict == "limited"

    def test_surplus_with_no_ac_chargeable_battery_reads_as_limited(self):
        # A DC-only pack (B2500) under surplus physically cannot absorb; the
        # export that remains is not the controller mis-steering.
        balancer, clock = self._make()
        for _ in range(60):
            self._poll(balancer, clock, -400.0, reported=0.0, device_type="HMA-1")
        assert balancer.control_quality().verdict == "limited"

    def test_a_saturated_pool_reads_as_limited(self):
        balancer, clock = self._make()
        for _ in range(60):
            self._poll(balancer, clock, 400.0)
        assert balancer.control_quality().verdict == "off_target"
        # Empty battery: commanded hard, producing nothing. It has to stay
        # that way for most of the window before the error is excused — one
        # saturated poll must not absolve the minute that preceded it.
        for _ in range(120):
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
        assert balancer.control_quality().verdict == "off_target"


class TestControlQualityOnTheMqttWire:
    """The verdict names no cause, so the evidence has to travel with it.

    Without these an MQTT-only user (no dashboard — it is opt-in outside the
    add-on) gets "off target" and nothing to act on.
    """

    def _quality(self, **kwargs):
        clock = _FakeClock()
        tracker = ControlQualityTracker(band=25.0, clock=clock)
        for value in kwargs.get("values", []):
            clock.advance(1.0)
            tracker.update(value, steering=True, limited=False)
        return tracker.snapshot()

    def test_evidence_is_published_in_the_units_the_docs_describe(self):
        quality = self._quality(values=[250.0, -250.0] * 60)
        evidence = _control_quality_evidence(quality)
        # Percent, not a 0..1 fraction; per minute, not per second.
        assert evidence["control_quality_in_band_pct"] == 0.0
        assert evidence["control_quality_crossings_per_min"] == pytest.approx(
            quality.crossings_per_second * 60, abs=0.01
        )
        assert evidence["control_quality_error_w"] == pytest.approx(250.0, abs=1.0)
        assert evidence["control_quality_band_w"] == 25.0

    def test_evidence_is_absent_before_anything_is_measured(self):
        evidence = _control_quality_evidence(self._quality())
        assert evidence["control_quality_error_w"] is None
        assert evidence["control_quality_in_band_pct"] is None
        assert evidence["control_quality_crossings_per_min"] is None
        # The band is configuration, not a measurement: always meaningful.
        assert evidence["control_quality_band_w"] == 25.0

    def test_evidence_is_real_during_warmup(self):
        """The EMAs are seeded from the first sample, so they are honest well
        before the score is — only a window with nothing in it is absent."""
        quality = self._quality(values=[900.0])
        assert quality.verdict == "warmup"
        assert quality.score is None
        assert _control_quality_evidence(quality)["control_quality_error_w"] == 900.0
