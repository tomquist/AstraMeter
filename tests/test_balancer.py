"""Unit tests for balancer components: BalancerConsumerState, SaturationTracker, LoadBalancer."""

import time

import pytest

from astrameter.ct002.balancer import (
    PRED_TRUST_MAX,
    PRED_TRUST_SHRINK,
    BalancerConfig,
    BalancerConsumerState,
    ConsumerMode,
    LoadBalancer,
    NetOutputW,
    SaturationTracker,
    to_grid_reading,
)


class TestToGridReading:
    """The single audited boundary: absolute net-output target -> meter reading.

    A grid reading is what the battery adds to its own output
    (``new_output = reported + reading``); positive = grid import.
    """

    def test_raise_output_toward_target(self):
        # Want 25 W net out, already at 10 W -> reading of +15 lands on target.
        assert to_grid_reading(NetOutputW(25), reported=10) == 15

    def test_steer_to_zero_from_discharge(self):
        # Want 0 W net out while reporting 200 W -> reading of -200 (charge).
        assert to_grid_reading(NetOutputW(0), reported=200) == -200

    def test_reported_plus_reading_lands_on_target(self):
        for target, reported in ((25.0, 10.0), (0.0, 200.0), (-100.0, 50.0)):
            reading = to_grid_reading(NetOutputW(target), reported)
            assert reported + reading == target


class _FakeClock:
    """Monotonic fake clock for deterministic time-weighted EMA tests.

    The saturation tracker uses the real wall clock to compute ``dt``
    between successive ``update()`` calls.  Tests that want to exercise
    the per-sample EMA formula (e.g. ``new = alpha * inst + (1-alpha) * prev``)
    must feed ``dt = SATURATION_REFERENCE_DT`` on every update, otherwise
    tight Python loops collapse to ``dt ≈ 0`` and the tracker's
    ``if dt == 0.0: return`` guard short-circuits the update.
    """

    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class TestBalancerConsumerState:
    def test_defaults(self):
        s = BalancerConsumerState()
        assert s.last_target is None
        assert s.fade_weight == 1.0
        assert s.saturation_score == 0.0
        assert s.saturation_grace_until == 0.0
        assert s.saturation_grace_started_at == 0.0

    def test_fields_mutate_independently(self):
        s = BalancerConsumerState()
        s.last_target = 100.0
        s.fade_weight = 0.5
        s.saturation_score = 0.3
        s.saturation_grace_until = 999.0
        s.saturation_grace_started_at = 123.0
        assert s.last_target == 100.0
        assert s.fade_weight == 0.5
        assert s.saturation_score == 0.3
        assert s.saturation_grace_until == 999.0
        assert s.saturation_grace_started_at == 123.0


