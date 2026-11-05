"""Regression checks for battery simulator behavior."""

from __future__ import annotations

import pytest

from .battery_test import _battery, _response_fields


@pytest.mark.parametrize("phase", ["A", "B", "C"])
@pytest.mark.parametrize("firmware_mode", [False, True])
def test_total_reading_shares_across_all_phase_buckets(
    phase: str,
    firmware_mode: bool,
) -> None:
    b = _battery(
        meter_dev_type="VNSE3-0",
        phase=phase,
        phase_detection=firmware_mode,
        stored_phase=phase,
        initial_power=200,
    )
    fields = _response_fields(phase_targets=(200, 200, 200))
    fields[8:12] = ["1", "1", "1", "0"]
    if firmware_mode:
        b._receive_firmware(fields)
    else:
        b._handle_ct_response(fields)
    assert b.target_power == 395  # 200 + 600/3 - 5


@pytest.mark.parametrize("firmware_mode", [False, True])
def test_phase_setting_selects_both_reading_and_count(firmware_mode: bool) -> None:
    b = _battery(
        meter_dev_type="VNSE3-0",
        phase="B",
        sum_all_phases=False,
        phase_detection=firmware_mode,
        stored_phase="B",
        initial_power=200,
    )
    fields = _response_fields(phase_targets=(800, 400, -300))
    fields[8:12] = ["3", "2", "4", "1"]
    if firmware_mode:
        b._receive_firmware(fields)
    else:
        b._handle_ct_response(fields)
    assert b.target_power == 395  # 200 + phase B's 400/2 - 5


def test_total_share_includes_combined_phase_devices() -> None:
    b = _battery(meter_dev_type="VNSE3-0", initial_power=200)
    fields = _response_fields(phase_targets=(200, 200, 200))
    fields[8:12] = ["1", "0", "0", "2"]
    b._handle_ct_response(fields)
    assert b.target_power == 395
