"""Tests for :class:`BatterySimulator` with firmware phase detection on.

Replies are fed straight to the reply handler, so no UDP is involved.
"""

from __future__ import annotations

from typing import Any

import pytest

from astrameter.simulator.battery import (
    METER_OFFLINE_AFTER_FAILURES,
    BatterySimulator,
)
from astrameter.simulator.phase_detection import HMG50PhaseDetector, VenusPhaseDetector


def _battery(**kwargs: Any) -> BatterySimulator:
    defaults: dict[str, Any] = {
        "mac": "02B250000001",
        "phase": "B",
        "ct_mac": "000000000000",
        "ct_dev_type": "HME-4",
        "phase_detection": True,
        "startup_delay": 0.0,
        "min_power_threshold": 0.0,
        "ramp_rate": 1e9,
        "initial_soc": 0.8,
    }
    defaults.update(kwargs)
    return BatterySimulator(**defaults)


def _reply(
    a: int = 300,
    b: int = 200,
    c: int = 150,
    *,
    info_idx: int = 1,
    ct_type: str = "HME-4",
    ct_mac: str = "112233445566",
    a_nb: int = 0,
    b_nb: int = 0,
) -> list[str]:
    fields = [ct_type, ct_mac, "HMG-50", "02B250000001"]
    fields += [str(a), str(b), str(c), str(a + b + c)]
    fields += [str(a_nb), str(b_nb), "0", "0", "-50", str(info_idx)]
    return fields + ["0"] * 10


def test_detector_follows_the_device_type() -> None:
    assert isinstance(_battery().detector, HMG50PhaseDetector)
    assert isinstance(_battery(meter_dev_type="VNSE3-0").detector, VenusPhaseDetector)
    assert _battery(phase_detection=False).detector is None


def test_unmodelled_family_is_rejected() -> None:
    with pytest.raises(ValueError, match="not modelled"):
        _battery(meter_dev_type="HMJ-2")


def test_invalid_stored_phase_is_rejected() -> None:
    with pytest.raises(ValueError, match="stored_phase"):
        _battery(stored_phase="X")


def test_reports_zero_until_detected_then_its_phase() -> None:
    b = _battery()
    assert b.reported_phase == "0"
    assert b._request_fields()[4] == "0"
    b._receive_firmware(_reply(info_idx=1))  # online: first pass zeroes
    b._advance(0.1)  # baseline + probe (2000 W -> clamped by the plant)
    assert b.target_power == 2000.0
    assert b.current_power == 800.0  # max_discharge_power
    # Phase B (where it is wired) drops by the probe.
    b._receive_firmware(_reply(b=200 - 800, info_idx=2))
    b._advance(0.1)
    assert b.reported_phase == "B"
    assert b._request_fields()[4] == "B"
    assert b.to_dict()["reported_phase"] == "B"


def test_stored_phase_reports_and_steers_at_once() -> None:
    b = _battery(stored_phase="C")
    assert b.reported_phase == "C"
    # Twice: the steering gate's one-shot spike filter skips the first jump.
    b._receive_firmware(_reply(c=300, info_idx=1))
    b._receive_firmware(_reply(c=300, info_idx=2))
    assert b.target_power > 0  # steered on phase C's import


def test_no_steering_without_a_phase() -> None:
    b = _battery()
    # A reply that, with a phase, would make the battery discharge.
    b._receive_firmware(_reply(a=900, b=900, c=900, info_idx=1))
    assert b.target_power == pytest.approx(0.01)  # the detector's zero, no steering


def test_sum_all_phases_steers_on_the_total() -> None:
    b = _battery(stored_phase="A", phase="C")
    # The total (field 8) is what counts, split by every bucket's count.
    for i in (1, 2):
        b._receive_firmware(_reply(a=0, b=0, c=500, info_idx=i))
    assert b.target_power > 0


def test_steering_uses_the_detected_phase_field() -> None:
    b = _battery(stored_phase="A", phase="C", sum_all_phases=False)
    b._receive_firmware(_reply(a=0, b=0, c=500, info_idx=1))
    b._receive_firmware(_reply(a=0, b=0, c=500, info_idx=2))
    assert b.target_power == 0  # A reads 0: nothing to do, C is ignored
    b._receive_firmware(_reply(a=500, b=0, c=0, info_idx=3))
    b._receive_firmware(_reply(a=500, b=0, c=0, info_idx=4))
    assert b.target_power > 0


def test_other_ct_type_is_rejected() -> None:
    b = _battery()
    for i in range(METER_OFFLINE_AFTER_FAILURES):
        b._receive_firmware(_reply(ct_type="HME-3", info_idx=i + 1))
    assert not b.meter_online
    assert b._readings == (0, 0, 0, 0)


