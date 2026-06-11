"""Unit tests for simulator/runner.py — config parsing and validation."""

import json

import pytest

from astrameter.simulator.load_model import Load
from astrameter.simulator.runner import (
    BatteryConfig,
    SimulationConfig,
    SimulationRunner,
    parse_config,
    quick_config,
    validate_config,
)

# -- parse_config ----------------------------------------------------------


def test_parse_config_minimal():
    data = {
        "batteries": [{"mac": "AABBCCDDEE01", "phase": "A"}],
    }
    cfg = parse_config(data)
    assert len(cfg.batteries) == 1
    assert cfg.batteries[0].mac == "AABBCCDDEE01"
    assert cfg.batteries[0].phase == "A"
    assert cfg.batteries[0].max_charge_power == 800  # default
    assert cfg.ct_mac == "112233445566"  # default


def test_parse_config_full():
    data = {
        "ct": {"mac": "112233445566", "host": "10.0.0.1", "port": 9999},
        "http": {"host": "0.0.0.0", "port": 8888},
        "powermeter": {
            "base_load": [200.0, 150.0, 100.0],
            "base_noise": 10.0,
            "loads": [{"name": "TV", "power": 80, "phase": "A"}],
            "solar_max": 5000.0,
            "solar_phases": ["A", "B"],
        },
        "batteries": [
            {
                "mac": "AABBCCDDEE01",
                "phase": "B",
                "max_charge_power": 1000,
                "max_discharge_power": 900,
                "capacity_wh": 5000.0,
                "initial_soc": 0.8,
                "ramp_rate": 300.0,
                "poll_interval": 2.0,
                "power_update_delay_ticks": 5,
                "max_dc_input": 500,
                "dc_input_power": 100.0,
                "idle_on_cross_phase_discharge": True,
            }
        ],
        "power_update_delay_ticks": 3,
        "auto_mode": True,
        "auto_interval": [5, 15],
        "log_interval": 10.0,
        "time_scale": 2.0,
    }
    cfg = parse_config(data)
    assert cfg.ct_host == "10.0.0.1"
    assert cfg.ct_port == 9999
    assert cfg.http_host == "0.0.0.0"
    assert cfg.http_port == 8888
    assert cfg.base_load == [200.0, 150.0, 100.0]
    assert cfg.base_noise == 10.0
    assert len(cfg.loads) == 1
    assert cfg.loads[0].name == "TV"
    assert cfg.solar_max == 5000.0
    assert cfg.solar_phases == ["A", "B"]
    assert cfg.batteries[0].max_charge_power == 1000
    assert cfg.batteries[0].max_dc_input == 500
    assert cfg.batteries[0].dc_input_power == 100.0
    assert cfg.batteries[0].idle_on_cross_phase_discharge is True
    assert cfg.batteries[0].power_update_delay_ticks == 5
    assert cfg.auto_mode is True
    assert cfg.auto_interval == (5, 15)
    assert cfg.log_interval == 10.0
    assert cfg.time_scale == 2.0


def test_parse_config_inherits_global_delay():
    data = {
        "batteries": [{"mac": "AABBCCDDEE01", "phase": "A"}],
        "power_update_delay_ticks": 7,
    }
    cfg = parse_config(data)
    assert cfg.batteries[0].power_update_delay_ticks == 7


def test_parse_config_battery_overrides_delay():
    data = {
        "batteries": [
            {"mac": "AABBCCDDEE01", "phase": "A", "power_update_delay_ticks": 3}
        ],
        "power_update_delay_ticks": 7,
    }
    cfg = parse_config(data)
    assert cfg.batteries[0].power_update_delay_ticks == 3


# -- validate_config -------------------------------------------------------


def _valid_battery(**overrides) -> BatteryConfig:
    defaults = dict(
        mac="AABBCCDDEE01",
        phase="A",
        max_charge_power=800,
        max_discharge_power=800,
        capacity_wh=2560.0,
        initial_soc=0.5,
        ramp_rate=200.0,
        poll_interval=1.0,
        power_update_delay_ticks=0,
        max_dc_input=0,
        dc_input_power=0.0,
    )
    defaults.update(overrides)
    return BatteryConfig(**defaults)


def _valid_config(**overrides) -> SimulationConfig:
    defaults = dict(
        batteries=[_valid_battery()],
        time_scale=1.0,
    )
    defaults.update(overrides)
    return SimulationConfig(**defaults)


def test_validate_config_valid():
    validate_config(_valid_config())


def test_validate_config_invalid_phase():
    cfg = _valid_config(batteries=[_valid_battery(phase="D")])
    with pytest.raises(ValueError, match="invalid phase"):
        validate_config(cfg)