class TestSaturationTracker:
    def _make_tracker(self, **kwargs):
        defaults = dict(
            alpha=0.15,
            min_target=20,
            decay_factor=0.995,
            stall_timeout_seconds=60.0,
            enabled=True,
        )
        defaults.update(kwargs)
        return SaturationTracker(**defaults)

    def _make_tracker_with_clock(self, **kwargs):
        """Return (tracker, clock) for time-weighted EMA tests.

        The clock is a :class:`_FakeClock` that the caller must
        ``advance()`` by :data:`SATURATION_REFERENCE_DT` (1.0 s) between
        updates to reproduce the per-sample EMA formula.
        """
        clock = _FakeClock()
        tracker = self._make_tracker(clock=clock, **kwargs)
        return tracker, clock

    def test_update_noop_when_disabled(self):
        tracker = self._make_tracker(enabled=False)
        state = BalancerConsumerState()
        tracker.update(state, 200, 0)
        assert state.saturation_score == 0.0

    def test_update_noop_when_last_target_none(self):
        tracker = self._make_tracker()
        state = BalancerConsumerState()
        tracker.update(state, None, 0)
        assert state.saturation_score == 0.0

    def test_update_saturated_when_actual_below_min_target(self):
        tracker = self._make_tracker(alpha=1.0, min_target=20)
        state = BalancerConsumerState()
        tracker.update(state, 200, 5)  # actual=5 < min_target=20
        assert state.saturation_score == 1.0

    def test_update_not_saturated_when_actual_at_min_target(self):
        tracker = self._make_tracker(alpha=1.0, min_target=20)
        state = BalancerConsumerState()
        tracker.update(state, 200, 20)  # actual=20 >= min_target=20
        assert state.saturation_score == 0.0

    def test_update_ema_smoothing(self):
        tracker, clock = self._make_tracker_with_clock(alpha=0.5, min_target=20)
        state = BalancerConsumerState()
        # First update: saturated.  The first sample is treated as one
        # reference period (``prev_t = now - SATURATION_REFERENCE_DT``),
        # so the per-sample EMA formula applies directly.
        tracker.update(state, 200, 0)
        assert state.saturation_score == 0.5  # 0.5*1.0 + 0.5*0.0
        # Second update one reference period later: still saturated.
        clock.advance(1.0)
        tracker.update(state, 200, 0)
        assert state.saturation_score == 0.75  # 0.5*1.0 + 0.5*0.5

    def test_update_decays_when_target_below_min(self):
        tracker = self._make_tracker(decay_factor=0.9, min_target=20)
        state = BalancerConsumerState(saturation_score=0.5)
        tracker.update(state, 10, 10)  # target_abs=10 < min_target=20
        assert abs(state.saturation_score - 0.45) < 1e-6  # 0.5 * 0.9

    def test_update_decay_floor(self):
        tracker = self._make_tracker(decay_factor=0.5, min_target=20)
        state = BalancerConsumerState(saturation_score=0.001)
        tracker.update(state, 10, 10)
        # 0.001 * 0.5 = 0.0005 < 0.001 → floored to 0.0
        assert state.saturation_score == 0.0

    def test_grace_period_skips_update(self):
        tracker = self._make_tracker(alpha=1.0, min_target=20)
        state = BalancerConsumerState(saturation_grace_until=time.time() + 100)
        tracker.update(state, 200, 0)  # actual=0, would saturate
        assert state.saturation_score == 0.0  # skipped due to grace

    def test_grace_clears_early_on_meaningful_output(self):
        tracker = self._make_tracker(alpha=1.0, min_target=20)
        now = time.time()
        state = BalancerConsumerState(
            saturation_grace_until=now + 100,
            saturation_grace_started_at=now,
        )
        tracker.update(state, 200, 50)  # actual=50 >= min_target=20
        assert state.saturation_grace_until == 0.0  # grace cleared early
        assert state.saturation_grace_started_at == 0.0
        assert state.saturation_score == 0.0  # not saturated

    def test_grace_expires(self):
        tracker = self._make_tracker(alpha=1.0, min_target=20)
        state = BalancerConsumerState(
            saturation_grace_until=time.time() - 1,
            saturation_grace_started_at=time.time() - 5,
        )
        tracker.update(state, 200, 0)
        assert state.saturation_grace_until == 0.0
        assert state.saturation_grace_started_at == 0.0
        assert state.saturation_score == 1.0  # grace expired, saturated

    def test_grace_stall_marks_immediate_saturation_after_timeout(self):
        tracker = self._make_tracker(
            alpha=0.15, min_target=20, stall_timeout_seconds=4.0
        )
        now = time.time()
        state = BalancerConsumerState(
            saturation_grace_until=now + 100,
            saturation_grace_started_at=now - 5,
        )
        tracker.update(state, 200, 0)
        assert state.saturation_score == 1.0
        assert state.saturation_grace_until == 0.0
        assert state.saturation_grace_started_at == 0.0

    def test_clear(self):
        tracker = self._make_tracker()
        state = BalancerConsumerState(
            saturation_score=0.8,
            saturation_grace_until=999,
            saturation_grace_started_at=123,
        )
        tracker.clear(state)
        assert state.saturation_score == 0.0
        assert state.saturation_grace_until == 0.0
        assert state.saturation_grace_started_at == 0.0

    def test_set_grace(self):
        tracker = self._make_tracker()
        state = BalancerConsumerState()
        tracker.set_grace(state, 42.0)
        assert state.saturation_grace_until == 42.0
        assert state.saturation_grace_started_at > 0.0

    def test_get(self):
        tracker = self._make_tracker()
        state = BalancerConsumerState(saturation_score=0.7)
        assert tracker.get(state) == 0.7

    def test_sign_reversal_decays_instead_of_accumulating(self):
        """When target and actual have opposite signs (direction reversal),
        saturation should decay rather than accumulate."""
        tracker = self._make_tracker(alpha=0.15, min_target=20, decay_factor=0.995)
        state = BalancerConsumerState()

        # Battery discharging at 100W, tracking well
        for _ in range(5):
            tracker.update(state, last_target=100.0, actual=95.0)
        assert state.saturation_score < 0.01

        # Target flips to charge (-100W), but actual is still +80W (ramping)
        for _ in range(20):
            tracker.update(state, last_target=-100.0, actual=80.0)
        # Score must NOT have accumulated — should have stayed near zero
        assert state.saturation_score < 0.01, (
            f"Saturation score {state.saturation_score:.3f} should not "
            f"accumulate during sign reversal"
        )

    def test_sign_reversal_existing_score_decays(self):
        """Pre-existing saturation score decays during sign reversal."""
        tracker = self._make_tracker(decay_factor=0.9)
        state = BalancerConsumerState(saturation_score=0.5)

        # Target positive, actual negative (battery reversing)
        for _ in range(10):
            tracker.update(state, last_target=100.0, actual=-50.0)
        assert state.saturation_score < 0.5, (
            f"Score should decay during sign reversal, got {state.saturation_score:.3f}"
        )

    def test_same_sign_low_output_still_accumulates(self):
        """When signs agree but output is below min_target, saturation
        should still accumulate (genuine saturation)."""
        tracker, clock = self._make_tracker_with_clock(alpha=0.15, min_target=20)
        state = BalancerConsumerState()

        # Target +100W but actual only +5W (same sign, truly saturated).
        # Advance the fake clock by SATURATION_REFERENCE_DT (1.0 s) per
        # update so the time-weighted EMA applies one full per-sample
        # step each iteration.
        for _ in range(20):
            tracker.update(state, last_target=100.0, actual=5.0)
            clock.advance(1.0)
        assert state.saturation_score > 0.4, (
            f"Saturation score {state.saturation_score:.3f} should accumulate "
            f"when signs agree but output is low"
        )

    def test_actual_zero_during_reversal_does_not_trigger_guard(self):
        """actual=0 should NOT activate the sign-reversal guard (actual
        has no sign), so saturation accumulates normally."""
        tracker, clock = self._make_tracker_with_clock(alpha=0.15, min_target=20)
        state = BalancerConsumerState()

        # Target is -100W (charge) but actual is exactly 0
        for _ in range(20):
            tracker.update(state, last_target=-100.0, actual=0.0)
            clock.advance(1.0)
        assert state.saturation_score > 0.4, (
            f"Saturation score {state.saturation_score:.3f} should accumulate "
            f"when actual is zero (no sign to disagree)"
        )

    def test_target_zero_does_not_trigger_guard(self):
        """target=0 should NOT activate the sign-reversal guard."""
        tracker = self._make_tracker(alpha=0.15, min_target=20, decay_factor=0.995)
        state = BalancerConsumerState(saturation_score=0.5)

        # Target is 0, actual is positive — target_abs < min_target so
        # the low-target decay path handles this, not the sign guard.
        for _ in range(10):
            tracker.update(state, last_target=0.0, actual=50.0)
        # Score should have decayed via the low-target path
        assert state.saturation_score < 0.5

    def test_sign_reversal_then_same_sign_resumes_accumulation(self):
        """After a sign reversal period, once actual crosses zero to match
        target sign, saturation tracking resumes normally."""
        tracker, clock = self._make_tracker_with_clock(alpha=0.15, min_target=20)
        state = BalancerConsumerState()

        # Phase 1: sign reversal — target negative, actual positive
        for _ in range(10):
            tracker.update(state, last_target=-100.0, actual=50.0)
            clock.advance(1.0)
        score_after_reversal = state.saturation_score
        assert score_after_reversal < 0.01

        # Phase 2: actual crosses zero but is small (same sign, low output)
        for _ in range(20):
            tracker.update(state, last_target=-100.0, actual=-5.0)
            clock.advance(1.0)
        assert state.saturation_score > 0.4, (
            f"Saturation score {state.saturation_score:.3f} should accumulate "
            f"once signs agree again"
        )

    def test_saturation_rise_is_sample_rate_invariant(self):
        """Two trackers polled at different cadences must converge to
        the same score after the same wall-clock window.

        This is the core regression guard for the V3-vs-V2 polling
        oscillation report: V3 batteries poll every ~0.45 s while V2
        batteries poll every ~3.1 s, and under the old per-sample EMA
        they drifted to completely different saturation scores for the
        same physical saturation condition.  Under the time-weighted
        formula the same 30 s wall-clock window must produce the same
        score on both cadences (tolerance allows for the discrete-step
        truncation error near the transient start).
        """
        window_seconds = 30.0

        def drive(dt: float) -> float:
            clock = _FakeClock()
            tracker = self._make_tracker(
                alpha=0.15, min_target=20, decay_factor=0.995, clock=clock
            )
            state = BalancerConsumerState()
            # Seed ``last_saturation_update`` so the first iteration uses
            # the test's ``dt`` rather than the default reference-period
            # bootstrap (which would skew fast vs slow by ~2.5 s of
            # effective EMA time).
            state.last_saturation_update = clock()
            elapsed = 0.0
            while elapsed < window_seconds - 1e-9:
                clock.advance(dt)
                tracker.update(state, last_target=100.0, actual=5.0)
                elapsed += dt
            return state.saturation_score

        fast = drive(0.5)  # V3-like cadence
        slow = drive(3.0)  # V2-like cadence
        assert abs(fast - slow) < 0.02, (
            f"Rise EMA is not sample-rate invariant: "
            f"fast(dt=0.5s)={fast:.4f} vs slow(dt=3.0s)={slow:.4f}"
        )
        # And the shared score must reflect meaningful saturation over
        # 30 s of "cannot follow target" — otherwise both trackers are
        # stuck at zero and the "invariance" is vacuous.
        assert fast > 0.4

    def test_saturation_decay_is_sample_rate_invariant(self):
        """Decay branch of the EMA must also be rate-invariant.

        Same wall-clock window, pre-seeded with a non-trivial score and
        a low target to force the decay path on every update.
        """
        window_seconds = 60.0

        def drive(dt: float) -> float:
            clock = _FakeClock()
            tracker = self._make_tracker(
                alpha=0.15, min_target=20, decay_factor=0.9, clock=clock
            )
            state = BalancerConsumerState(saturation_score=0.8)
            state.last_saturation_update = clock()
            elapsed = 0.0
            while elapsed < window_seconds - 1e-9:
                clock.advance(dt)
                tracker.update(state, last_target=5.0, actual=5.0)
                elapsed += dt
            return state.saturation_score

        fast = drive(0.5)
        slow = drive(3.0)
        assert abs(fast - slow) < 0.02, (
            f"Decay EMA is not sample-rate invariant: "
            f"fast(dt=0.5s)={fast:.4f} vs slow(dt=3.0s)={slow:.4f}"
        )
        # Sanity: the decay must actually progress over 60 s.
        assert fast < 0.8

    def test_long_gap_between_updates_is_dropped(self):
        """Gaps above SATURATION_LONG_GAP_SECONDS re-seed instead of
        dosing the EMA with one huge rise/decay step."""
        from astrameter.ct002.balancer import SATURATION_LONG_GAP_SECONDS

        clock = _FakeClock()
        tracker = self._make_tracker(alpha=0.15, min_target=20, clock=clock)
        state = BalancerConsumerState(saturation_score=0.5)
        # Simulate a battery dropping off the network for well over
        # the long-gap threshold, then reporting again.
        state.last_saturation_update = clock()
        clock.advance(SATURATION_LONG_GAP_SECONDS + 10.0)
        tracker.update(state, last_target=100.0, actual=5.0)
        # The score must not have moved by a huge amount — the gap
        # should have been dropped and the update re-seeded.
        assert state.saturation_score == 0.5


