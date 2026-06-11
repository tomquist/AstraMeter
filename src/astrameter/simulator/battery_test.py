"""Tests for :class:`BatterySimulator` power target delay."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from astrameter.simulator.battery import BatterySimulator
from astrameter.simulator.runner import parse_config, validate_config


def _battery(delay: int = 0, **kwargs) -> BatterySimulator:
    defaults: dict = {
        "mac": "02B250000001",
        "phase": "A",
        "ct_mac": "112233445566",
        "ct_host": "127.0.0.1",
        "ct_port": 12345,
        "inspection_count": 0,
        "power_update_delay_ticks": delay,
        "startup_delay": 0.0,
        "min_power_threshold": 0.0,
        "ramp_rate": 1e9,
        "poll_interval": 1.0,
    }
    defaults.update(kwargs)
    return BatterySimulator(**defaults)


def test_power_update_immediate_when_delay_zero() -> None:
    b = _battery(0)
    b._current_power = 100.0
    b._apply_ct_derived_target(250.0)
    assert b.target_power == 250.0


@pytest.mark.asyncio
async def test_power_update_delayed_by_n_ticks() -> None:
    b = _battery(2)
    b._current_power = 100.0

    async def send_sets_target(_self: BatterySimulator) -> list[str] | None:
        b._apply_ct_derived_target(250.0)
        return []

    with patch.object(BatterySimulator, "_send_request", new=send_sets_target):
        await b.step(1.0)
        assert b.target_power == 0.0
        await b.step(1.0)
        assert b.target_power == 0.0
        await b.step(1.0)
        assert b.target_power == 250.0


@pytest.mark.asyncio
async def test_power_update_delay_one_tick() -> None:
    b = _battery(1)

    async def send_sets_target(_self: BatterySimulator) -> list[str] | None:
        b._apply_ct_derived_target(80.0)
        return []

    with patch.object(BatterySimulator, "_send_request", new=send_sets_target):
        await b.step(1.0)
        assert b.target_power == 0.0
        d = b.to_dict()
        assert d["target"] == 80
        assert d["applied_target"] == 0
        await b.step(1.0)
        assert b.target_power == 80.0
        assert b.to_dict()["target"] == 80
        assert b.to_dict()["applied_target"] == 80


def test_dc_passthrough_at_full_soc() -> None:
    """Venus D-like: full SoC + DC input → AC output forced to DC value."""
    b = _battery(max_dc_input=500, initial_soc=1.0, dc_input_power=500.0)
    # Even if the AC target is "charge", passthrough should override.
    b._apply_ct_derived_target(-300.0)
    b._update_power(1.0)
    assert b.current_power >= 500.0, (
        f"Expected DC passthrough to force +500W, got {b.current_power}"
    )


def test_dc_passthrough_inactive_when_soc_below_one() -> None:
    """Below full SoC, DC input is absorbed by the cells, not passed through."""
    b = _battery(max_dc_input=500, initial_soc=0.5, dc_input_power=500.0)
    b._apply_ct_derived_target(0.0)
    b._update_power(1.0)
    # Without saturation, the inverter doesn't dump DC to AC.
    assert b.current_power == 0.0


def test_dc_input_charges_cells_below_full_soc() -> None:
    """DC input raises SoC over time when not full."""
    b = _battery(
        max_dc_input=500,
        initial_soc=0.5,
        dc_input_power=500.0,
        capacity_wh=1000.0,  # small capacity for visible change
    )
    initial_soc = b.soc
    # Drive 60 seconds of DC input with no AC activity.
    for _ in range(60):
        b._update_soc(1.0)
    assert b.soc > initial_soc, (
        f"DC input should charge cells; SoC went {initial_soc} -> {b.soc}"
    )


def test_dc_input_setter_clamps_to_max() -> None:
    b = _battery(max_dc_input=500)
    b.dc_input_power = 2000.0  # over max
    assert b.dc_input_power == 500.0
    b.dc_input_power = -10.0  # below zero
    assert b.dc_input_power == 0.0


def _response_fields(
    phase_targets: tuple[int, int, int] = (0, 0, 0),
    dchrg: tuple[int, int, int] = (0, 0, 0),
) -> list[str]:
    """Build a CT002 response field list with the given phase targets + dchrg."""
    return [
        "HME-4",
        "112233445566",
        "HMG-50",
        "02B250000001",
        str(phase_targets[0]),
        str(phase_targets[1]),
        str(phase_targets[2]),
        str(sum(phase_targets)),
        "0",
        "0",
        "0",
        "0",  # *_chrg_nb
        "-50",
        "1",  # wifi_rssi, info_idx
        "0",
        "0",
        "0",
        "0",
        "0",  # x/A/B/C/ABC chrg_power
        "0",
        str(dchrg[0]),
        str(dchrg[1]),
        str(dchrg[2]),
        "0",  # x/A/B/C/ABC dchrg_power
    ]


def test_idle_on_cross_phase_discharge_when_flag_on() -> None:
    """A charging battery sees another phase's dchrg>0 → target snaps to 0."""
    b = _battery(idle_on_cross_phase_discharge=True)  # phase A
    b._current_power = -500.0  # currently charging

    # phase_C grid = -500 (charge signal); B_dchrg = 400 (cross-phase
    # discharge instruction from another battery on phase B).
    fields = _response_fields(phase_targets=(0, 0, -500), dchrg=(0, 400, 0))
    b._handle_ct_response(fields)

    assert b.target_power == 0.0, (
        f"Expected target forced to 0 by cross-phase dchrg signal, got {b.target_power}"
    )


