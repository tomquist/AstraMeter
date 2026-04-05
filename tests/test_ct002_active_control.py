"""Tests for CT002 active control, fair distribution, and saturation detection."""

import dataclasses
import time

from astrameter.ct002.balancer import ProbeState
from astrameter.ct002.ct002 import CT002


class TestActiveControl:
    """Tests for smooth target and load splitting."""

    def test_smooth_target_splits_across_consumers(self):
        device = CT002(active_control=True, fair_distribution=False)
        device._update_consumer_report("a", "A", 100)
        device._update_consumer_report("b", "A", 100)
        out = device._compute_smooth_target([400, 0, 0], "a")
        assert out[0] == 200
        assert out[1] == 0
        assert out[2] == 0

    def test_smooth_target_ema_smooths_raw_input(self):
        device = CT002(
            active_control=True,
            fair_distribution=False,
            smooth_target_alpha=0.5,
        )
        device._update_consumer_report("a", "A", 0)
        first = device._compute_smooth_target([400, 0, 0], "a")
        second = device._compute_smooth_target([100, 0, 0], "a")
        assert first[0] == 400
        assert second[0] == 250

    def test_active_control_off_passes_through_values(self):
        device = CT002(active_control=False)
        device._update_consumer_report("a", "A", 0)
        out = device._compute_smooth_target([100, 50, 25], "a")
        assert out == [100, 50, 25]

    def test_no_consumer_id_returns_fair_share(self):
        device = CT002(active_control=True, fair_distribution=False)
        device._update_consumer_report("a", "A", 0)
        out = device._compute_smooth_target([300, 0, 0], None)
        assert out[0] == 300

    def test_active_control_splits_target_across_detected_phases(self):
        device = CT002(active_control=True, fair_distribution=False)
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "B", 0)

        out = device._compute_smooth_target([400, 0, 0], "a")

        assert out[0] == 100
        assert out[1] == 100
        assert out[2] == 0

    def test_deadband_decays_smoothed_toward_zero(self):
        """When raw total is within deadband, smoothed target should decay
        toward zero rather than holding stale values."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            smooth_target_alpha=0.3,
            deadband=20,
        )
        device._update_consumer_report("a", "A", 0)
        # Set a large initial smoothed target
        device._compute_smooth_target([500, 0, 0], "a")
        assert device._smoother.value == 500

        # Feed readings within deadband (grid balanced).
        # Each call uses a unique value so the sample-dedup sees a fresh reading.
        for i in range(20):
            device._compute_smooth_target([i, 0, 0], "a")

        # Smoothed should have decayed significantly toward zero
        assert device._smoother.value < 10

    def test_deadband_decay_does_not_overshoot_zero(self):
        """Deadband decay should not make smoothed target cross zero."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            smooth_target_alpha=0.5,
            deadband=20,
        )
        device._update_consumer_report("a", "A", 0)
        device._compute_smooth_target([100, 0, 0], "a")
        # Decay multiple times with unique values within deadband
        for i in range(50):
            device._compute_smooth_target([i % 19, 0, 0], "a")
        # Should approach zero but stay non-negative
        assert device._smoother.value >= 0

    def test_smoothing_applies_once_per_sample(self):
        """Multiple consumers calling with the same meter reading should
        not compound the smoothing update."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            smooth_target_alpha=0.5,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([400, 0, 0], "a")
        assert device._smoother.value == 400

        # Two consumers call with the same new reading
        device._compute_smooth_target([100, 0, 0], "a")
        after_first = device._smoother.value
        device._compute_smooth_target([100, 0, 0], "b")
        after_second = device._smoother.value

        # Smoothing should have applied only once
        assert after_first == 250  # 400 + 0.5*(100-400)
        assert after_second == 250  # unchanged


class TestFairDistribution:
    """Tests for fair load distribution across consumers."""

    def test_underperforming_consumer_gets_higher_target(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            balance_gain=0.3,
            balance_deadband=0,
            deadband=0,
            max_correction_per_step=0,
            max_target_step=0,
        )
        device._update_consumer_report("a", "A", 44)
        device._update_consumer_report("b", "A", 356)
        out_a = device._compute_smooth_target([400, 0, 0], "a")
        assert out_a[0] > 200

    def test_overperforming_consumer_gets_lower_target(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            balance_gain=0.3,
            balance_deadband=0,
            deadband=0,
            max_correction_per_step=0,
            max_target_step=0,
        )
        device._update_consumer_report("a", "A", 44)
        device._update_consumer_report("b", "A", 356)
        out_b = device._compute_smooth_target([400, 0, 0], "b")
        assert out_b[0] < 200

    def test_fair_distribution_off_gives_equal_share(self):
        device = CT002(
            active_control=True,
            fair_distribution=False,
        )
        device._update_consumer_report("a", "A", 44)
        device._update_consumer_report("b", "A", 356)
        out_a = device._compute_smooth_target([400, 0, 0], "a")
        out_b = device._compute_smooth_target([400, 0, 0], "b")
        assert out_a[0] == out_b[0] == 200

    def test_balance_gain_zero_no_correction(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            balance_gain=0,
            balance_deadband=0,
            deadband=0,
            max_correction_per_step=0,
            max_target_step=0,
        )
        device._update_consumer_report("a", "A", 44)
        device._update_consumer_report("b", "A", 356)
        out_a = device._compute_smooth_target([400, 0, 0], "a")
        out_b = device._compute_smooth_target([400, 0, 0], "b")
        assert out_a[0] == out_b[0] == 200

    def test_large_error_gets_boosted_correction(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            balance_gain=0.3,
            error_boost_threshold=100,
            error_boost_max=1.0,
            balance_deadband=0,
            deadband=0,
            max_correction_per_step=0,
            max_target_step=0,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 400)
        out_a = device._compute_smooth_target([400, 0, 0], "a")
        out_b = device._compute_smooth_target([400, 0, 0], "b")
        assert out_a[0] > 250
        assert out_b[0] < 150

    def test_error_boost_disabled_when_threshold_zero(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            balance_gain=0.3,
            error_boost_threshold=0,
            balance_deadband=0,
            deadband=0,
            max_correction_per_step=0,
            max_target_step=0,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 400)
        out_a = device._compute_smooth_target([400, 0, 0], "a")
        out_b = device._compute_smooth_target([400, 0, 0], "b")
        assert out_a[0] == 260
        assert out_b[0] == 140

    def test_small_offset_gets_small_adjustment(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            balance_gain=0.3,
            error_reduce_threshold=20,
        )
        device._update_consumer_report("a", "A", 95)
        device._update_consumer_report("b", "A", 105)
        out_a = device._compute_smooth_target([200, 0, 0], "a")
        out_b = device._compute_smooth_target([200, 0, 0], "b")
        assert 98 < out_a[0] < 102
        assert 98 < out_b[0] < 102

    def test_error_reduce_disabled_when_threshold_zero(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            balance_gain=0.3,
            error_reduce_threshold=0,
            error_boost_threshold=0,
            balance_deadband=0,
            deadband=0,
            max_correction_per_step=0,
            max_target_step=0,
        )
        device._update_consumer_report("a", "A", 90)
        device._update_consumer_report("b", "A", 110)
        out_a = device._compute_smooth_target([200, 0, 0], "a")
        assert out_a[0] == 103

    def test_balance_deadband_skips_small_correction(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            balance_gain=0.3,
            balance_deadband=25,
            deadband=0,
            max_correction_per_step=0,
            max_target_step=0,
        )
        device._update_consumer_report("a", "A", 95)
        device._update_consumer_report("b", "A", 105)
        out_a = device._compute_smooth_target([200, 0, 0], "a")
        assert out_a[0] == 100

    def test_max_correction_per_step_caps_correction(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            balance_gain=0.5,
            balance_deadband=0,
            deadband=0,
            max_correction_per_step=50,
            max_target_step=0,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 400)
        out_a = device._compute_smooth_target([400, 0, 0], "a")
        assert 200 < out_a[0] <= 250

    def test_max_target_step_caps_target_vs_actual(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            balance_gain=0.5,
            balance_deadband=0,
            deadband=0,
            max_correction_per_step=0,
            max_target_step=100,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 400)
        out_a = device._compute_smooth_target([400, 0, 0], "a")
        assert out_a[0] == 100


class TestSaturationDetection:
    """Tests for saturation detection (full/empty battery)."""

    def test_saturated_consumer_gets_reduced_share(self):
        device = CT002(
            active_control=True,
            fair_distribution=True,
            saturation_detection=True,
            saturation_alpha=1.0,
            min_target_for_saturation=10,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 400)
        device._balancer._get_consumer("a").last_target = 200
        device._balancer._get_consumer("b").last_target = 200
        out_a = device._compute_smooth_target([400, 0, 0], "a")
        out_b = device._compute_smooth_target([400, 0, 0], "b")
        assert out_a[0] < out_b[0]
        assert out_b[0] > 200

    def test_saturation_ema_smooths_in(self):
        device = CT002(
            active_control=True,
            fair_distribution=False,
            saturation_detection=True,
            saturation_alpha=0.5,
            min_target_for_saturation=10,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 200)
        device._balancer._get_consumer("a").last_target = 200
        device._balancer._get_consumer("b").last_target = 200
        out1 = device._compute_smooth_target([400, 0, 0], "a")
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 200)
        out2 = device._compute_smooth_target([400, 0, 0], "a")
        assert out2[0] < out1[0]

    def test_saturation_ema_smooths_out_when_recovering(self):
        device = CT002(
            active_control=True,
            fair_distribution=False,
            saturation_detection=True,
            saturation_alpha=0.5,
            min_target_for_saturation=10,
        )
        device._balancer._get_consumer("a").saturation_score = 1.0
        device._update_consumer_report("a", "A", 200)
        device._update_consumer_report("b", "A", 200)
        device._balancer._get_consumer("a").last_target = 200
        device._balancer._get_consumer("b").last_target = 200
        out1 = device._compute_smooth_target([400, 0, 0], "a")
        device._update_consumer_report("a", "A", 200)
        device._update_consumer_report("b", "A", 200)
        out2 = device._compute_smooth_target([400, 0, 0], "a")
        assert out2[0] > out1[0]

    def test_saturation_ignores_low_target(self):
        device = CT002(
            active_control=True,
            fair_distribution=False,
            saturation_detection=True,
            saturation_alpha=1.0,
            min_target_for_saturation=100,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._balancer._get_consumer("a").last_target = 10
        device._balancer._get_consumer("b").last_target = 10
        out = device._compute_smooth_target([20, 0, 0], "a")
        assert out[0] == 10

    def test_saturation_off_no_reduction(self):
        device = CT002(
            active_control=True,
            fair_distribution=False,
            saturation_detection=False,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 400)
        device._balancer._get_consumer("a").last_target = 200
        device._balancer._get_consumer("b").last_target = 200
        out_a = device._compute_smooth_target([400, 0, 0], "a")
        out_b = device._compute_smooth_target([400, 0, 0], "b")
        assert out_a[0] == out_b[0] == 200

    def test_saturation_opposite_sign_meaningful_output_not_saturated(self):
        """Meaningful output in the wrong direction can be normal ramp-down."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            saturation_detection=True,
            saturation_alpha=1.0,
            min_target_for_saturation=10,
        )
        device._update_consumer_report("a", "A", -100)
        device._update_consumer_report("b", "A", 200)
        device._balancer._get_consumer("a").last_target = 200
        device._balancer._get_consumer("b").last_target = 200
        out = device._compute_smooth_target([400, 0, 0], "a")
        # Consumer "a" is still producing meaningful power, so it should not
        # be flagged as saturated solely because it has not crossed zero yet.
        assert out[0] == 200
        assert device._balancer._get_consumer("a").saturation_score == 0.0

    def test_partial_output_not_saturated(self):
        """A battery producing meaningful output below target is NOT saturated.

        This is the key behavioral distinction: only near-zero output counts as
        saturation.  A battery lagging behind a moving target (e.g. due to load
        fluctuation) should not be penalised.
        """
        device = CT002(
            active_control=True,
            fair_distribution=False,
            saturation_detection=True,
            saturation_alpha=1.0,
            min_target_for_saturation=20,
        )
        device._update_consumer_report("a", "A", 50)
        device._update_consumer_report("b", "A", 150)
        device._balancer._get_consumer("a").last_target = 200
        device._balancer._get_consumer("b").last_target = 200
        device._compute_smooth_target([200, 0, 0], "a")
        # actual=50 is well above min_target_for_saturation=20, so no saturation.
        assert device._balancer._get_consumer("a").saturation_score == 0.0

    def test_saturation_boundary_at_threshold(self):
        """Output exactly at min_target_for_saturation is not saturated;
        output just below it is."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            saturation_detection=True,
            saturation_alpha=1.0,
            min_target_for_saturation=20,
        )
        # actual=20 (at threshold) → not saturated
        device._update_consumer_report("a", "A", 20)
        device._update_consumer_report("b", "A", 180)
        device._balancer._get_consumer("a").last_target = 200
        device._balancer._get_consumer("b").last_target = 200
        device._compute_smooth_target([200, 0, 0], "a")
        assert device._balancer._get_consumer("a").saturation_score == 0.0

        # actual=19 (just below threshold) → saturated
        device._update_consumer_report("a", "A", 19)
        device._balancer._get_consumer("a").last_target = 200
        device._compute_smooth_target([200, 0, 0], "a")
        assert device._balancer._get_consumer("a").saturation_score > 0.0


class TestCleanup:
    """Tests that saturation state is cleaned up with consumers."""

    def test_cleanup_removes_saturation_state(self):
        device = CT002(saturation_detection=True, consumer_ttl=0.01)
        device._update_consumer_report("a", "A", 0)
        device._balancer._get_consumer("a").last_target = 100
        device._balancer._get_consumer("a").saturation_score = 0.5
        time.sleep(0.02)
        device._cleanup_consumers()
        assert "a" not in device._balancer._consumers

    def test_cleanup_removes_efficiency_state(self):
        device = CT002(min_efficient_power=150, consumer_ttl=0.01)
        device._update_consumer_report("a", "A", 0)
        device._balancer._deprioritized.add("a")
        device._balancer._priority.append("a")
        device._balancer._get_consumer("a").fade_weight = 0.5
        time.sleep(0.02)
        device._cleanup_consumers()
        assert "a" not in device._balancer._deprioritized
        assert "a" not in device._balancer._priority
        assert "a" not in device._balancer._consumers


class TestEfficiencyOptimization:
    """Tests for efficiency optimization (low-demand power concentration)."""

    def test_disabled_by_default(self):
        """With min_efficient_power=0, output is identical to current behavior."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=0,
        )
        device._update_consumer_report("a", "A", 100)
        device._update_consumer_report("b", "A", 100)
        out = device._compute_smooth_target([400, 0, 0], "a")
        assert out[0] == 200

    def test_low_demand_concentrates_on_one_consumer(self):
        """200W with 2 consumers and threshold=150 → one gets ~200W, other ~0W."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        out_a = device._compute_smooth_target([200, 0, 0], "a")
        out_b = device._compute_smooth_target([200, 0, 0], "b")
        # One should get ~200W, the other ~0W
        assert (out_a[0] > 150 and out_b[0] < 10) or (out_b[0] > 150 and out_a[0] < 10)

    def test_high_demand_activates_all_consumers(self):
        """600W with 2 consumers and threshold=150 → both get ~300W."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        out_a = device._compute_smooth_target([600, 0, 0], "a")
        out_b = device._compute_smooth_target([600, 0, 0], "b")
        assert out_a[0] == 300
        assert out_b[0] == 300

    def test_hysteresis_prevents_oscillation(self):
        """At steady 250W with threshold=150, system should stay at 1 active
        (not oscillate between 1 and 2)."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # First call: enters limiting (250/2=125 < 150)
        device._compute_smooth_target([250, 0, 0], "a")
        device._compute_smooth_target([250, 0, 0], "b")
        assert len(device._balancer._deprioritized) == 1
        # Second call with same demand: should stay limiting (hysteresis)
        device._compute_smooth_target([251, 0, 0], "a")
        device._compute_smooth_target([251, 0, 0], "b")
        assert len(device._balancer._deprioritized) == 1

    def test_exits_limiting_at_higher_threshold(self):
        """Hysteresis requires higher per-consumer demand to exit limiting."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # Enter limiting
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        assert len(device._balancer._deprioritized) == 1
        # At 340W: per_consumer=170 < 180 (150*1.2), stays limiting
        device._compute_smooth_target([340, 0, 0], "a")
        device._compute_smooth_target([340, 0, 0], "b")
        assert len(device._balancer._deprioritized) == 1
        # At 370W: per_consumer=185 >= 180, exits limiting
        device._compute_smooth_target([370, 0, 0], "a")
        device._compute_smooth_target([370, 0, 0], "b")
        assert len(device._balancer._deprioritized) == 0

    def test_priority_rotation(self):
        """After rotation interval, the deprioritized consumer changes."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_rotation_interval=10,
            efficiency_fade_alpha=1.0,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        first_deprioritized = set(device._balancer._deprioritized)
        assert len(first_deprioritized) == 1
        # Simulate time passing beyond rotation interval.
        # Use the SAME sample to exercise the rotation-before-cache path
        # (the real bug was rotation not firing when the sample stayed the same).
        device._balancer._last_rotation -= 11
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        second_deprioritized = set(device._balancer._deprioritized)
        assert len(second_deprioritized) == 1
        assert first_deprioritized != second_deprioritized

    def test_single_consumer_always_active(self):
        """With only 1 consumer, it's always active regardless of threshold."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        device._update_consumer_report("a", "A", 0)
        out = device._compute_smooth_target([50, 0, 0], "a")
        assert out[0] == 50
        assert len(device._balancer._deprioritized) == 0

    def test_three_consumers_demand_supports_two(self):
        """350W with 3 consumers and threshold=150 → 2 active, 1 deprioritized."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._update_consumer_report("c", "A", 0)
        device._compute_smooth_target([350, 0, 0], "a")
        device._compute_smooth_target([350, 0, 0], "b")
        device._compute_smooth_target([350, 0, 0], "c")
        assert len(device._balancer._deprioritized) == 1

    def test_negative_target_concentrates(self):
        """Charging (negative target) should also concentrate on fewer batteries."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        out_a = device._compute_smooth_target([-200, 0, 0], "a")
        out_b = device._compute_smooth_target([-200, 0, 0], "b")
        # One should get ~-200W, the other ~0W
        total = abs(out_a[0]) + abs(out_b[0])
        assert total > 150
        assert min(abs(out_a[0]), abs(out_b[0])) < 10

    def test_cache_consistency_across_consumers(self):
        """Same sample should produce consistent deprioritized set."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        deprioritized_after_a = set(device._balancer._deprioritized)
        device._compute_smooth_target([200, 0, 0], "b")
        deprioritized_after_b = set(device._balancer._deprioritized)
        assert deprioritized_after_a == deprioritized_after_b

    def test_works_with_fair_distribution_off(self):
        """Efficiency optimization should work even with fair_distribution=False."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
        )
        # Report 0W power so estimated demand = battery(0) + grid(200) = 200W
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        out_a = device._compute_smooth_target([200, 0, 0], "a")
        out_b = device._compute_smooth_target([200, 0, 0], "b")
        assert (out_a[0] > 150 and out_b[0] < 10) or (out_b[0] > 150 and out_a[0] < 10)