class TestLoadBalancerLifecycle:
    def _make_balancer(self, **kwargs):
        cfg_kwargs = {}
        balancer_kwargs = {}
        cfg_fields = {f.name for f in BalancerConfig.__dataclass_fields__.values()}
        for k, v in kwargs.items():
            if k in cfg_fields:
                cfg_kwargs[k] = v
            else:
                balancer_kwargs[k] = v
        return LoadBalancer(
            config=BalancerConfig(**cfg_kwargs),
            saturation_alpha=balancer_kwargs.pop("saturation_alpha", 0.15),
            saturation_min_target=balancer_kwargs.pop("saturation_min_target", 20),
            saturation_decay_factor=balancer_kwargs.pop(
                "saturation_decay_factor", 0.995
            ),
            saturation_grace_seconds=balancer_kwargs.pop(
                "saturation_grace_seconds", 90.0
            ),
            saturation_stall_timeout_seconds=balancer_kwargs.pop(
                "saturation_stall_timeout_seconds", 60.0
            ),
            **balancer_kwargs,
        )

    def test_get_consumer_auto_vivifies(self):
        lb = self._make_balancer()
        state = lb._get_consumer("x")
        assert state.last_target is None
        assert state.fade_weight == 1.0
        assert "x" in lb._consumers

    def test_get_consumer_returns_existing(self):
        lb = self._make_balancer()
        s1 = lb._get_consumer("x")
        s1.last_target = 42.0
        s2 = lb._get_consumer("x")
        assert s2.last_target == 42.0
        assert s1 is s2

    def test_remove_consumer(self):
        lb = self._make_balancer()
        lb._get_consumer("x")
        lb._priority.append("x")
        lb._deprioritized.add("x")
        lb.remove_consumer("x")
        assert "x" not in lb._consumers
        assert "x" not in lb._priority
        assert "x" not in lb._deprioritized

    def test_remove_nonexistent_is_noop(self):
        lb = self._make_balancer()
        lb.remove_consumer("nope")  # should not raise

    def test_reset_consumer(self):
        lb = self._make_balancer()
        state = lb._get_consumer("x")
        state.last_target = 100.0
        state.saturation_score = 0.5
        lb.reset_consumer("x")
        assert state.last_target is None
        assert state.saturation_score == 0.0
        assert state.saturation_grace_until > time.time()
        assert state.saturation_grace_started_at > 0.0

    def test_detach_from_auto_pool(self):
        lb = self._make_balancer()
        lb._get_consumer("x")
        lb._priority.append("x")
        lb._deprioritized.add("x")
        lb.detach_from_auto_pool("x")
        assert "x" not in lb._consumers
        assert "x" not in lb._priority
        assert "x" not in lb._deprioritized

    def test_get_saturation_unknown_consumer(self):
        lb = self._make_balancer()
        assert lb.get_saturation("unknown") == 0.0

    def test_get_last_target_unknown_consumer(self):
        lb = self._make_balancer()
        assert lb.get_last_target("unknown") is None


