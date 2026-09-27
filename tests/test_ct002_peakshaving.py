"""Tests for CT002 peak shaving: capping the household demand handed to the
load balancer at a configurable threshold, based on reconstructed total
household demand (grid reading + active battery contribution) rather than
the raw grid reading alone.
"""

from astrameter.ct002.balancer import split_balancer_knobs
from astrameter.ct002.ct002 import CT002


def _ct002(**kwargs) -> CT002:
    kwargs.setdefault("pace_base_step", 0)
    balancer, other = split_balancer_knobs(kwargs)
    return CT002(balancer=balancer, **other)


class TestPeakshavingThreshold:
    def test_disabled_by_default_passes_through_unchanged(self):
        device = _ct002(active_control=True, fair_distribution=False)
        device._update_consumer_report("a", "A", 0)
        out = device._compute_smooth_target([2800, 0, 0], "a")
        assert out[0] == 2800

    def test_zero_threshold_passes_through_unchanged(self):
        device = _ct002(
            active_control=True, fair_distribution=False, peakshaving_threshold=0.0
        )
        device._update_consumer_report("a", "A", 0)
        out = device._compute_smooth_target([2800, 0, 0], "a")
        assert out[0] == 2800

    def test_demand_below_threshold_with_no_battery_output_shaves_to_zero(self):
        """No battery running yet, demand under threshold: nothing to do."""
        device = _ct002(
            active_control=True,
            fair_distribution=False,
            peakshaving_threshold=500.0,
        )
        device._update_consumer_report("a", "A", 0)
        out = device._compute_smooth_target([300, 0, 0], "a")
        assert out[0] == 0

    def test_demand_above_threshold_shaves_to_excess(self):
        """No battery running yet: excess above threshold should be requested."""
        device = _ct002(
            active_control=True,
            fair_distribution=False,
            peakshaving_threshold=500.0,
        )
        device._update_consumer_report("a", "A", 0)
        out = device._compute_smooth_target([800, 0, 0], "a")
        assert out[0] == 300  # 800 - 500

    def test_battery_already_discharging_below_threshold_gets_restoring_force(self):
        """
        Regression test for the "dead zone" bug: household demand is under
        the threshold, but a battery is already discharging (contributing
        180W). The raw grid reading alone (300 - 180 = 120W) looks like it's
        already under threshold, which would report 0 and freeze the battery
        at 180W forever. Reconstructing true demand (120 + 180 = 300W, still
        under the 500W threshold) must instead produce a negative signal
        equal to -180W, giving the balancer a reason to wind the battery
        back down toward zero.
        """
        device = _ct002(
            active_control=True,
            fair_distribution=False,
            peakshaving_threshold=500.0,
        )
        # Battery "a" is already contributing 180W.
        device._update_consumer_report("a", "A", 180)
        # Raw grid reading is 120W (300W real house load - 180W battery help).
        out = device._compute_smooth_target([120, 0, 0], "a")
        assert out[0] == -180

    def test_battery_overcharging_below_threshold_gets_restoring_force(self):
        """
        Mirror scenario: a battery is slightly overcharging (negative power,
        i.e. importing 64W to charge) while true household demand (300W)
        is still comfortably under the threshold. The raw grid reading
        (300 + 64 = 364W, since the charging draws extra from the grid)
        must not be silently accepted; the reconstructed demand (300W) is
        still under threshold, so the signal should push the battery back
        toward zero (a positive signal of +64W, i.e. "reduce your charging").
        """
        device = _ct002(
            active_control=True,
            fair_distribution=False,
            peakshaving_threshold=500.0,
        )
        # Battery "a" is charging with 64W (negative = consuming to charge).
        device._update_consumer_report("a", "A", -64)
        # Raw grid reading is 364W (300W real house load + 64W charging draw).
        out = device._compute_smooth_target([364, 0, 0], "a")
        assert out[0] == 64

    def test_battery_discharging_above_threshold_converges_correctly(self):
        """
        Household demand is above the threshold and a battery is already
        discharging to help cover it. The reported signal should reflect
        the remaining gap to the threshold-adjusted target, not simply the
        raw excess.
        """
        device = _ct002(
            active_control=True,
            fair_distribution=False,
            peakshaving_threshold=500.0,
        )
        # Battery "a" already discharging 300W.
        device._update_consumer_report("a", "A", 300)
        # Raw grid reading is 500W (800W true demand - 300W battery help).
        out = device._compute_smooth_target([500, 0, 0], "a")
        # true demand = 500 + 300 = 800; shaved target = min(800, 500) = 500
        # reported = raw_grid(500) - shaved_target(500) = 0
        # The battery is already exactly at the correct equilibrium (500W
        # true demand above threshold worth of discharge = 300W... wait:
        # 800 - 500 = 300W needed, battery already provides 300W, so the
        # system has nothing left to correct: signal is 0.
        assert out[0] == 0

    def test_export_surplus_below_zero_passes_through_unaffected(self):
        """Net export (PV surplus) must be untouched so charging still works."""
        device = _ct002(
            active_control=True,
            fair_distribution=False,
            peakshaving_threshold=500.0,
        )
        device._update_consumer_report("a", "A", 0)
        out = device._compute_smooth_target([-1500, 0, 0], "a")
        assert out[0] == -1500

    def test_multi_phase_total_used_for_shaving_decision(self):
        """Peakshaving operates on the summed total across phases."""
        device = _ct002(
            active_control=True,
            fair_distribution=False,
            peakshaving_threshold=500.0,
        )
        device._update_consumer_report("a", "A", 0)
        # Total = 300 + 100 + 100 = 500, exactly at threshold -> shaved to 0.
        out = device._compute_smooth_target([300, 100, 100], "a")
        assert sum(out) == 0


