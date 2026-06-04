"""Codegen unit tests for the ct002 ESPHome external component.

These tests import the schema validators from `esphome/components/ct002/__init__.py`
directly and exercise them without spinning up the full ESPHome codegen
pipeline. Skipped if ESPHome isn't installed, since the schema imports
`esphome.codegen` etc.; the YAML compile matrix in CI is the integration-level
guard.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

esphome = pytest.importorskip(
    "esphome", reason="ESPHome not installed; skipping codegen unit tests"
)

# Make the `esphome/components/ct002/__init__.py` importable as a plain module.
REPO_ROOT = Path(__file__).parent.parent.parent.parent
COMPONENTS_PATH = REPO_ROOT / "esphome" / "components"
sys.path.insert(0, str(COMPONENTS_PATH))

import ct002 as ct002_component  # noqa: E402


def test_validate_ct_mac_accepts_empty():
    assert ct002_component._validate_ct_mac("") == ""


def test_validate_ct_mac_normalizes_colons_and_case():
    assert ct002_component._validate_ct_mac("02:B2:50:12:AB:CD") == "02b25012abcd"
    assert ct002_component._validate_ct_mac("02-B2-50-12-AB-CD") == "02b25012abcd"
    assert ct002_component._validate_ct_mac("02b25012abcd") == "02b25012abcd"


def test_validate_ct_mac_rejects_wrong_length():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid):
        ct002_component._validate_ct_mac("02b250")


def test_validate_ct_mac_rejects_non_hex():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid):
        ct002_component._validate_ct_mac("zzbb250012abcd"[:12])


def test_three_phase_validator_accepts_l1_only():
    config = {ct002_component.CONF_POWER_SENSOR_L1: "grid_l1"}
    assert ct002_component._validate_three_phase_sensors(config) is config


def test_three_phase_validator_accepts_all_three():
    config = {
        ct002_component.CONF_POWER_SENSOR_L1: "grid_l1",
        ct002_component.CONF_POWER_SENSOR_L2: "grid_l2",
        ct002_component.CONF_POWER_SENSOR_L3: "grid_l3",
    }
    assert ct002_component._validate_three_phase_sensors(config) is config


def test_three_phase_validator_rejects_only_l2():
    import esphome.config_validation as cv

    config = {
        ct002_component.CONF_POWER_SENSOR_L1: "grid_l1",
        ct002_component.CONF_POWER_SENSOR_L2: "grid_l2",
    }
    with pytest.raises(cv.Invalid):
        ct002_component._validate_three_phase_sensors(config)


def test_three_phase_validator_rejects_only_l3():
    import esphome.config_validation as cv

    config = {
        ct002_component.CONF_POWER_SENSOR_L1: "grid_l1",
        ct002_component.CONF_POWER_SENSOR_L3: "grid_l3",
    }
    with pytest.raises(cv.Invalid):
        ct002_component._validate_three_phase_sensors(config)


def test_mqtt_insights_device_id_defaults_to_python_default():
    # A blank device_id must fall back to the same default the Python add-on
    # uses, so both stacks publish HA discovery under astrameter_ct002_device-1
    # (regression: ESPHome used to derive it from the ct002: component id,
    # yielding astrameter_ct002_ct002_main).
    assert ct002_component.DEFAULT_MQTT_INSIGHTS_DEVICE_ID == "device-1"
    assert ct002_component._resolve_mqtt_insights_device_id("") == "device-1"


def test_mqtt_insights_device_id_uses_explicit_value():
    assert ct002_component._resolve_mqtt_insights_device_id("garage") == "garage"


def test_mqtt_insights_schema_device_id_blank_by_default():
    # The schema default must stay blank so _resolve_mqtt_insights_device_id
    # (not the schema) owns the fallback at to_code time. The sub-block schema
    # gates on an `mqtt:` component via cv.requires_component, so mark it loaded.
    from esphome.core import CORE

    added = "mqtt" not in CORE.loaded_integrations
    if added:
        CORE.loaded_integrations.add("mqtt")
    try:
        config = ct002_component.MQTT_INSIGHTS_SCHEMA({})
    finally:
        if added:
            CORE.loaded_integrations.discard("mqtt")
    assert config[ct002_component.CONF_DEVICE_ID] == ""
    assert (
        ct002_component._resolve_mqtt_insights_device_id(
            config[ct002_component.CONF_DEVICE_ID]
        )
        == "device-1"
    )