class TestPaceReading:
    """Ramp pacing for the auto path (issue #458) — mirrored by the C++
    host test ``LoadBalancer.PaceReadingCapsGrowsAndResets``.

    The caps are W per ``PACE_REFERENCE_DT``; these tests drive a fake
    clock at the reference cadence (1 s per poll), where the time-based
    law reduces to the original per-poll semantics."""

    def _make_balancer(self, **cfg_kwargs):
        cfg_kwargs.setdefault("fair_distribution", False)
        # These tests exercise ramp pacing in isolation, asserting the cap
        # against a residual that equals the raw grid; the adaptive grid-state
        # predictor (on by default) would act on a different, predicted grid, so
        # disable it here to keep the pacing math under test.
        cfg_kwargs.setdefault("grid_predict_trust", 0.0)
        self.clock = _FakeClock()
        return LoadBalancer(
            config=BalancerConfig(**cfg_kwargs),
            saturation_alpha=0.15,
            saturation_min_target=20,
            saturation_decay_factor=0.995,
            saturation_grace_seconds=90.0,
            saturation_stall_timeout_seconds=60.0,
            saturation_enabled=False,
            clock=self.clock,
        )

    def _auto(self, lb, reported, grid, dt=1.0):
        self.clock.advance(dt)
        reports = {"a": {"device_type": "HMG-50", "phase": "A", "power": reported}}
        return lb.compute_target(
            "a", ConsumerMode("auto"), reports, grid, frozenset(), frozenset(), ()
        )[0]

    def test_caps_grows_when_tracking_and_resets_on_reversal(self):
        lb = self._make_balancer(pace_base_step=50, pace_max_step=200)
        # First poll: a 600 W demand is capped to the base step.
        assert self._auto(lb, 0, 600) == 50.0
        # Battery did not move (startup delay): cap stays at the base step.
        assert self._auto(lb, 0, 600) == 50.0
        # Battery tracks (+50 W toward the command): cap doubles.
        assert self._auto(lb, 50, 550) == 100.0
        # Tracks again (+100 W): cap doubles to the configured max.
        assert self._auto(lb, 150, 450) == 200.0
        # Tracks again, but the max holds.
        assert self._auto(lb, 350, 250) == 200.0
        # Error fits under the cap: passes through, cap follows it down.
        assert self._auto(lb, 520, 80) == 80.0
        # Direction reversal: cap resets to the base step.
        assert self._auto(lb, 600, -300) == -50.0

    def test_slow_firmware_crawl_still_bootstraps_the_cap(self):
        """Issue #469 follow-up: the HMG ramp law can step as little as
        10 W/poll on a constant reading (its sqrt term collapses when its
        internal reference captures the reading itself).  The tracking
        threshold must sit below that so the cap still grows and the loop
        escapes the crawl, instead of deadlocking at 10 W/poll."""
        lb = self._make_balancer(pace_base_step=50, pace_max_step=200)
        assert self._auto(lb, 0, 2000) == 50.0
        # Battery crawls at +10 W/poll — below the old 20 W threshold,
        # above the new 5 W one: the cap must double anyway.
        assert self._auto(lb, 10, 1990) == 100.0
        assert self._auto(lb, 20, 1980) == 200.0

    def test_fast_poller_clamp_scales_with_cadence(self):
        """The caps are W per reference second: a 0.5 s poller's grown cap
        clamps at half the per-poll value (same W/s slew), floored at the
        base step so coarse hysteresis regulators still get a reading big
        enough to clear their input hold window."""
        lb = self._make_balancer(pace_base_step=50, pace_max_step=200)
        assert self._auto(lb, 0, 600, dt=0.5) == 50.0  # base floor
        # Tracking at +25 W per 0.5 s (50 W/s): growth gate scales too
        # (5 * 0.5 = 2.5 W).  Cap grows by 2**0.5 per poll; the sent
        # reading stays floored at the base step until cap * 0.5 > 50.
        assert self._auto(lb, 25, 575, dt=0.5) == pytest.approx(50.0)  # cap ~70.7
        assert self._auto(lb, 50, 550, dt=0.5) == pytest.approx(50.0)  # cap = 100
        reading = self._auto(lb, 75, 525, dt=0.5)  # cap ~141.4
        assert reading == pytest.approx(70.71, abs=0.01)
        reading = self._auto(lb, 110, 490, dt=0.5)  # cap = 200
        assert reading == pytest.approx(100.0, abs=0.01)
        # At the max cap the per-poll clamp is half the 1 s value: the
        # battery integrates the same 200 W/s either way.
        assert self._auto(lb, 160, 440, dt=0.5) == pytest.approx(100.0, abs=0.01)

    def test_cap_clamped_to_max_after_fast_polls(self):
        # The else branch back-computes the cap as abs(reading) / dt_ratio; a
        # very fast poll could push that above pace_max_step. It must be
        # clamped so a later normal-cadence poll can't slew past pace_max_step.
        lb = self._make_balancer(pace_base_step=50, pace_max_step=200)
        # Two fast polls at the base step: the second hits the else branch and
        # would store cap = 50 / 0.05 = 1000 W without the clamp.
        self._auto(lb, 0, 50, dt=0.05)
        self._auto(lb, 0, 50, dt=0.05)
        # A subsequent normal-cadence, high-demand poll must still be bounded by
        # pace_max_step (200), not the inflated cap.
        assert self._auto(lb, 0, 5000, dt=1.0) == pytest.approx(200.0, abs=0.01)

    def test_deprioritized_wind_down_is_paced(self):
        """A consumer faded out by efficiency mode is steered to zero
        through the pacing cap — the firmware applies a charge-direction
        reading in full in one cycle, so an unpaced wind-down would dump
        its whole output on the pool in one poll (issue #469 follow-up)."""
        lb = self._make_balancer(
            pace_base_step=50, pace_max_step=200, min_efficient_power=400
        )
        reports = {
            "a": {"device_type": "HMG-50", "phase": "A", "power": 300},
            "b": {"device_type": "HMG-50", "phase": "A", "power": 300},
        }

        def target_for(cid):
            self.clock.advance(1.0)
            return lb.compute_target(
                cid,
                ConsumerMode("auto"),
                reports,
                0.0,
                frozenset(),
                frozenset(),
                (self.clock(),),
            )[0]

        # 600 W demand over two units is below min_efficient_power * 2:
        # one unit gets deprioritized and fades toward zero.
        deltas = [target_for("b") for _ in range(40)]
        assert lb._deprioritized == {"b"}
        # Every wind-down step is bounded by the pacing cap — never the
        # full -300 W one-shot.
        assert all(d >= -200.0 for d in deltas)
        assert min(deltas) < 0

    def test_zero_base_disables_pacing(self):
        lb = self._make_balancer(pace_base_step=0)
        assert self._auto(lb, 0, 600) == 600.0

    def test_pace_max_clamped_to_at_least_base(self):
        cfg = BalancerConfig(pace_base_step=80, pace_max_step=10)
        assert cfg.pace_max_step == 80

    def test_small_errors_pass_unclamped(self):
        lb = self._make_balancer(pace_base_step=50, pace_max_step=200)
        assert self._auto(lb, 0, 30) == 30.0

    def test_manual_and_inactive_paths_are_not_paced(self):
        lb = self._make_balancer(pace_base_step=50, pace_max_step=200)
        reports = {"a": {"device_type": "HMG-50", "phase": "A", "power": 600}}
        manual = lb.compute_target(
            "a", ConsumerMode("manual", 0.0), reports, 0, frozenset(), frozenset(), ()
        )
        assert manual[0] == -600.0
        inactive = lb.compute_target(
            "a", ConsumerMode("inactive"), reports, 0, frozenset(), frozenset(), ()
        )
        assert inactive[0] == -600.0


