"""Tests for :class:`BatterySimulator` power target delay."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from astrameter.simulator.battery import BatterySimulator
from astrameter.simulator.runner import parse_config, validate_config


def _battery(delay: int = 0) -> BatterySimulator:
    return BatterySimulator(
        mac="02B250000001",
        phase="A",
        ct_mac="112233445566",
        ct_host="127.0.0.1",
        ct_port=12345,
        inspection_count=0,
        power_update_delay_ticks=delay,
        startup_delay=0.0,
        min_power_threshold=0.0,
        ramp_rate=1e9,
        poll_interval=1.0,
    )


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