def test_bound_ct_mac_must_match() -> None:
    b = _battery(ct_mac="AABBCCDDEEFF")
    b._receive_firmware(_reply(ct_mac="112233445566"))
    assert not b.meter_online
    b._receive_firmware(_reply(ct_mac="AABBCCDDEEFF"))
    assert b.meter_online


def test_repeated_info_idx_keeps_the_meter_online_but_brings_no_reading() -> None:
    b = _battery()
    b._receive_firmware(_reply(a=100, info_idx=7))
    assert b._readings[0] == 100
    b._receive_firmware(_reply(a=999, info_idx=7))
    assert b.meter_online and b._readings[0] == 100
    # info_idx 0 repeating is still a new reading.
    b._receive_firmware(_reply(a=1, info_idx=0))
    b._receive_firmware(_reply(a=2, info_idx=0))
    assert b._readings[0] == 2


def test_meter_goes_offline_after_failed_polls() -> None:
    b = _battery(stored_phase="A")
    b._receive_firmware(_reply(info_idx=1))
    for _ in range(METER_OFFLINE_AFTER_FAILURES - 1):
        b._note_failed_poll()
    assert b.meter_online
    b._note_failed_poll()
    assert not b.meter_online


def test_start_phase_detection_drops_output_and_phase() -> None:
    b = _battery(stored_phase="A")
    b._apply_ct_derived_target(500.0)
    b.start_phase_detection()
    assert b.reported_phase == "0"
    assert b.target_power == pytest.approx(0.01)


def test_start_phase_detection_needs_detection_on() -> None:
    with pytest.raises(RuntimeError):
        _battery(phase_detection=False).start_phase_detection()


def test_without_detection_the_old_inspection_count_applies() -> None:
    b = _battery(phase_detection=False, inspection_count=2)
    assert b.reported_phase == "0"
    b._request_count = 2
    assert b.reported_phase == "B"


def _venus_d(**kwargs: Any) -> BatterySimulator:
    defaults: dict[str, Any] = {
        "meter_dev_type": "VNSD-0",
        "max_dc_input": 2500,
        "max_charge_power": 800,
        "initial_soc": 0.4,
    }
    defaults.update(kwargs)
    return _battery(**defaults)


def test_venus_d_charging_gives_way_to_pv() -> None:
    b = _venus_d(dc_input_power=600)
    b._apply_ct_derived_target(-800.0)
    assert b.target_power == -200.0  # PV fills 600 W of the 800 W limit
    b.dc_input_power = 900
    b._apply_ct_derived_target(-800.0)
    assert b.target_power == 0.0  # PV alone covers the limit
    b.dc_input_power = 10  # not above 10 W: no PV as far as the rule goes
    b._apply_ct_derived_target(-800.0)
    assert b.target_power == -800.0
    b._apply_ct_derived_target(500.0)
    assert b.target_power == 500.0  # discharging is left alone


def test_venus_d_charge_cap_is_2500_w() -> None:
    b = _venus_d(max_charge_power=3000, dc_input_power=1000)
    b._apply_ct_derived_target(-3000.0)
    assert b.target_power == -1500.0


def test_pv_rules_are_venus_d_only_and_need_firmware_mode() -> None:
    for b in (
        _venus_d(meter_dev_type="VNSE3-0", dc_input_power=600),
        _venus_d(phase_detection=False, dc_input_power=600),
    ):
        b._apply_ct_derived_target(-800.0)
        assert b.target_power == -800.0


def test_venus_d_surplus_feed_in_floors_the_output() -> None:
    b = _venus_d(surplus_feed_in=True, initial_soc=1.0, dc_input_power=300)
    b._apply_ct_derived_target(0.0)
    assert b.target_power == 310.0
    b._apply_ct_derived_target(800.0)
    assert b.target_power == 800.0
    b.soc = 0.96  # holds down to 95 %
    b._apply_ct_derived_target(0.0)
    assert b.target_power == 310.0
    b.soc = 0.94
    b._apply_ct_derived_target(0.0)
    assert b.target_power == 0.0
    b.soc = 0.97  # below 100 % it does not start again
    b._apply_ct_derived_target(0.0)
    assert b.target_power == 0.0


def test_venus_d_feed_in_needs_the_setting() -> None:
    b = _venus_d(initial_soc=1.0, dc_input_power=300)
    b._apply_ct_derived_target(0.0)
    assert b.target_power == 0.0


def test_venus_d_charge_probe_shrinks_with_pv() -> None:
    # Below 50 % the probe charges at the configured power; with 600 W of PV
    # only 200 W of it reaches the AC side, under the 400 W threshold.
    b = _venus_d(dc_input_power=600)
    b._receive_firmware(_reply(info_idx=1))
    for _ in range(60):  # past the 3 s baseline
        b._advance(0.1)
    assert b.detector is not None and not b.detector.steering
    assert b.target_power == -200.0
