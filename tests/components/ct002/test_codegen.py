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


def test_power_unit_scale_accepts_power_units():
    assert ct002_component._power_unit_scale("W") == 1.0
    assert ct002_component._power_unit_scale("kW") == 1000.0
    assert ct002_component._power_unit_scale("MW") == 1e6
    assert ct002_component._power_unit_scale("mW") == 0.001


def test_power_unit_scale_assumes_watts_when_undeclared():
    assert ct002_component._power_unit_scale(None) == 1.0
    assert ct002_component._power_unit_scale("") == 1.0


def test_power_unit_scale_rejects_non_power_units():
    # Case matters ("mW" milli vs "MW" mega), so "KW"/"w" are not accepted;
    # non-power units (temperature, energy, ...) must map to None → rejected.
    for unit in ("°C", "%", "V", "A", "kWh", "Wh", "KW", "w"):
        assert ct002_component._power_unit_scale(unit) is None


def test_validate_power_unit_accepts_watts_and_undeclared():
    ct002_component._validate_power_unit("power_sensor_l1", "grid_l1", "W")
    ct002_component._validate_power_unit("power_sensor_l1", "grid_l1", "kW")
    ct002_component._validate_power_unit("power_sensor_l1", "grid_l1", None)


def test_validate_power_unit_rejects_celsius():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid) as exc_info:
        ct002_component._validate_power_unit("power_sensor_l1", "temp_sensor", "°C")
    assert "not a power unit" in str(exc_info.value)


def test_validate_power_unit_rejects_energy_unit():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid):
        ct002_component._validate_power_unit("power_sensor_l2", "meter_total", "kWh")


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


# ── dashboard sub-block ────────────────────────────────────────────────


def test_dashboard_shorthand_accepts_bare_key_and_true():
    # Turning the dashboard on must not require knowing an option name:
    # `dashboard:` and `dashboard: true` both mean "on, with the defaults".
    assert ct002_component._dashboard_shorthand(None) == {}
    assert ct002_component._dashboard_shorthand(True) == {}


def test_dashboard_shorthand_passes_a_block_through():
    block = {"path": "/astrameter"}
    assert ct002_component._dashboard_shorthand(block) is block


def test_dashboard_false_is_the_same_as_leaving_it_out():
    # ...and it must drop the key entirely, so a disabled dashboard pulls in
    # no HTTP server and no page in flash.
    config = {"power_sensor_l1": "grid_l1", "dashboard": False}
    assert ct002_component._drop_disabled_dashboard(config) == {
        "power_sensor_l1": "grid_l1"
    }


def test_dashboard_absent_config_is_untouched():
    config = {"power_sensor_l1": "grid_l1"}
    assert ct002_component._drop_disabled_dashboard(config) == config


def test_dashboard_path_normalizes_to_a_prefix():
    # The mount prefix is concatenated with "/api/status" at runtime, so it
    # must never carry a trailing slash; the root becomes the empty prefix.
    assert ct002_component._validate_dashboard_path("/") == ""
    assert ct002_component._validate_dashboard_path("/astrameter") == "/astrameter"
    assert ct002_component._validate_dashboard_path("/astrameter/") == "/astrameter"


def test_dashboard_path_requires_a_leading_slash():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid):
        ct002_component._validate_dashboard_path("astrameter")


def test_dashboard_path_rejects_a_query_string():
    import esphome.config_validation as cv

    with pytest.raises(cv.Invalid):
        ct002_component._validate_dashboard_path("/astrameter?x=1")


def test_auto_load_pulls_the_web_server_only_when_asked():
    # A build without a dashboard must not gain an HTTP server.
    assert "web_server_base" not in ct002_component.AUTO_LOAD({})
    assert "web_server_base" in ct002_component.AUTO_LOAD({"dashboard": {}})


def test_auto_load_always_carries_the_sub_block_infrastructure():
    for component in ("socket", "json", "md5"):
        assert component in ct002_component.AUTO_LOAD({})


def test_dashboard_at_the_root_conflicts_with_esphome_web_server():
    # Both mount on the shared HTTP server and the first handler to claim a
    # URL wins, so "/" for two pages would resolve on codegen order alone.
    import esphome.config_validation as cv

    config = {ct002_component.CONF_DASHBOARD: {ct002_component.CONF_PATH: ""}}
    with pytest.raises(cv.Invalid) as exc_info:
        ct002_component._final_validate_dashboard_path(config, {"web_server": {}})
    assert "path: /astrameter" in str(exc_info.value)


def test_dashboard_on_its_own_path_coexists_with_the_web_server():
    config = {
        ct002_component.CONF_DASHBOARD: {ct002_component.CONF_PATH: "/astrameter"}
    }
    ct002_component._final_validate_dashboard_path(config, {"web_server": {}})


def test_dashboard_at_the_root_is_fine_without_the_web_server():
    config = {ct002_component.CONF_DASHBOARD: {ct002_component.CONF_PATH: ""}}
    ct002_component._final_validate_dashboard_path(config, {})


def test_astrameter_version_is_read_from_the_repo():
    # The page shows this on its Diagnostics tab; an unusual layout must show
    # nothing rather than something wrong.
    version = ct002_component._astrameter_version()
    assert version and version[0].isdigit()


def _dashboard_default(key: str):
    """The schema default for one dashboard option.

    Read off the schema rather than by validating it: validation resolves
    component ids and the target platform, neither of which exists outside a
    real codegen run.
    """
    for marker in ct002_component.DASHBOARD_OPTIONS_SCHEMA.schema:
        if str(marker) == key:
            return marker.default()
    raise AssertionError(f"{key} is not a dashboard option")


def test_dashboard_controls_are_off_by_default():
    # The page has no login of its own, so steering someone's batteries from
    # the LAN has to be asked for — matching DASHBOARD_ALLOW_WRITE on the
    # Python side, which also defaults to off outside the add-on.
    assert _dashboard_default(ct002_component.CONF_CONTROLS) is False


def test_dashboard_mounts_at_the_root_by_default():
    assert _dashboard_default(ct002_component.CONF_PATH) == "/"