def test_validate_config_invalid_soc():
    cfg = _valid_config(batteries=[_valid_battery(initial_soc=1.5)])
    with pytest.raises(ValueError, match="initial_soc"):
        validate_config(cfg)


def test_validate_config_negative_power():
    cfg = _valid_config(batteries=[_valid_battery(max_charge_power=-1)])
    with pytest.raises(ValueError, match="power values must be >= 0"):
        validate_config(cfg)


def test_validate_config_negative_delay():
    cfg = _valid_config(batteries=[_valid_battery(power_update_delay_ticks=-1)])
    with pytest.raises(ValueError, match="power_update_delay_ticks must be >= 0"):
        validate_config(cfg)


def test_validate_config_negative_dc_input():
    cfg = _valid_config(batteries=[_valid_battery(max_dc_input=-1)])
    with pytest.raises(ValueError, match="max_dc_input must be >= 0"):
        validate_config(cfg)


def test_validate_config_dc_input_power_out_of_range():
    cfg = _valid_config(
        batteries=[_valid_battery(max_dc_input=100, dc_input_power=200.0)]
    )
    with pytest.raises(ValueError, match="dc_input_power must be within"):
        validate_config(cfg)


def test_validate_config_bad_mac():
    cfg = _valid_config(batteries=[_valid_battery(mac="ZZZZZZZZZZZZ")])
    with pytest.raises(ValueError, match="12 hex chars"):
        validate_config(cfg)


def test_validate_config_short_mac():
    cfg = _valid_config(batteries=[_valid_battery(mac="AABB")])
    with pytest.raises(ValueError, match="12 hex chars"):
        validate_config(cfg)


def test_validate_config_duplicate_mac():
    b1 = _valid_battery(mac="AABBCCDDEE01")
    b2 = _valid_battery(mac="AABBCCDDEE01")
    cfg = _valid_config(batteries=[b1, b2])
    with pytest.raises(ValueError, match="Duplicate battery MAC"):
        validate_config(cfg)


def test_validate_config_invalid_load_phase():
    cfg = _valid_config(loads=[Load("bad", 100, "X")])
    with pytest.raises(ValueError, match="invalid phase"):
        validate_config(cfg)


def test_validate_config_invalid_solar_phase():
    cfg = _valid_config(solar_phases=["Z"])
    with pytest.raises(ValueError, match="Invalid solar phase"):
        validate_config(cfg)


def test_validate_config_zero_time_scale():
    cfg = _valid_config(time_scale=0.0)
    with pytest.raises(ValueError, match="time_scale must be positive"):
        validate_config(cfg)


def test_validate_config_negative_time_scale():
    cfg = _valid_config(time_scale=-1.0)
    with pytest.raises(ValueError, match="time_scale must be positive"):
        validate_config(cfg)


# -- quick_config ----------------------------------------------------------


def test_quick_config_defaults():
    cfg = quick_config()
    assert len(cfg.batteries) == 1
    assert cfg.batteries[0].phase == "A"
    assert cfg.base_load == [300.0, 0.0, 0.0]
    validate_config(cfg)


def test_quick_config_multi_battery():
    cfg = quick_config(num_batteries=3, num_phases=3)
    assert len(cfg.batteries) == 3
    phases = [b.phase for b in cfg.batteries]
    assert phases == ["A", "B", "C"]
    assert cfg.base_load == [100.0, 100.0, 100.0]
    validate_config(cfg)


def test_quick_config_custom_soc():
    cfg = quick_config(initial_soc=0.9)
    assert cfg.batteries[0].initial_soc == 0.9
    validate_config(cfg)


def test_quick_config_custom_base_load():
    cfg = quick_config(base_load=[500.0, 200.0, 100.0])
    assert cfg.base_load == [500.0, 200.0, 100.0]
    validate_config(cfg)


def test_quick_config_delay():
    cfg = quick_config(power_update_delay_ticks=5)
    assert cfg.batteries[0].power_update_delay_ticks == 5
    validate_config(cfg)


# -- SimulationRunner construction -----------------------------------------


def test_runner_builds_from_config():
    cfg = quick_config(num_batteries=2)
    runner = SimulationRunner(cfg)
    assert len(runner.batteries) == 2
    assert runner.load_model is not None
    assert runner.powermeter is not None


def test_runner_from_config_file(tmp_path):
    config_data = {
        "batteries": [{"mac": "AABBCCDDEE01", "phase": "A"}],
        "ct": {"mac": "112233445566"},
    }
    path = tmp_path / "sim.json"
    path.write_text(json.dumps(config_data))
    runner = SimulationRunner.from_config_file(path)
    assert len(runner.batteries) == 1
    assert runner.batteries[0].mac == "AABBCCDDEE01"