class TestEfficiencyFade:
    """Tests for smooth fade transitions during efficiency optimization."""

    def test_fade_gradual_deprioritize(self):
        """With default alpha, deprioritized consumer should fade gradually."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # First call: deprioritization decided, but fade hasn't converged.
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        # The deprioritized consumer should NOT be at zero yet — it's fading.
        deprioritized_cid = next(iter(device._balancer._deprioritized))
        fade_w = device._balancer._consumers[deprioritized_cid].fade_weight
        assert 0 < fade_w < 1.0, f"Expected intermediate fade, got {fade_w}"

    def test_fade_blend_drives_consumer_down(self):
        """During fade-down, the blend formula should produce negative targets
        to actively drive the consumer toward zero, not just reduce its share."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
        )
        # Get consumer "b" fully deprioritized (instant with alpha=1.0).
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        assert len(device._balancer._deprioritized) == 1
        deprioritized_cid = next(iter(device._balancer._deprioritized))

        # Switch to gradual fade.  Exit limiting so fade weight rises.
        device._balancer._cfg = dataclasses.replace(
            device._balancer._cfg, efficiency_fade_alpha=0.3
        )
        device._balancer._cache_sample = None
        device._compute_smooth_target([600, 0, 0], deprioritized_cid)
        # 600W/2 = 300 > 180 (hysteresis exit): no longer limited.
        assert len(device._balancer._deprioritized) == 0
        fade_w = device._balancer._get_consumer(deprioritized_cid).fade_weight
        assert 0 < fade_w < 1.0, f"Should be mid-fade, got {fade_w}"

        # Now drop demand to re-enter limiting with consumer reporting 100W.
        device._update_consumer_report(deprioritized_cid, "A", 100)
        device._balancer._cache_sample = None
        out = device._compute_smooth_target([100, 0, 0], deprioritized_cid)
        # The blend: target = fade_w * normal + (1 - fade_w) * (-100)
        # With fade_w < 1 and reported=100, the drive-to-zero dominates.
        fade_w = {cid: s.fade_weight for cid, s in device._balancer._consumers.items()}
        assert out[0] < 0, (
            f"Expected negative target to drive consumer down during fade, "
            f"got {out[0]}. fade_w={fade_w}"
        )

    def test_fade_gradual_activate(self):
        """When demand rises, reactivated consumer fades in gradually."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # Fully deprioritize at low demand (instant with alpha=1.0).
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        assert len(device._balancer._deprioritized) == 1
        deprioritized_cid = next(iter(device._balancer._deprioritized))
        assert device._balancer._consumers[deprioritized_cid].fade_weight == 0.0

        # Now switch to gradual fade and raise demand above hysteresis exit.
        device._balancer._cfg = dataclasses.replace(
            device._balancer._cfg, efficiency_fade_alpha=0.3
        )
        device._balancer._cache_sample = None  # Force recompute
        device._compute_smooth_target([400, 0, 0], deprioritized_cid)
        # Demand 400W / 2 = 200W > 180W (150*1.2): exits limiting.
        # Fade weight should move toward 1.0 but not reach it yet.
        fade_w = device._balancer._consumers[deprioritized_cid].fade_weight
        assert 0 < fade_w < 1.0, f"Expected gradual activate, got {fade_w}"

    def test_fade_converges(self):
        """After enough calls, fade weight snaps to target."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # Run many cycles — use different sample_ids to ensure EMA advances.
        for i in range(20):
            device._compute_smooth_target([200 + i, 0, 0], "a")
            device._compute_smooth_target([200 + i, 0, 0], "b")
        deprioritized_cid = next(iter(device._balancer._deprioritized))
        assert device._balancer._consumers[deprioritized_cid].fade_weight == 0.0

    def test_fade_instant_with_alpha_one(self):
        """With alpha=1.0, fade is instant (matches old behavior)."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        out_a = device._compute_smooth_target([200, 0, 0], "a")
        out_b = device._compute_smooth_target([200, 0, 0], "b")
        # One should be at ~200W, the other at ~0W — same as old behavior.
        assert (out_a[0] > 150 and out_b[0] < 10) or (out_b[0] > 150 and out_a[0] < 10)

    def test_fade_rotation_during_fade(self):
        """Rotation fires even while a consumer is mid-fade."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_rotation_interval=10,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # Trigger deprioritization — fade is in progress.
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        first_deprioritized = set(device._balancer._deprioritized)
        # Simulate time passing beyond rotation interval.
        device._balancer._last_rotation -= 11
        device._balancer._cache_sample = None
        device._compute_smooth_target([201, 0, 0], "a")
        device._compute_smooth_target([201, 0, 0], "b")
        # Rotation should fire — fade handles overlapping transitions.
        assert device._balancer._deprioritized != first_deprioritized

    def test_fade_consumer_disconnect_mid_fade(self):
        """Consumer with active fade gets pruned by cleanup."""
        device = CT002(
            min_efficient_power=150,
            consumer_ttl=0.01,
        )
        device._update_consumer_report("a", "A", 0)
        device._balancer._get_consumer("a").fade_weight = 0.5
        time.sleep(0.02)
        device._cleanup_consumers()
        assert "a" not in device._balancer._consumers

    def test_fade_new_consumer_during_fade(self):
        """New consumer starts its fade from 1.0, not from 0.0."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # Trigger fade.
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        # New consumer appears — with high demand so it stays active.
        device._update_consumer_report("c", "A", 0)
        device._balancer._cache_sample = None  # Force recompute
        device._compute_smooth_target([600, 0, 0], "c")
        # 600W/3 = 200W > 180W (hysteresis exit): all consumers active.
        # New consumer "c" should be at 1.0 (never deprioritized).
        assert device._balancer._get_consumer("c").fade_weight == 1.0

    def test_fade_demand_reversal(self):
        """Deprioritization reverses mid-fade; EMA reverses direction."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # Start fading down at low demand.
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        deprioritized_cid = next(iter(device._balancer._deprioritized))
        fade_after_low = device._balancer._consumers[deprioritized_cid].fade_weight
        assert fade_after_low < 1.0

        # Now raise demand above hysteresis exit (per_consumer > 150*1.2=180).
        device._balancer._cache_sample = None
        device._compute_smooth_target([400, 0, 0], deprioritized_cid)
        fade_after_high = device._balancer._consumers[deprioritized_cid].fade_weight
        # Weight should have moved back toward 1.0.
        assert fade_after_high > fade_after_low