class TestLivePeakshavingThreshold:
    def test_set_peakshaving_threshold_updates_value(self):
        device = _ct002(active_control=True, fair_distribution=False)
        assert device.peakshaving_threshold == 0.0
        device.set_peakshaving_threshold(2000.0)
        assert device.peakshaving_threshold == 2000.0

    def test_set_peakshaving_threshold_takes_effect_immediately(self):
        """A live threshold change must affect the very next control cycle,
        without requiring a restart. Tested directly against
        _apply_peakshaving(), which is the exact, isolated calculation our
        live-update wires into — proven correct here without also exercising
        the balancer's own unrelated rate-limiting/smoothing machinery
        (predictive filter, oscillation damping, step limiting, efficiency
        EMA), which intentionally blends successive outputs and would
        otherwise make this test about that machinery instead of ours."""
        device = _ct002(active_control=True, fair_distribution=False)

        # Threshold disabled: full demand passes through unchanged.
        assert device._apply_peakshaving(2800.0) == 2800.0

        # Live-update the threshold.
        device.set_peakshaving_threshold(2000.0)

        # The very next call already reflects the new threshold.
        assert device._apply_peakshaving(2800.0) == 800.0

    def test_set_peakshaving_threshold_to_zero_disables_it(self):
        device = _ct002(
            active_control=True,
            fair_distribution=False,
            peakshaving_threshold=2000.0,
        )

        assert device._apply_peakshaving(2800.0) == 800.0

        device.set_peakshaving_threshold(0.0)

        assert device._apply_peakshaving(2800.0) == 2800.0


class TestOnlySteeredBatteriesCountTowardDemand:
    """A battery the balancer does not steer is not ours to move: its output
    lowers household demand the way solar does.  Counting it would ask the
    steered batteries to cancel it out by charging from the grid."""

    def _device(self, **kwargs) -> CT002:
        kwargs.setdefault("active_control", True)
        kwargs.setdefault("fair_distribution", False)
        kwargs.setdefault("peakshaving_threshold", 500.0)
        return _ct002(**kwargs)

    def test_a_manual_battery_does_not_make_the_others_charge(self):
        device = self._device()
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("m", "A", 300)
        device.set_consumer_auto_target("m", False)
        device.set_consumer_manual_target("m", 300)
        # House 400 W, the manual battery covers 300 of it: the grid reads
        # 100 W, well under the threshold, so the auto battery stays at 0.
        assert device._apply_peakshaving(100.0) == 0.0
        out = device._compute_smooth_target([100, 0, 0], "a")
        assert out[0] == 0

    def test_a_battery_opted_out_of_control_is_left_alone(self):
        device = self._device()
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("x", "A", 300, participates=False)
        assert device._apply_peakshaving(100.0) == 0.0

    def test_a_paused_battery_is_left_alone(self):
        device = self._device()
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("p", "A", 300)
        device.set_consumer_active("p", False)
        assert device._apply_peakshaving(100.0) == 0.0

    def test_a_battery_gone_silent_no_longer_counts(self):
        now = [1000.0]
        device = self._device(clock=lambda: now[0], consumer_ttl=5)
        device._update_consumer_report("gone", "A", 300)
        now[0] += 10
        device._update_consumer_report("a", "A", 0)
        assert device._apply_peakshaving(100.0) == 0.0

    def test_above_the_threshold_only_the_excess_is_asked_of_steered_ones(self):
        device = self._device()
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("m", "A", 300)
        device.set_consumer_auto_target("m", False)
        device.set_consumer_manual_target("m", 300)
        # House 1100 W, the manual battery covers 300: the grid reads 800 W
        # and the steered battery is asked for the 300 W above the threshold.
        assert device._apply_peakshaving(800.0) == 300.0