class TestGridPredictor:
    """Adaptive grid-state predictor (``grid_predict_trust``).

    The C++ port is covered by the differential parity suite, which now threads
    a grid-derived ``sample_id`` so the meter-correction / trust-adaptation
    branch is exercised there too; these tests pin the Python contract directly.

    Pacing and oscillation damping are disabled so the single consumer's
    returned reading equals the predicted grid the control path acted on
    (``fair_distribution=False`` ⇒ residual = fair_share = predicted grid).
    """

    def _make(self, **cfg):
        cfg.setdefault("fair_distribution", False)
        cfg.setdefault("pace_base_step", 0.0)
        cfg.setdefault("osc_damp_max", 0.0)
        self.clock = _FakeClock()
        return LoadBalancer(
            config=BalancerConfig(**cfg),
            saturation_alpha=0.15,
            saturation_min_target=20,
            saturation_decay_factor=0.995,
            saturation_grace_seconds=90.0,
            saturation_stall_timeout_seconds=60.0,
            saturation_enabled=False,
            clock=self.clock,
        )

    def _grid(self, lb, reported, grid):
        # sample_id = (grid,) mirrors production (the meter reading), so a
        # changed grid is a fresh sample.
        reports = {"a": {"device_type": "HMG-50", "phase": "A", "power": reported}}
        return lb.compute_target(
            "a", ConsumerMode("auto"), reports, grid, frozenset(), frozenset(), (grid,)
        )[0]

    def test_disabled_is_raw_passthrough(self):
        lb = self._make(grid_predict_trust=0.0)
        assert self._grid(lb, 0, 300) == 300.0
        # Reported output never feeds back when the predictor is off.
        assert self._grid(lb, 100, 300) == 300.0

    def test_first_sample_returns_raw_grid(self):
        lb = self._make(grid_predict_trust=0.5)
        assert self._grid(lb, 0, 300) == 300.0

    def test_credits_delivered_output_within_a_sample(self):
        """Between meter refreshes (same sample_id) the estimate falls by the
        pool's reported output change, so the loop commands only the remainder
        instead of re-issuing the in-flight correction."""
        lb = self._make(grid_predict_trust=0.5)
        assert self._grid(lb, 0, 300) == 300.0  # init
        # Same grid → same sample → no meter correction, only output crediting.
        assert self._grid(lb, 120, 300) == pytest.approx(180.0)
        assert self._grid(lb, 300, 300) == pytest.approx(0.0)

    def test_meter_correction_uses_trust_on_a_fresh_sample(self):
        lb = self._make(grid_predict_trust=0.5)
        assert self._grid(lb, 0, 0) == 0.0  # init, trust seeded to 0.5
        # Fresh sample: innovation 200, first significant one raises trust to
        # 0.7 (0.5 seed + PRED_TRUST_RAISE_STEP), estimate += 0.7 * 200.
        assert self._grid(lb, 0, 200) == pytest.approx(140.0)

    def test_trust_rises_on_sustained_step_and_collapses_on_reversal(self):
        lb = self._make(grid_predict_trust=0.5)
        self._grid(lb, 0, 0)
        # A sustained same-sign run drives the trust up to the cap.
        for g in (200, 400, 600):
            self._grid(lb, 0, g)
        assert lb._pred_trust == pytest.approx(PRED_TRUST_MAX)
        # A single sign reversal (the signature of hunting) cuts it hard.
        self._grid(lb, 0, -200)
        assert lb._pred_trust < PRED_TRUST_MAX * PRED_TRUST_SHRINK + 1e-6