class TestEfficiencySaturationSwap:
    """Tests for saturation-aware forced rotation in efficiency optimization."""

    def test_efficiency_force_rotation_on_saturation(self):
        """Active consumer with saturation above threshold gets swapped out."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        first_deprioritized = set(device._balancer._deprioritized)
        assert len(first_deprioritized) == 1
        # The active consumer is whichever is NOT deprioritized
        active_cid = "a" if "b" in first_deprioritized else "b"
        # Inject high saturation on the active consumer
        device._balancer._get_consumer(active_cid).saturation_score = 0.5
        device._balancer._cache_sample = None
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        # Active consumer should now be deprioritized (swapped)
        assert active_cid in device._balancer._deprioritized

    def test_efficiency_no_force_rotation_below_threshold(self):
        """Saturation below threshold does not trigger a swap."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
            efficiency_rotation_interval=9999,
            saturation_alpha=0.15,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        first_deprioritized = set(device._balancer._deprioritized)
        active_cid = "a" if "b" in first_deprioritized else "b"
        # Inject low saturation that stays below threshold even after one
        # EMA update (0.15*1.0 + 0.85*0.1 = 0.235 < 0.4).
        device._balancer._get_consumer(active_cid).saturation_score = 0.1
        device._balancer._cache_sample = None
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        # No swap should have occurred
        assert device._balancer._deprioritized == first_deprioritized

    def test_efficiency_no_force_rotation_all_saturated(self):
        """When all consumers are saturated, no swap occurs (no healthy replacement)."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        first_deprioritized = set(device._balancer._deprioritized)
        # Inject high saturation on BOTH consumers
        device._balancer._get_consumer("a").saturation_score = 0.6
        device._balancer._get_consumer("b").saturation_score = 0.6
        device._balancer._cache_sample = None
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        # No swap — deprioritized set unchanged
        assert device._balancer._deprioritized == first_deprioritized

    def test_efficiency_force_rotation_resets_timer(self):
        """Forced swap resets the rotation timer."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
            efficiency_rotation_interval=900,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        # Use sentinel so we can detect that the timer was actually updated
        device._balancer._last_rotation = 0
        active_cid = "a" if "b" in device._balancer._deprioritized else "b"
        device._balancer._get_consumer(active_cid).saturation_score = 0.5
        device._balancer._cache_sample = None
        device._compute_smooth_target([200, 0, 0], "a")
        # Rotation timer should have been updated from sentinel
        assert device._balancer._last_rotation > 0

    def test_efficiency_force_rotation_disabled_when_zero(self):
        """Threshold=0.0 disables forced swap even with high saturation."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.0,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        first_deprioritized = set(device._balancer._deprioritized)
        active_cid = "a" if "b" in first_deprioritized else "b"
        device._balancer._get_consumer(active_cid).saturation_score = 0.9
        device._balancer._cache_sample = None
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        assert device._balancer._deprioritized == first_deprioritized

    def test_efficiency_saturation_decay(self):
        """Saturation decays multiplicatively when target < min_target."""
        device = CT002(
            active_control=True,
            saturation_detection=True,
            saturation_decay_factor=0.9,
            min_target_for_saturation=20,
        )
        device._balancer._get_consumer("a").saturation_score = 0.5
        # target (10) < min_target_for_saturation (20) → decay branch
        device._balancer._saturation.update(device._balancer._get_consumer("a"), 10, 10)
        expected = 0.5 * 0.9
        assert abs(device._balancer._consumers["a"].saturation_score - expected) < 1e-6

    def test_efficiency_saturation_decay_floor(self):
        """Saturation entry is removed when it decays below 0.001."""
        device = CT002(
            active_control=True,
            saturation_detection=True,
            saturation_decay_factor=0.5,
            min_target_for_saturation=20,
        )
        device._balancer._get_consumer("a").saturation_score = 0.001
        device._balancer._saturation.update(device._balancer._get_consumer("a"), 10, 10)
        # 0.001 * 0.5 = 0.0005 < 0.001 → entry should be removed
        assert device._balancer._get_consumer("a").saturation_score == 0.0

    def test_efficiency_force_swap_during_active_fade(self):
        """Forced swap during an active fade converges correctly."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_saturation_threshold=0.4,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # Trigger initial efficiency (starts fade)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        deprioritized_cid = next(iter(device._balancer._deprioritized))
        active_cid = "a" if deprioritized_cid == "b" else "b"
        # Verify fade is in progress (default alpha < 1.0)
        assert 0.0 < device._balancer._get_consumer(deprioritized_cid).fade_weight < 1.0
        # Inject saturation on active consumer to force swap
        device._balancer._get_consumer(active_cid).saturation_score = 0.5
        device._balancer._cache_sample = None
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        # After swap, the previously active consumer should now be deprioritized
        assert active_cid in device._balancer._deprioritized
        # Continue iterating — system should converge (no crash)
        for _ in range(20):
            device._balancer._cache_sample = None
            device._compute_smooth_target([200, 0, 0], "a")
            device._compute_smooth_target([200, 0, 0], "b")

    def test_efficiency_force_rotation_cache_invalidation(self):
        """After forced swap, next consumer call returns post-swap result."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        active_cid = "a" if "b" in device._balancer._deprioritized else "b"
        depr_cid = "b" if active_cid == "a" else "a"
        # Inject saturation on active consumer
        device._balancer._get_consumer(active_cid).saturation_score = 0.5
        device._balancer._cache_sample = None
        # First consumer call triggers swap
        device._compute_smooth_target([200, 0, 0], "a")
        # Second consumer call should see post-swap state (not stale cache)
        device._compute_smooth_target([200, 0, 0], "b")
        # The originally active consumer should be deprioritized
        assert active_cid in device._balancer._deprioritized
        assert depr_cid not in device._balancer._deprioritized

    def test_efficiency_force_rotation_three_consumers(self):
        """With 3 consumers and 2 active slots, only the saturated one swaps."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
            efficiency_rotation_interval=9999,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._update_consumer_report("c", "A", 0)
        # 350W / 3 = 116 < 150 → limiting, slots = int(350/150) = 2
        device._compute_smooth_target([350, 0, 0], "a")
        device._compute_smooth_target([350, 0, 0], "b")
        device._compute_smooth_target([350, 0, 0], "c")
        assert len(device._balancer._deprioritized) == 1
        depr_cid = next(iter(device._balancer._deprioritized))
        active_cids = [c for c in ["a", "b", "c"] if c != depr_cid]
        # Saturate only one of the two active consumers
        sat_cid = active_cids[0]
        device._balancer._get_consumer(sat_cid).saturation_score = 0.5
        device._balancer._cache_sample = None
        device._compute_smooth_target([350, 0, 0], "a")
        device._compute_smooth_target([350, 0, 0], "b")
        device._compute_smooth_target([350, 0, 0], "c")
        # The saturated active should be swapped with the deprioritized one
        assert sat_cid in device._balancer._deprioritized
        assert depr_cid not in device._balancer._deprioritized
        # Still exactly 1 deprioritized
        assert len(device._balancer._deprioritized) == 1

    def test_efficiency_activation_resets_stale_saturation(self):
        """Activation clears residual saturation when a consumer becomes active."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
            efficiency_rotation_interval=10,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        depr_cid = next(iter(device._balancer._deprioritized))
        # Give the deprioritized consumer a residual saturation score
        device._balancer._get_consumer(depr_cid).saturation_score = 0.2
        # Trigger timed rotation to activate the deprioritized consumer
        device._balancer._last_rotation -= 11
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        # The previously deprioritized consumer should now be active
        assert depr_cid not in device._balancer._deprioritized
        # Its saturation should have been reset on activation
        assert device._balancer._get_consumer(depr_cid).saturation_score == 0.0

    def test_efficiency_rampdown_does_not_poison_replacement_battery(self):
        """A healthy battery ramping down must remain eligible for takeover."""
        device = CT002(
            active_control=True,
            fair_distribution=True,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
        )
        # a is the saturated active battery, b is the healthy replacement that
        # is currently ramping down after being deprioritized.
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 200)
        device._balancer._priority = ["a", "b"]
        device._balancer._deprioritized = {"b"}
        device._balancer._get_consumer("a").last_target = 200
        device._balancer._get_consumer("b").last_target = -80
        device._balancer._get_consumer("a").saturation_score = 0.5

        device._compute_smooth_target([200, 0, 0], "b")

        assert device._balancer._get_consumer("b").saturation_score == 0.0

    def test_efficiency_force_rotation_on_saturation_charging(self):
        """Forced swap also works for charging (negative target / solar excess)."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # Negative values = solar excess / charging
        device._compute_smooth_target([-200, 0, 0], "a")
        device._compute_smooth_target([-200, 0, 0], "b")
        assert len(device._balancer._deprioritized) == 1
        active_cid = "a" if "b" in device._balancer._deprioritized else "b"
        # Inject high saturation on the active consumer (can't charge)
        device._balancer._get_consumer(active_cid).saturation_score = 0.5
        device._balancer._cache_sample = None
        device._compute_smooth_target([-200, 0, 0], "a")
        device._compute_smooth_target([-200, 0, 0], "b")
        # Active consumer should now be deprioritized (swapped)
        assert active_cid in device._balancer._deprioritized

    def test_rotation_grace_period_prevents_immediate_swap_back(self):
        """After timed rotation promotes a consumer, saturation updates are
        skipped during the grace period so the battery has time to ramp up."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=1.0,
            efficiency_saturation_threshold=0.4,
            efficiency_rotation_interval=1800,
            saturation_alpha=0.15,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        first_deprioritized = set(device._balancer._deprioritized)
        assert len(first_deprioritized) == 1
        first_active = "a" if "b" in first_deprioritized else "b"
        first_depr = next(iter(first_deprioritized))

        # Simulate the active consumer becoming saturated and being swapped
        device._balancer._get_consumer(first_active).saturation_score = 0.5
        device._balancer._cache_sample = None
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        # The swap should have happened: originally-active is now deprioritized
        assert first_active in device._balancer._deprioritized
        # The promoted consumer should have a grace period set
        assert device._balancer._get_consumer(first_depr).saturation_grace_until > 0

        # Now simulate rapid polling where the promoted consumer reports
        # zero output (battery ramping up).  The last target was set to a
        # real value, so without the grace period, saturation would climb
        # to >=0.4 in about 4 polls and trigger an immediate swap-back.
        device._balancer._get_consumer(first_depr).last_target = 200
        for _ in range(10):
            device._balancer._saturation.update(
                device._balancer._get_consumer(first_depr), 200, 0
            )
            device._balancer._cache_sample = None
            device._compute_smooth_target([200, 0, 0], "a")
            device._compute_smooth_target([200, 0, 0], "b")

        # The promoted consumer should STILL be active (grace period protects it)
        assert first_depr not in device._balancer._deprioritized
        # Its saturation should be zero (updates skipped during grace)
        assert device._balancer._get_consumer(first_depr).saturation_score == 0.0

    def test_rotation_grace_period_expires(self):
        """Probe timeout restores the previous active battery."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_fade_alpha=0.15,
            efficiency_saturation_threshold=0.4,
            efficiency_rotation_interval=10,
            saturation_alpha=0.15,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        first_depr = next(iter(device._balancer._deprioritized))
        first_active = "a" if first_depr == "b" else "b"

        # Trigger timed rotation to promote the deprioritized consumer
        device._balancer._last_rotation -= 11
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        assert first_depr not in device._balancer._deprioritized
        assert device._balancer._probe_state is not None

        # Expire the probe window and keep the promoted battery at 0W.
        device._balancer._probe_state.deadline = time.time() - 1

        device._balancer._cache_sample = None
        out_a = device._compute_smooth_target([200, 0, 0], "a")
        out_b = device._compute_smooth_target([200, 0, 0], "b")
        assert first_depr in device._balancer._deprioritized
        assert first_active not in device._balancer._deprioritized
        assert device._balancer._probe_state is None
        assert device._balancer._get_consumer(first_depr).fade_weight == 0.0
        assert device._balancer._get_consumer(first_active).fade_weight == 1.0
        rejected_out = out_a if first_depr == "a" else out_b
        restored_out = out_b if first_active == "b" else out_a
        assert rejected_out[0] == 0.0
        assert restored_out[0] > 0.0

    def test_probe_backup_uses_delta_not_absolute_output(self):
        """Initial probe command should ramp up instead of jumping to absolute output."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            smooth_target_alpha=1.0,
            min_efficient_power=150,
            probe_min_power=80,
            efficiency_fade_alpha=1.0,
            efficiency_rotation_interval=10,
            deadband=0,
        )
        device._update_consumer_report("a", "A", 200)
        device._update_consumer_report("b", "A", 0)
        device._balancer._priority = ["a", "b"]
        device._balancer._deprioritized = {"b"}
        device._balancer._last_rotation -= 11

        out_a = device._compute_smooth_target([0, 0, 0], "a")
        out_b = device._compute_smooth_target([0, 0, 0], "b")

        assert device._balancer._probe_state is not None
        assert out_a[0] == 0
        assert out_b[0] == 5

    def test_probe_backup_ignores_probe_output_and_follows_demand(self):
        """Backup should keep following live demand during probe."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            smooth_target_alpha=1.0,
            min_efficient_power=150,
            probe_min_power=80,
            efficiency_fade_alpha=1.0,
            efficiency_rotation_interval=10,
            deadband=0,
        )
        device._update_consumer_report("a", "A", 200)
        device._update_consumer_report("b", "A", 40)
        device._balancer._priority = ["a", "b"]
        device._balancer._deprioritized = {"b"}
        device._balancer._last_rotation -= 11

        out_a = device._compute_smooth_target([-40, 0, 0], "a")
        out_b = device._compute_smooth_target([-40, 0, 0], "b")

        assert device._balancer._probe_state is not None
        assert out_a[0] == 0
        assert out_b[0] == 0

    def test_probe_backup_backs_off_after_first_qualifying_sample(self):
        """Once the probe has one qualifying sample, backup should subtract actual probe output."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            probe_min_power=80,
        )
        device._balancer._probe_state = ProbeState(
            candidate_id="b",
            active_ids=("b",),
            backup_ids=("a",),
            restore_active_ids=("a",),
            deadline=time.time() + 10,
            started_at=time.time(),
            proof_samples=1,
        )
        reports = {
            "a": {"phase": "A", "power": 200},
            "b": {"phase": "A", "power": 80},
        }
        out_a = device._balancer._compute_probe_target("a", reports, -80, {})

        assert out_a is not None
        assert out_a[0] == -80


class TestInactiveConsumers:
    """Tests for the active/pause flag (set_consumer_active)."""

    def test_inactive_consumer_drives_to_zero_on_phase(self):
        """An inactive consumer with non-zero reported power should get
        target = -reported_power on its phase, not just [0,0,0]."""
        device = CT002(active_control=True, fair_distribution=False)
        device._update_consumer_report("bat1", "B", 250)
        device.set_consumer_active("bat1", False)

        result = device._compute_smooth_target([400, 0, 0], "bat1")

        # Phase B → index 1, target should be -250 to steer output to zero
        assert result == [0.0, -250.0, 0.0]

    def test_inactive_consumer_already_at_zero_returns_zeros(self):
        """An inactive consumer reporting 0W should get [0,0,0]."""
        device = CT002(active_control=True, fair_distribution=False)
        device._update_consumer_report("bat1", "A", 0)
        device.set_consumer_active("bat1", False)

        result = device._compute_smooth_target([400, 0, 0], "bat1")
        assert result == [0, 0, 0]

    def test_inactive_consumer_excluded_from_fair_distribution(self):
        """Fair share should only count active consumers."""
        device = CT002(active_control=True, fair_distribution=False)
        device._update_consumer_report("bat1", "A", 0)
        device._update_consumer_report("bat2", "A", 0)
        device.set_consumer_active("bat1", False)

        # Only bat2 is active → gets the full target, not half
        result = device._compute_smooth_target([400, 0, 0], "bat2")
        assert result[0] == 400

    def test_inactive_consumer_excluded_from_efficiency_rotation(self):
        """Inactive consumers should not appear in the efficiency priority list."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=200,
        )
        device._update_consumer_report("bat1", "A", 50)
        device._update_consumer_report("bat2", "A", 50)
        device._update_consumer_report("bat3", "A", 50)
        device.set_consumer_active("bat2", False)

        device._compute_smooth_target([100, 0, 0], "bat1")

        assert "bat2" not in device._balancer._priority
        assert "bat1" in device._balancer._priority
        assert "bat3" in device._balancer._priority

    def test_reactivated_consumer_rejoins_distribution(self):
        """After re-activating, consumer should participate normally."""
        device = CT002(active_control=True, fair_distribution=False)
        device._update_consumer_report("bat1", "A", 0)
        device._update_consumer_report("bat2", "A", 0)

        # Pause bat1
        device.set_consumer_active("bat1", False)
        result = device._compute_smooth_target([400, 0, 0], "bat2")
        assert result[0] == 400  # bat2 gets full share

        # Reactivate bat1
        device.set_consumer_active("bat1", True)
        device._smoother._last_sample = None  # force re-evaluation
        result = device._compute_smooth_target([400, 0, 0], "bat1")
        assert result[0] == 200  # now split between two

    def test_set_consumer_active_toggle(self):
        device = CT002()
        assert device.is_consumer_active("x")
        device.set_consumer_active("x", False)
        assert not device.is_consumer_active("x")
        device.set_consumer_active("x", True)
        assert device.is_consumer_active("x")

    def test_reactivation_clears_stale_state(self):
        """Re-enabling a consumer should clear saturation and last_target."""
        device = CT002(active_control=True)
        device._balancer._get_consumer("bat1").saturation_score = 0.8
        device._balancer._get_consumer("bat1").last_target = 50
        device.set_consumer_active("bat1", False)
        # Stale state is preserved while inactive
        assert device._balancer._get_consumer("bat1").saturation_score > 0.0
        # Reactivation clears it
        device.set_consumer_active("bat1", True)
        assert device._balancer._get_consumer("bat1").saturation_score == 0.0
        assert device._balancer._get_consumer("bat1").last_target is None

    def test_last_target_set_to_zero_for_inactive(self):
        """Inactive consumer's last_target should be recorded as 0."""
        device = CT002(active_control=True)
        device._update_consumer_report("bat1", "A", 100)
        device.set_consumer_active("bat1", False)
        device._compute_smooth_target([400, 0, 0], "bat1")
        assert device._balancer._get_consumer("bat1").last_target == 0
