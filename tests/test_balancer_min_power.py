"""MIN_CHARGE_POWER / MIN_DISCHARGE_POWER: no battery runs at a few watts (#721).

An inverter is least efficient at a few watts; a Venus E covering a 19 W
night standby runs at about 68 %.  Below its minimum a battery idles at 0 W and
the other batteries, or the grid, take its share.  It resumes once its share
reaches EFFICIENCY_HYSTERESIS_FACTOR times the minimum, so a demand hovering at
the minimum does not toggle it.
"""

from __future__ import annotations

import pytest

from astrameter.ct002.balancer import (
    EFFICIENCY_HYSTERESIS_FACTOR,
    BalancerConfig,
    ConsumerReport,
    split_balancer_knobs,
)
from astrameter.ct002.controls import BATTERY_POWER_MAX, CONSUMER_CONTROLS_BY_FIELD
from astrameter.ct002.ct002 import CT002


def _ct002(**kwargs) -> CT002:
    # Raw, unpaced, unpredicted commands, so each reading is the share itself.
    kwargs.setdefault("pace_base_step", 0)
    kwargs.setdefault("grid_predict_trust", 0.0)
    kwargs.setdefault("import_trim_w", 0.0)
    kwargs.setdefault("fair_distribution", False)
    kwargs.setdefault("active_control", True)
    balancer, other = split_balancer_knobs(kwargs)
    return CT002(balancer=balancer, **other)


def _send(device: CT002, grid: float, consumer: str = "a") -> float:
    """The total reading *consumer* is sent for a grid reading on phase A."""
    return sum(device._compute_smooth_target([grid, 0, 0], consumer))


def _idle(device: CT002, consumer: str) -> bool:
    snapshot = device._balancer.snapshot_consumer(consumer)
    assert snapshot is not None
    return snapshot.min_power_idle


class TestSingleBattery:
    def test_standby_below_the_minimum_comes_from_the_grid(self):
        device = _ct002(min_discharge_power=50.0)
        device._update_consumer_report("a", "A", 0)
        assert _send(device, 19) == 0
        assert _idle(device, "a")

    def test_without_a_minimum_the_battery_covers_the_standby(self):
        device = _ct002()
        device._update_consumer_report("a", "A", 0)
        assert _send(device, 19) == 19
        assert not _idle(device, "a")

    def test_demand_above_the_minimum_is_covered_in_full(self):
        device = _ct002(min_discharge_power=50.0)
        device._update_consumer_report("a", "A", 0)
        assert _send(device, 300) == 300

    def test_an_idle_battery_resumes_only_past_the_hysteresis(self):
        device = _ct002(min_discharge_power=50.0)
        device._update_consumer_report("a", "A", 0)
        assert _send(device, 19) == 0
        resume = 50.0 * EFFICIENCY_HYSTERESIS_FACTOR
        # Between the minimum and the resume point it stays idle ...
        assert _send(device, resume - 1) == 0
        # ... and past it, covers everything.
        assert _send(device, resume + 1) == resume + 1
        assert not _idle(device, "a")

    def test_a_running_battery_keeps_running_down_to_the_minimum(self):
        device = _ct002(min_discharge_power=50.0)
        device._update_consumer_report("a", "A", 100)
        # Demand 55 W: the battery delivers 100 W, the grid exports 45 W.
        assert _send(device, -45) == -45
        assert not _idle(device, "a")

    def test_a_running_battery_winds_down_when_demand_drops_below(self):
        device = _ct002(min_discharge_power=50.0)
        device._update_consumer_report("a", "A", 100)
        # Demand 40 W: the battery is told to go from 100 W to 0 W.
        assert _send(device, -60) == -100
        assert _idle(device, "a")

    def test_charging_has_its_own_minimum(self):
        device = _ct002(min_charge_power=100.0, min_discharge_power=0.0)
        device._update_consumer_report("a", "A", 0)
        assert _send(device, -60) == 0
        assert _send(device, -150) == -150
        # Discharging is unaffected by the charge minimum.
        device2 = _ct002(min_charge_power=100.0)
        device2._update_consumer_report("a", "A", 0)
        assert _send(device2, 19) == 19

    def test_the_import_trim_does_not_wake_an_idle_battery(self):
        device = _ct002(min_discharge_power=50.0, import_trim_w=15.0)
        device._update_consumer_report("a", "A", 0)
        # A steady 40 W import is what the trim targets; 40 + 15 would pass
        # the resume point, but the trim is no demand of the house's.
        for i in range(20):
            assert device._compute_smooth_target([40 + i % 2, 0, 0], "a") == [0, 0, 0]

    def test_peak_shaving_is_applied_first(self):
        device = _ct002(min_discharge_power=50.0, peakshaving_threshold=500.0)
        device._update_consumer_report("a", "A", 0)
        # Demand 520 W, 500 W of it under the threshold: 20 W is left.
        assert _send(device, 520) == 0
        assert _send(device, 800) == 300


