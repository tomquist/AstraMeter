"""Regression checks for battery simulator behavior."""

from __future__ import annotations

import pytest

from .battery_test import _battery, _response_fields


@pytest.mark.parametrize("device", ["VNSA-0", "VNSD-0", "VNSE3-0"])
@pytest.mark.parametrize("firmware_mode", [False, True])
def test_venus_smr2_is_explicitly_unsupported(device: str, firmware_mode: bool) -> None:
    with pytest.raises(ValueError, match="SMR-2 steering"):
        _battery(
            meter_dev_type=device, ct_dev_type="SMR-2", phase_detection=firmware_mode
        )


@pytest.mark.parametrize("meter", ["SMR-0", "SMR-1", "TPM2-0"])
@pytest.mark.parametrize("firmware_mode", [False, True])
def test_venus_uses_its_meter_type_for_the_spike_gate(
    meter: str,
    firmware_mode: bool,
) -> None:
    b = _battery(
        meter_dev_type="VNSE3-0",
        ct_dev_type=meter,
        phase_detection=firmware_mode,
        stored_phase="A",
    )
    fields = _response_fields(phase_targets=(500, 0, 0))
    fields[0] = meter
    fields[8] = "1"
    if firmware_mode:
        b._receive_firmware(fields)
    else:
        b._handle_ct_response(fields)
    assert b.target_power == 495
