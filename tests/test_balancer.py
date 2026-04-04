"""Unit tests for balancer components: BalancerConsumerState, SaturationTracker, LoadBalancer."""

import time

from b2500_meter.ct002.balancer import (
    BalancerConfig,
    BalancerConsumerState,
    LoadBalancer,
    SaturationTracker,
)


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
        tracker = self._make_tracker(alpha=0.5, min_target=20)
        state = BalancerConsumerState()
        # First update: saturated
        tracker.update(state, 200, 0)
        assert state.saturation_score == 0.5  # 0.5*1.0 + 0.5*0.0
        # Second update: still saturated
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