class TestConcentrateDeadband:
    """Opt-in deadband concentration: when the grid error is small enough that a
    fair-share split would drop each battery below its firmware deadband, the
    whole correction is handed to the single most-active battery. Mirrored by the
    C++ port and the differential parity suite."""

    def _lb(self, **cfg):
        # Disable the grid predictor so the test exercises the raw control grid
        # directly (concentration acts on the same control grid either way).
        cfg.setdefault("grid_predict_trust", 0.0)
        return LoadBalancer(
            config=BalancerConfig(**cfg),
            saturation_alpha=0.15,
            saturation_min_target=20,
            saturation_decay_factor=0.995,
            saturation_grace_seconds=90.0,
            saturation_stall_timeout_seconds=60.0,
            saturation_enabled=False,
            clock=_FakeClock(),
        )

    def _reports(self):
        return {
            "a": {"device_type": "HMG-50", "phase": "A", "power": 200},
            "b": {"device_type": "HMG-50", "phase": "A", "power": 100},
        }

    def _target(self, lb, cid, reports, grid):
        return lb.compute_target(
            cid, ConsumerMode("auto"), reports, grid, frozenset(), frozenset(), ()
        )[0]

    def test_disabled_splits_the_correction(self):
        lb = self._lb(concentrate_deadband=0)  # explicitly off
        reports = self._reports()
        a = self._target(lb, "a", reports, 30)
        b = self._target(lb, "b", reports, 30)
        assert a > 0 and b > 0

    def test_cross_phase_pool_is_not_concentrated(self):
        # control_grid sums phases, so concentration must not fire when the
        # batteries are on different phases (it would over-correct one phase).
        lb = self._lb(concentrate_deadband=60)
        reports = {
            "a": {"device_type": "HMG-50", "phase": "A", "power": 200},
            "b": {"device_type": "HMG-50", "phase": "B", "power": 100},
        }

        def total(cid):
            return sum(
                lb.compute_target(
                    cid, ConsumerMode("auto"), reports, 30, frozenset(), frozenset(), ()
                )
            )

        # Both still take a share (no concentration on a mixed-phase pool); if
        # concentration had fired, the non-designated battery's total would be 0.
        assert total("a") != 0 and total("b") != 0

    def test_small_error_concentrated_on_most_active_battery(self):
        lb = self._lb(concentrate_deadband=60)
        reports = self._reports()
        # 30 W error < 60 W threshold: the most-active battery (a, 200 W) takes
        # the whole correction; the other is left untouched.
        a = self._target(lb, "a", reports, 30)
        b = self._target(lb, "b", reports, 30)
        assert a == pytest.approx(30.0, abs=1.0)
        assert b == pytest.approx(0.0, abs=1e-6)

    def test_large_error_still_split(self):
        lb = self._lb(concentrate_deadband=60)
        reports = self._reports()
        # 200 W error >= 60 W threshold: normal fair-share split, both react.
        a = self._target(lb, "a", reports, 200)
        b = self._target(lb, "b", reports, 200)
        assert a > 0 and b > 0

    def test_single_battery_unaffected(self):
        lb = self._lb(concentrate_deadband=60)
        reports = {"a": {"device_type": "HMG-50", "phase": "A", "power": 200}}
        assert self._target(lb, "a", reports, 30) == pytest.approx(30.0, abs=1.0)

    def test_zero_weight_battery_not_designated(self):
        lb = self._lb(concentrate_deadband=60)
        # ``a`` is the most-active (200 W) but configured to take no share
        # (weight 0). Without excluding it from candidates it would be picked as
        # designee and swallow the whole correction; instead ``b`` (next most
        # active among the weighted batteries) takes it and ``a`` stays at 0.
        # Needs a third battery so the candidate set still has >1 after dropping
        # ``a`` (otherwise concentration wouldn't fire at all).
        reports = {
            "a": {"device_type": "HMG-50", "phase": "A", "power": 200, "weight": 0.0},
            "b": {"device_type": "HMG-50", "phase": "A", "power": 100},
            "c": {"device_type": "HMG-50", "phase": "A", "power": 50},
        }
        assert self._target(lb, "a", reports, 30) == pytest.approx(0.0, abs=1e-6)
        assert self._target(lb, "b", reports, 30) == pytest.approx(30.0, abs=1.0)
        assert self._target(lb, "c", reports, 30) == pytest.approx(0.0, abs=1e-6)