class TestThresholdValidation:
    def test_invalid_values_are_ignored(self):
        device = _ct002(peakshaving_threshold=1000.0)
        for bad in (-1.0, float("nan"), float("inf")):
            device.set_peakshaving_threshold(bad)
            assert device.peakshaving_threshold == 1000.0

    def test_an_invalid_configured_value_leaves_it_off(self):
        assert _ct002(peakshaving_threshold=float("nan")).peakshaving_threshold == 0.0
        assert _ct002(peakshaving_threshold=-5.0).peakshaving_threshold == 0.0


class TestIdleBelowThreshold:
    """Once demand is under the threshold and the pool has wound down to about
    net zero, every steered battery is held at 0 W on its own.

    Sharing the correction instead left a pair trading power inside their
    deadbands, one charging from the other, for as long as demand stayed under
    the threshold.
    """

    def _device(self) -> CT002:
        return _ct002(
            active_control=True,
            fair_distribution=True,
            peakshaving_threshold=2500.0,
        )

    def _trading_pair(self) -> CT002:
        device = self._device()
        device._update_consumer_report("charging", "A", -30)
        device._update_consumer_report("discharging", "A", 20)
        return device

    def test_a_pair_trading_power_is_idled_battery_by_battery(self):
        device = self._trading_pair()
        # House 400 W: the grid carries it plus the pair's net 10 W charge.
        assert device._peakshaving(410.0) == (-(-30 + 20), True)
        assert device._compute_smooth_target([410, 0, 0], "charging")[0] == 30
        assert device._compute_smooth_target([410, 0, 0], "discharging")[0] == -20

    def test_a_real_wind_down_stays_on_the_shared_correction(self):
        """A battery still delivering hundreds of watts is wound down by the
        latency-compensated shared correction, not dropped: with a stale meter
        the fresh, falling battery output makes demand look under the threshold
        for a few polls when it is not."""
        device = self._device()
        device._update_consumer_report("idle", "A", 0)
        device._update_consumer_report("busy", "A", 300)
        shaved, hold = device._peakshaving(400.0)
        assert shaved == -300.0
        assert hold is False

    def test_a_residual_in_one_direction_stays_on_the_shared_correction(self):
        """Two units each charging a few watts are not trading power, and each
        one's own sub-deadband correction would be ignored by its firmware;
        the shared correction concentrates it on one battery instead."""
        device = self._device()
        device._update_consumer_report("a", "A", -18)
        device._update_consumer_report("b", "A", -15)
        assert device._peakshaving(433.0) == (33.0, False)

    def test_above_the_threshold_the_pool_shares_the_excess(self):
        device = self._trading_pair()
        assert device._peakshaving(3500.0)[1] is False
        out_a = device._compute_smooth_target([3500, 0, 0], "charging")
        out_b = device._compute_smooth_target([3500, 0, 0], "discharging")
        assert out_a[0] > 0 and out_b[0] > 0

    def test_a_surplus_still_charges_through_the_pool(self):
        device = self._trading_pair()
        assert device._peakshaving(-800.0) == (-800.0, False)
        assert device._compute_smooth_target([-800, 0, 0], "charging")[0] < 0
