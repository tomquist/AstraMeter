"""Tests for CT002 active control, fair distribution, and saturation detection."""

import time

from b2500_meter.ct002.ct002 import CT002


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
        assert device._smoothed_target == 500

        # Feed readings within deadband (grid balanced).
        # Each call uses a unique value so the sample-dedup sees a fresh reading.
        for i in range(20):
            device._compute_smooth_target([i, 0, 0], "a")

        # Smoothed should have decayed significantly toward zero
        assert device._smoothed_target < 10

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
        assert device._smoothed_target >= 0

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
        assert device._smoothed_target == 400

        # Two consumers call with the same new reading
        device._compute_smooth_target([100, 0, 0], "a")
        after_first = device._smoothed_target
        device._compute_smooth_target([100, 0, 0], "b")
        after_second = device._smoothed_target

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
        device._last_target_by_consumer["a"] = 200
        device._last_target_by_consumer["b"] = 200
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
        device._last_target_by_consumer["a"] = 200
        device._last_target_by_consumer["b"] = 200
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
        device._saturation_by_consumer["a"] = 1.0
        device._update_consumer_report("a", "A", 200)
        device._update_consumer_report("b", "A", 200)
        device._last_target_by_consumer["a"] = 200
        device._last_target_by_consumer["b"] = 200
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
        device._last_target_by_consumer["a"] = 10
        device._last_target_by_consumer["b"] = 10
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
        device._last_target_by_consumer["a"] = 200
        device._last_target_by_consumer["b"] = 200
        out_a = device._compute_smooth_target([400, 0, 0], "a")
        out_b = device._compute_smooth_target([400, 0, 0], "b")
        assert out_a[0] == out_b[0] == 200

    def test_saturation_opposite_sign_increases_saturation(self):
        """When target and actual have opposite signs (e.g. DC-only battery
        ignoring a charge command), saturation should increase."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            saturation_detection=True,
            saturation_alpha=1.0,
            min_target_for_saturation=10,
        )
        device._update_consumer_report("a", "A", -100)
        device._update_consumer_report("b", "A", 200)
        device._last_target_by_consumer["a"] = 200
        device._last_target_by_consumer["b"] = 200
        out = device._compute_smooth_target([400, 0, 0], "a")
        # Consumer "a" has opposite sign (actual=-100, target=200), so it
        # should be detected as saturated and get a reduced share.
        assert out[0] < 200
        assert device._saturation_by_consumer.get("a", 0) > 0


class TestCleanup:
    """Tests that saturation state is cleaned up with consumers."""

    def test_cleanup_removes_saturation_state(self):
        device = CT002(saturation_detection=True, consumer_ttl=0.01)
        device._update_consumer_report("a", "A", 0)
        device._last_target_by_consumer["a"] = 100
        device._saturation_by_consumer["a"] = 0.5
        time.sleep(0.02)
        device._cleanup_consumers()
        assert "a" not in device._saturation_by_consumer
        assert "a" not in device._last_target_by_consumer

    def test_cleanup_removes_efficiency_state(self):
        device = CT002(min_efficient_power=150, consumer_ttl=0.01)
        device._update_consumer_report("a", "A", 0)
        device._efficiency_deprioritized.add("a")
        device._efficiency_priority.append("a")
        time.sleep(0.02)
        device._cleanup_consumers()
        assert "a" not in device._efficiency_deprioritized
        assert "a" not in device._efficiency_priority


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
        assert len(device._efficiency_deprioritized) == 1
        # Second call with same demand: should stay limiting (hysteresis)
        device._compute_smooth_target([251, 0, 0], "a")
        device._compute_smooth_target([251, 0, 0], "b")
        assert len(device._efficiency_deprioritized) == 1

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
        assert len(device._efficiency_deprioritized) == 1
        # At 340W: per_consumer=170 < 180 (150*1.2), stays limiting
        device._compute_smooth_target([340, 0, 0], "a")
        device._compute_smooth_target([340, 0, 0], "b")
        assert len(device._efficiency_deprioritized) == 1
        # At 370W: per_consumer=185 >= 180, exits limiting
        device._compute_smooth_target([370, 0, 0], "a")
        device._compute_smooth_target([370, 0, 0], "b")
        assert len(device._efficiency_deprioritized) == 0

    def test_priority_rotation(self):
        """After rotation interval, the deprioritized consumer changes."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
            efficiency_rotation_interval=10,
        )
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        first_deprioritized = set(device._efficiency_deprioritized)
        assert len(first_deprioritized) == 1
        # Simulate time passing beyond rotation interval.
        # Use the SAME sample to exercise the rotation-before-cache path
        # (the real bug was rotation not firing when the sample stayed the same).
        device._efficiency_last_rotation -= 11
        device._compute_smooth_target([200, 0, 0], "a")
        device._compute_smooth_target([200, 0, 0], "b")
        second_deprioritized = set(device._efficiency_deprioritized)
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
        assert len(device._efficiency_deprioritized) == 0

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
        assert len(device._efficiency_deprioritized) == 1

    def test_negative_target_concentrates(self):
        """Charging (negative target) should also concentrate on fewer batteries."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
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
        deprioritized_after_a = set(device._efficiency_deprioritized)
        device._compute_smooth_target([200, 0, 0], "b")
        deprioritized_after_b = set(device._efficiency_deprioritized)
        assert deprioritized_after_a == deprioritized_after_b

    def test_works_with_fair_distribution_off(self):
        """Efficiency optimization should work even with fair_distribution=False."""
        device = CT002(
            active_control=True,
            fair_distribution=False,
            min_efficient_power=150,
        )
        # Report 0W power so estimated demand = battery(0) + grid(200) = 200W
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        out_a = device._compute_smooth_target([200, 0, 0], "a")
        out_b = device._compute_smooth_target([200, 0, 0], "b")
        assert (out_a[0] > 150 and out_b[0] < 10) or (out_b[0] > 150 and out_a[0] < 10)
