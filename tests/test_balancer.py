"""Unit tests for balancer components: BalancerConsumerState, SaturationTracker, LoadBalancer."""

import time

from astrameter.ct002.balancer import (
    BalancerConfig,
    BalancerConsumerState,
    LoadBalancer,
    SaturationTracker,
)


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
