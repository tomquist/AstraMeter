"""Codegen unit tests for the ct002 ESPHome external component.

These tests import the schema validators from `esphome/components/ct002/__init__.py`
directly and exercise them without spinning up the full ESPHome codegen
pipeline. Skipped if ESPHome isn't installed, since the schema imports
`esphome.codegen` etc.; the YAML compile matrix in CI is the integration-level
guard.
"""

from __future__ import annotations

import contextlib
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


@contextlib.contextmanager
def _target_platform(platform):
    """Point CORE at *platform* for the duration, then put it back as it was.

    Restoring means *deleting* a key that was absent, not writing None over it:
    the code under test asks CORE what the target is, and a leftover None is a
    third answer neither branch expects.
    """
    from esphome.core import CORE

    core_data = CORE.data.setdefault(esphome.const.KEY_CORE, {})
    missing = object()
    previous = core_data.get(esphome.const.KEY_TARGET_PLATFORM, missing)
    core_data[esphome.const.KEY_TARGET_PLATFORM] = platform
    try:
        yield CORE
    finally:
        if previous is missing:
            core_data.pop(esphome.const.KEY_TARGET_PLATFORM, None)
        else:
            core_data[esphome.const.KEY_TARGET_PLATFORM] = previous


@pytest.fixture
def esp32_core():
    """CORE pointed at an ESP32, which is where the dashboard exists."""
    with _target_platform(esphome.const.PLATFORM_ESP32) as core:
        yield core


@pytest.fixture
def host_core():
    """CORE pointed at the host platform, which has no ESPHome web server."""
    with _target_platform(esphome.const.PLATFORM_HOST) as core:
        yield core


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


def test_dashboard_off_by_substitution_string(esp32_core):
    # `dashboard: ${enable_dashboard}` expands to a string, not a bool, so the
    # two spellings have to mean the same thing.
    for spelling in ("false", "False", "no", "off"):
        config = {"power_sensor_l1": "grid_l1", "dashboard": spelling}
        assert ct002_component._resolve_dashboard(config) == {
            "power_sensor_l1": "grid_l1"
        }, spelling


def test_dashboard_on_by_substitution_string(esp32_core):
    config = {"power_sensor_l1": "grid_l1", "dashboard": "true"}
    ct002_component._resolve_dashboard(config)
    assert ct002_component._dashboard_shorthand(config["dashboard"]) == {}


def test_dashboard_false_drops_the_key_entirely(esp32_core):
    # `dashboard: false` must leave nothing behind, so a disabled dashboard
    # pulls in no HTTP server and no page in flash.
    config = {"power_sensor_l1": "grid_l1", "dashboard": False}
    assert ct002_component._resolve_dashboard(config) == {"power_sensor_l1": "grid_l1"}


def test_dashboard_absent_means_on(esp32_core):
    # The whole point of opt-out: a configuration that never mentions the
    # dashboard still gets one.
    config = {"power_sensor_l1": "grid_l1"}
    assert ct002_component._resolve_dashboard(config) == {
        "power_sensor_l1": "grid_l1",
        "dashboard": {},
    }


def test_dashboard_absent_stays_absent_off_esp32(host_core):
    # ESPHome has no HTTP server for the other targets, so the default must
    # not conjure a block the schema would then reject.
    config = {"power_sensor_l1": "grid_l1"}
    assert ct002_component._resolve_dashboard(config) == {"power_sensor_l1": "grid_l1"}


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


def test_auto_load_pulls_the_web_server_only_for_a_resolved_dashboard():
    # A parameterized AUTO_LOAD runs *after* CONFIG_SCHEMA, so it is handed the
    # validated config — one _resolve_dashboard has already been through. There
    # the key is present exactly when a dashboard was asked for, which is the
    # only shape worth asserting: reading the raw spelling instead would treat
    # `dashboard: false` (deleted by then) as absent and load the server anyway.
    assert "web_server_base" in ct002_component.AUTO_LOAD({"dashboard": {}})
    assert "web_server_base" not in ct002_component.AUTO_LOAD({})


def test_auto_load_matches_resolve_dashboard_for_every_spelling(esp32_core):
    # Belt and braces on the above: run each spelling through the resolver the
    # way ESPHome does, and check AUTO_LOAD agrees with the outcome.
    for spelling, wants_server in (
        ({}, True),  # absent — the default
        ({"dashboard": None}, True),  # bare `dashboard:`
        ({"dashboard": True}, True),
        ({"dashboard": {"controls": True}}, True),
        ({"dashboard": False}, False),
        ({"dashboard": "false"}, False),  # a substitution expands to a string
    ):
        resolved = ct002_component._resolve_dashboard(dict(spelling))
        loaded = "web_server_base" in ct002_component.AUTO_LOAD(resolved)
        assert loaded is wants_server, f"{spelling} -> {resolved}"


def test_auto_load_leaves_the_web_server_out_off_esp32(host_core):
    assert "web_server_base" not in ct002_component.AUTO_LOAD(
        ct002_component._resolve_dashboard({})
    )


def test_auto_load_always_carries_the_sub_block_infrastructure(esp32_core):
    for component in ("socket", "json", "md5"):
        assert component in ct002_component.AUTO_LOAD({})


def test_dashboard_without_a_path_takes_the_root():
    # No `web_server:` in the configuration, so the root is the dashboard's.
    config = {ct002_component.CONF_DASHBOARD: {}}
    ct002_component._final_validate_dashboard_path(config, {})
    assert config[ct002_component.CONF_DASHBOARD][ct002_component.CONF_PATH] == ""


def test_dashboard_steps_aside_for_the_esphome_web_server():
    # A default-on dashboard must never be the reason an existing
    # `web_server:` build stops working, so it moves rather than collides.
    config = {ct002_component.CONF_DASHBOARD: {}}
    ct002_component._final_validate_dashboard_path(config, {"web_server": {}})
    assert (
        config[ct002_component.CONF_DASHBOARD][ct002_component.CONF_PATH]
        == ct002_component.DASHBOARD_ASIDE_PATH
    )


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