def test_no_idle_when_flag_off() -> None:
    """Flag off → cross-phase dchrg ignored, steering controller drives target."""
    b = _battery(idle_on_cross_phase_discharge=False)  # phase A
    b._current_power = -500.0

    fields = _response_fields(phase_targets=(0, 0, -500), dchrg=(0, 400, 0))
    b._handle_ct_response(fields)

    # grid_reading=-500 (export): the controller's first step drives its setpoint
    # to +500 (charge), i.e. simulator target -500.
    assert b.target_power == -500.0


def test_idle_ignores_own_phase_dchrg() -> None:
    """A_dchrg on the battery's own phase doesn't trigger the idle rule."""
    b = _battery(idle_on_cross_phase_discharge=True)  # phase A
    b._current_power = -500.0

    # Same-phase (A) dchrg should NOT trigger idle; the controller's first step
    # on grid_reading=-500 yields target -500.
    fields = _response_fields(phase_targets=(-500, 0, 0), dchrg=(400, 0, 0))
    b._handle_ct_response(fields)

    assert b.target_power == -500.0


def test_non_participating_battery_appends_seventh_field() -> None:
    """A non-participating battery appends the 7th 'participate' field as 0."""
    b = _battery(participates=False)
    b._current_power = -100.0
    fields = b._request_fields()
    assert len(fields) == 7
    assert fields[6] == "0"


def test_participating_battery_omits_seventh_field() -> None:
    """A participating battery sends only the 6 base fields (Venus-style)."""
    b = _battery(participates=True)
    b._current_power = -100.0
    assert len(b._request_fields()) == 6


def test_parse_config_power_update_delay_ticks() -> None:
    data = {
        "power_update_delay_ticks": 3,
        "batteries": [
            {"mac": "02B250000001", "phase": "A"},
            {"mac": "02B250000002", "phase": "B", "power_update_delay_ticks": 1},
        ],
    }
    cfg = parse_config(data)
    validate_config(cfg)
    assert cfg.power_update_delay_ticks == 3
    assert cfg.batteries[0].power_update_delay_ticks == 3
    assert cfg.batteries[1].power_update_delay_ticks == 1


def test_parse_config_venus_d_fields() -> None:
    data = {
        "batteries": [
            {
                "mac": "02B250000001",
                "phase": "A",
                "max_dc_input": 800,
                "dc_input_power": 500,
                "idle_on_cross_phase_discharge": True,
            },
        ],
    }
    cfg = parse_config(data)
    validate_config(cfg)
    bc = cfg.batteries[0]
    assert bc.max_dc_input == 800
    assert bc.dc_input_power == 500.0
    assert bc.idle_on_cross_phase_discharge is True
