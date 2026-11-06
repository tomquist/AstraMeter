"""Regression checks for battery simulator behavior."""

from __future__ import annotations

import pytest

from .battery_test import _battery, _response_fields


@pytest.mark.parametrize("device", ["HMN-1", "HMM-1", "JPLS-1", "HMJ-2"])
def test_dc_only_devices_never_charge_from_ac(device: str) -> None:
    b = _battery(meter_dev_type=device, initial_power=-500)
    assert b.current_power == 0
    initial_soc = b.soc
    for _ in range(20):
        b._handle_ct_response(_response_fields(phase_targets=(-500, 0, 0)))
        b._advance(1)
        assert b.target_power == 0
        assert b.current_power == 0
        assert b.soc == initial_soc
    # The plant and delayed targets also enforce the capability independently
    # of which controller or caller requests charging.
    b._apply_ct_derived_target(-800)
    assert b.to_dict()["target"] == 0
    b._target_power = -800
    soc_before = b.soc
    b.current_power = -300
    b._advance(0.1)
    assert b.current_power >= 0
    assert b.soc <= soc_before


@pytest.mark.parametrize("device", ["HMN-1", "HMM-1", "JPLS-1"])
def test_jupiter_can_still_charge_from_dc(device: str) -> None:
    b = _battery(meter_dev_type=device, max_dc_input=500, dc_input_power=300)
    before = b.soc
    b._advance(60)
    assert b.current_power == 0
    assert b.soc > before


@pytest.mark.parametrize("device", ["HMN-1", "HMM-1", "JPLS-1"])
def test_jupiter_can_still_discharge_on_import(device: str) -> None:
    b = _battery(meter_dev_type=device)
    before = b.soc
    for _ in range(6):
        b._handle_ct_response(_response_fields(phase_targets=(500, 0, 0)))
    b._advance(1)
    assert b.target_power > 0
    assert b.current_power > 0
    assert b.soc < before