class TestPerBattery:
    def test_a_battery_setting_overrides_the_global_one(self):
        device = _ct002(min_discharge_power=50.0)
        device._update_consumer_report("a", "A", 0)
        device.set_consumer_min_discharge_power("a", 0.0)
        assert _send(device, 19) == 19

    def test_a_battery_setting_applies_without_a_global_one(self):
        device = _ct002()
        device._update_consumer_report("a", "A", 0)
        device.set_consumer_min_discharge_power("a", 50.0)
        assert _send(device, 19) == 0
        assert device.effective_min_discharge_power("a") == 50.0
        assert device.effective_min_charge_power("a") == 0.0

    def test_the_setting_survives_eviction(self):
        device = _ct002()
        device._update_consumer_report("a", "A", 0)
        device.set_consumer_min_charge_power("a", 120.0)
        device.set_consumer_min_discharge_power("a", 60.0)
        del device._consumers["a"]
        revived = device._get_consumer("a")
        assert revived.min_charge_power == 120.0
        assert revived.min_discharge_power == 60.0

    @pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf")])
    def test_invalid_values_are_refused(self, value):
        device = _ct002()
        device._update_consumer_report("a", "A", 0)
        with pytest.raises(ValueError):
            device.set_consumer_min_discharge_power("a", value)
        with pytest.raises(ValueError):
            device.set_consumer_min_charge_power("a", value)

    def test_the_controls_reach_up_to_a_16a_three_phase_connection(self):
        assert BATTERY_POWER_MAX == 11040.0
        for field in ("min_charge_power", "min_discharge_power"):
            control = CONSUMER_CONTROLS_BY_FIELD[field]
            assert control.coerce(BATTERY_POWER_MAX) == BATTERY_POWER_MAX
            with pytest.raises(ValueError):
                control.coerce(BATTERY_POWER_MAX + 1)


class TestPool:
    def test_two_batteries_below_the_minimum_become_one_above_it(self):
        device = _ct002(min_discharge_power=50.0)
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        # 80 W would be 40 W each, below the minimum; one runs at 80 W.
        out = {cid: _send(device, 80, cid) for cid in ("a", "b")}
        assert sorted(out.values()) == [0, 80]

    def test_three_batteries_share_among_the_two_that_clear_it(self):
        device = _ct002(min_discharge_power=50.0)
        for cid in ("a", "b", "c"):
            device._update_consumer_report(cid, "A", 0)
        out = {cid: _send(device, 120, cid) for cid in ("a", "b", "c")}
        assert sorted(out.values()) == [0, 60, 60]

    def test_the_whole_pool_idles_when_one_alone_is_below(self):
        device = _ct002(min_discharge_power=50.0)
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        assert [_send(device, 30, cid) for cid in ("a", "b")] == [0, 0]

    def test_each_battery_is_judged_by_its_own_minimum(self):
        device = _ct002()
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("b", "A", 0)
        device.set_consumer_min_discharge_power("a", 100.0)
        # 120 W split 60/60 is below a's 100 W but fine for b.
        out = {cid: _send(device, 120, cid) for cid in ("a", "b")}
        assert out == {"a": 0, "b": 120}

    def test_a_manual_battery_counts_like_solar(self):
        device = _ct002(min_discharge_power=50.0)
        device._update_consumer_report("a", "A", 0)
        device._update_consumer_report("m", "A", 200)
        device.set_consumer_auto_target("m", False)
        device.set_consumer_manual_target("m", 200)
        # The house needs 230 W, the manual battery gives 200 W: 30 W is left.
        assert _send(device, 30, "a") == 0


def test_report_and_config_defaults_mean_no_minimum():
    report = ConsumerReport()
    assert report.min_charge_power is None
    assert report.min_discharge_power is None
    cfg = BalancerConfig()
    assert cfg.min_charge_power == 0
    assert cfg.min_discharge_power == 0
    # Bounded like the per-battery controls.
    assert BalancerConfig(min_discharge_power=1e9).min_discharge_power == (
        BATTERY_POWER_MAX
    )