class TestDampOscillation:
    """Oscillation-gated residual damping (issue #473) — mirrored by the C++
    host test ``LoadBalancer.DampOscillation`` and the differential parity
    suite (both stacks run the same damper on the same residual stream)."""

    def _lb(self, **cfg):
        cfg.setdefault("osc_damp_max", 0.8)
        # Intentionally above BalancerConfig's 0.15 default: a larger alpha
        # accumulates the score in fewer reversals, so the assertions below
        # (e.g. one reversal -> factor 1 - 0.8*0.25 = 0.8) use round numbers.
        cfg.setdefault("osc_damp_alpha", 0.25)
        cfg.setdefault("osc_damp_decay", 0.1)
        cfg.setdefault("osc_damp_threshold", 450)
        return LoadBalancer(
            config=BalancerConfig(**cfg),
            saturation_alpha=0.15,
            saturation_min_target=20,
            saturation_decay_factor=0.995,
            saturation_grace_seconds=90.0,
            saturation_stall_timeout_seconds=60.0,
            saturation_enabled=False,
        )

    def test_steady_same_sign_is_not_damped(self):
        # A genuine load step holds one sign: the residual passes through.
        lb = self._lb()
        for _ in range(10):
            assert lb._damp_oscillation("a", 100.0) == pytest.approx(100.0)

    def test_sustained_reversals_are_damped(self):
        # A hunting limit cycle (sign flips every poll) accumulates the score
        # and shrinks the residual toward (1 - osc_damp_max) of its magnitude.
        lb = self._lb()
        outs = [
            lb._damp_oscillation("a", 100.0 if i % 2 == 0 else -100.0)
            for i in range(20)
        ]
        # Early polls are near full magnitude; once the score saturates the
        # magnitude is cut by ~osc_damp_max (0.8 -> ~20 of 100).
        assert abs(outs[1]) > abs(outs[-1])
        assert abs(outs[-1]) == pytest.approx(20.0, abs=2.0)

    def test_large_residual_bypasses_damping_even_while_hunting(self):
        # Drive the score up with small reversals, then a step above the
        # threshold must react at full gain (not be bled by the prior hunt).
        lb = self._lb(osc_damp_threshold=450)
        for i in range(20):
            lb._damp_oscillation("a", 100.0 if i % 2 == 0 else -100.0)
        assert lb._damp_oscillation("a", 1500.0) == pytest.approx(1500.0)

    def test_single_reversal_barely_damps(self):
        # One sign flip (e.g. a solar ramp crossing zero once) only adds
        # osc_damp_alpha to the score, so the response stays near full gain.
        lb = self._lb()
        for _ in range(8):
            lb._damp_oscillation("a", 100.0)
        out = lb._damp_oscillation("a", -100.0)
        # score == alpha (0.25) -> factor 1 - 0.8*0.25 = 0.8 -> ~80 of 100.
        assert abs(out) == pytest.approx(80.0, abs=1.0)

    def test_disabled_when_max_zero(self):
        lb = self._lb(osc_damp_max=0.0)
        for i in range(10):
            r = 100.0 if i % 2 == 0 else -100.0
            assert lb._damp_oscillation("a", r) == pytest.approx(r)
