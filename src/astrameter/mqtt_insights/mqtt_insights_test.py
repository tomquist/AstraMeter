"""Tests for MQTT Insights — discovery, service, and E2E with Mosquitto."""

from __future__ import annotations

import asyncio
import configparser
import contextlib
import json
import re
import time
from unittest.mock import AsyncMock

import aiomqtt

from astrameter.config.config_loader import (
    create_powermeter,
    read_mqtt_insights_config,
)
from astrameter.conftest import needs_mosquitto
from astrameter.powermeter.base import Powermeter
from astrameter.powermeter.wrappers.health import HealthTrackingPowermeter

from .discovery import (
    _sanitize_id,
    build_addon_device_discovery,
    build_ct002_consumer_discovery,
    build_ct002_device_discovery,
    build_powermeter_device_discovery,
    build_shelly_battery_discovery,
    build_shelly_device_discovery,
)
from .marstek_mqtt import MarstekMqttBinding
from .service import (
    POWERMETER_IDLE_THRESHOLD,
    MqttInsightsConfig,
    MqttInsightsService,
)

# ── Discovery payload unit tests ──────────────────────────────────────────

_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _assert_valid_node_id(node_id: str) -> None:
    assert _SAFE_ID_RE.match(node_id), f"Invalid node_id: {node_id!r}"


def _assert_discovery_structure(topic: str, payload: dict) -> None:
    """Validate HA Device Discovery required fields."""
    assert "device" in payload
    assert "identifiers" in payload["device"]
    assert "origin" in payload
    assert "name" in payload["origin"]
    assert "components" in payload
    for comp_key, comp in payload["components"].items():
        assert "platform" in comp, f"Missing platform in {comp_key}"
        assert "unique_id" in comp, f"Missing unique_id in {comp_key}"
        _assert_valid_node_id(_sanitize_id(comp["unique_id"]))


def test_ct002_consumer_discovery_structure():
    topic, payload = build_ct002_consumer_discovery(
        "astrameter", "dev1", "aabbccddeeff", "homeassistant", device_type="HMJ-2"
    )
    _assert_discovery_structure(topic, payload)

    assert "homeassistant/device/" in topic
    assert topic.endswith("/config")

    # Device info — AstraMeter branding
    dev = payload["device"]
    assert dev["identifiers"] == ["astrameter_consumer_aabbccddeeff"]
    assert dev["name"] == "AstraMeter Consumer HMJ-2 aabbccddeeff"
    assert dev["manufacturer"] == "Marstek"
    assert dev["model_id"] == "HMJ-2"
    # The consumer device advertises no connections — the battery MAC is never
    # exposed (it would merge with the hm2mqtt battery device; issue #438); the
    # meter link is carried by via_device.
    assert "connections" not in dev
    assert dev["via_device"] == "astrameter_ct002_dev1"

    # Check two-level availability
    assert payload["availability_mode"] == "all"
    assert len(payload["availability"]) == 2

    # Check key components exist
    comps = payload["components"]
    assert "grid_power_total" in comps
    assert "active" in comps
    assert "saturation" in comps
    assert "phase" in comps
    assert "battery_ip" in comps
    assert "ct_type" in comps
    assert "ct_mac" in comps
    assert "last_seen" in comps
    assert "poll_interval" in comps

    # Poll interval sensor
    poll = comps["poll_interval"]
    assert poll["platform"] == "sensor"
    assert poll["device_class"] == "duration"
    assert poll["unit_of_measurement"] == "s"
    assert poll["entity_category"] == "diagnostic"

    # Primary entity has name: null
    assert comps["grid_power_total"]["name"] is None

    # Power sensors carry state_class measurement so they are usable as
    # power source entities in the Home Assistant energy dashboard.
    for power_key in (
        "grid_power_total",
        "grid_power_l1",
        "grid_power_l2",
        "grid_power_l3",
        "target_l1",
        "target_l2",
        "target_l3",
        "reported_power",
        "last_target",
    ):
        assert comps[power_key]["device_class"] == "power"
        assert comps[power_key]["state_class"] == "measurement"

    # Switch has correct topics — each control uses its own retained command
    # sub-topic so Home Assistant persists the value across restarts.
    switch = comps["active"]
    assert switch["platform"] == "switch"
    assert switch["command_topic"].endswith("/active/set")
    assert switch["retain"] is True
    assert switch["payload_on"] == "true"
    assert switch["payload_off"] == "false"
    assert switch["state_on"] == "True"
    assert switch["state_off"] == "False"

    # Manual target number entity
    manual = comps["manual_target"]
    assert manual["platform"] == "number"
    assert manual["device_class"] == "power"
    assert manual["mode"] == "box"
    assert manual["command_topic"].endswith("/manual_target/set")
    assert manual["retain"] is True
    assert "command_template" not in manual
    assert manual["entity_category"] == "config"

    # Auto target switch entity
    auto = comps["auto_target"]
    assert auto["platform"] == "switch"
    assert auto["command_topic"].endswith("/auto_target/set")
    assert auto["retain"] is True
    assert auto["state_on"] == "True"
    assert auto["state_off"] == "False"
    assert auto["entity_category"] == "config"

    # Distribution weight number entity
    weight = comps["distribution_weight"]
    assert weight["platform"] == "number"
    assert weight["command_topic"].endswith("/distribution_weight/set")
    assert weight["retain"] is True
    assert weight["min"] == 0
    assert weight["max"] == 10
    assert weight["entity_category"] == "config"

    # Efficiency Window Weight is gated on efficiency rotation (covered by its
    # own test); the default structure here has rotation off, so it's absent.
    assert "efficiency_window_weight" not in comps

    # Min DC Output number entity — present for the B2500 family (HMJ-2).
    min_dc = comps["min_dc_output"]
    assert min_dc["platform"] == "number"
    assert min_dc["command_topic"].endswith("/min_dc_output/set")
    assert min_dc["retain"] is True
    assert min_dc["min"] == 0
    assert min_dc["max"] == 1000
    assert min_dc["unit_of_measurement"] == "W"
    assert min_dc["entity_category"] == "config"


def test_min_dc_output_entity_only_for_external_inverter_types():
    """The Min DC Output number is gated on the device-capabilities classifier."""

    def _components(device_type: str) -> dict:
        _, payload = build_ct002_consumer_discovery(
            "astrameter",
            "dev1",
            "aabbccddeeff",
            "homeassistant",
            device_type=device_type,
        )
        return payload["components"]

    # External-inverter DC families get the entity.
    for dt in ("HMJ-1", "HMA-2", "HMK-1"):
        assert "min_dc_output" in _components(dt), dt
    # Venus / Jupiter / unknown do not.
    for dt in ("HMG-50", "VNSD", "HMN-1", "UNKNOWN", ""):
        assert "min_dc_output" not in _components(dt), dt


def test_ct002_consumer_discovery_no_device_type():
    """Name omits device_type when empty."""
    _, payload = build_ct002_consumer_discovery(
        "astrameter", "dev1", "aabbccddeeff", "homeassistant"
    )
    dev = payload["device"]
    assert dev["name"] == "AstraMeter Consumer aabbccddeeff"
    assert "model_id" not in dev


def test_ct002_consumer_discovery_non_mac_consumer():
    """Non-MAC consumer_id has no connections but is still linked via via_device."""
    _, payload = build_ct002_consumer_discovery(
        "astrameter", "dev1", "192.168.1.1:12345", "homeassistant"
    )
    assert "connections" not in payload["device"]
    assert payload["device"]["via_device"] == "astrameter_ct002_dev1"


def test_ct002_consumer_discovery_emits_no_connections_issue_438():
    """The consumer device advertises NO ``connections`` at all.

    Advertising the battery's own MAC (bluetooth or mac) would make HA merge
    this standalone "AstraMeter Consumer" device into the battery device owned
    by another bridge (e.g. hm2mqtt, which publishes ``["bluetooth", MAC]`` for
    the same battery), non-deterministically depending on MQTT registration
    order. The device is identified solely by its own namespaced
    ``identifiers`` and linked to the meter via ``via_device``. See #438.
    """
    _, payload = build_ct002_consumer_discovery(
        "astrameter",
        "dev1",
        "aabbccddeeff",  # a 12-hex MAC consumer_id — must NOT become a connection
        "homeassistant",
        device_type="HMJ-2",
    )
    dev = payload["device"]
    assert "connections" not in dev
    assert dev["identifiers"] == ["astrameter_consumer_aabbccddeeff"]
    assert dev["via_device"] == "astrameter_ct002_dev1"


def test_ct002_device_discovery_structure():
    topic, payload = build_ct002_device_discovery(
        "astrameter",
        "dev1",
        "homeassistant",
        addon_slug="34dea19a_astrameter",
        efficiency_rotation=True,
    )
    _assert_discovery_structure(topic, payload)
    assert "AstraMeter" in payload["device"]["name"]
    assert payload["device"]["via_device"] == "34dea19a_astrameter"
    comps = payload["components"]
    assert "smooth_target" in comps
    assert "active_control" in comps
    assert "consumer_count" in comps
    assert comps["smooth_target"]["name"] is None  # primary
    assert comps["smooth_target"]["device_class"] == "power"
    assert comps["smooth_target"]["state_class"] == "measurement"

    # Active Control switch — controllable, retained command for restart restore
    ac = comps["active_control"]
    assert ac["platform"] == "switch"
    assert ac["command_topic"] == "astrameter/ct002/dev1/set"
    assert ac["value_template"] == "{{ value_json.active_control }}"
    assert ac["state_on"] == "True"
    assert ac["state_off"] == "False"
    assert ac["retain"] is True
    assert ac["entity_category"] == "config"

    # Force rotation button
    btn = comps["force_rotation"]
    assert btn["platform"] == "button"
    assert "command_topic" in btn
    assert "payload_press" in btn
    assert btn["entity_category"] == "config"


def test_ct002_device_discovery_omits_force_rotation_without_efficiency():
    """The Force Rotation button is only surfaced when efficiency rotation is
    enabled (the default omits it — there's nothing to rotate)."""
    _, default_payload = build_ct002_device_discovery(
        "astrameter", "dev1", "homeassistant"
    )
    assert "force_rotation" not in default_payload["components"]
    # The remaining device entities are unaffected.
    assert "smooth_target" in default_payload["components"]
    assert "active_control" in default_payload["components"]
    assert "consumer_count" in default_payload["components"]

    _, enabled_payload = build_ct002_device_discovery(
        "astrameter", "dev1", "homeassistant", efficiency_rotation=True
    )
    assert "force_rotation" in enabled_payload["components"]


def test_ct002_consumer_discovery_gates_efficiency_window_weight():
    """The Efficiency Window Weight number is only surfaced when efficiency
    rotation is enabled (the default omits it — every battery stays active)."""
    _, default_payload = build_ct002_consumer_discovery(
        "astrameter", "dev1", "aabbccddeeff", "homeassistant", device_type="HMJ-2"
    )
    comps = default_payload["components"]
    assert "efficiency_window_weight" not in comps
    # The remaining per-consumer entities are unaffected.
    assert "distribution_weight" in comps
    assert "min_dc_output" in comps

    _, enabled_payload = build_ct002_consumer_discovery(
        "astrameter",
        "dev1",
        "aabbccddeeff",
        "homeassistant",
        device_type="HMJ-2",
        efficiency_rotation=True,
    )
    eww = enabled_payload["components"]["efficiency_window_weight"]
    assert eww["platform"] == "number"
    assert eww["command_topic"].endswith("/efficiency_window_weight/set")
    assert eww["retain"] is True
    assert eww["unit_of_measurement"] == "%"
    assert eww["min"] == 0
    assert eww["max"] == 100
    assert eww["entity_category"] == "config"
    assert "* 100" in eww["value_template"]


def test_shelly_battery_discovery_structure():
    topic, payload = build_shelly_battery_discovery(
        "astrameter", "shelly1", "192.168.1.100", "homeassistant"
    )
    _assert_discovery_structure(topic, payload)
    assert "AstraMeter" in payload["device"]["name"]
    assert payload["device"]["via_device"] == "astrameter_shelly_shelly1"
    comps = payload["components"]
    assert "grid_power_total" in comps
    assert "active" in comps
    assert "last_seen" in comps
    assert "poll_interval" in comps
    for power_key in (
        "grid_power_total",
        "grid_power_l1",
        "grid_power_l2",
        "grid_power_l3",
    ):
        assert comps[power_key]["device_class"] == "power"
        assert comps[power_key]["state_class"] == "measurement"
    poll = comps["poll_interval"]
    assert poll["device_class"] == "duration"
    assert poll["unit_of_measurement"] == "s"
    assert payload["availability_mode"] == "all"
    assert len(payload["availability"]) == 2


def test_shelly_device_discovery_structure():
    topic, payload = build_shelly_device_discovery(
        "astrameter", "shelly1", "homeassistant", addon_slug="34dea19a_astrameter"
    )
    _assert_discovery_structure(topic, payload)
    assert "AstraMeter" in payload["device"]["name"]
    assert payload["device"]["via_device"] == "34dea19a_astrameter"
    assert "battery_count" in payload["components"]


def test_powermeter_device_discovery_structure():
    topic, payload = build_powermeter_device_discovery(
        "astrameter",
        "MQTT_1",
        "MQTT_1",
        "homeassistant",
        addon_slug="34dea19a_astrameter",
    )
    _assert_discovery_structure(topic, payload)
    assert topic == "homeassistant/device/astrameter_powermeter_MQTT_1/config"
    assert payload["device"]["identifiers"] == "astrameter_powermeter_MQTT_1"
    # Section name is Capital-Cased for the display label.
    assert payload["device"]["name"] == "AstraMeter Powermeter Mqtt 1"
    # Links the powermeter device under the AstraMeter hub device.
    assert payload["device"]["via_device"] == "34dea19a_astrameter"
    assert payload["state_topic"] == "astrameter/powermeter/MQTT_1"
    assert len(payload["availability"]) == 1

    comps = payload["components"]
    online = comps["online"]
    assert online["platform"] == "binary_sensor"
    assert online["device_class"] == "connectivity"
    assert online["payload_on"] == "True"
    assert online["payload_off"] == "False"
    assert online["entity_category"] == "diagnostic"
    assert online["value_template"] == "{{ value_json.online }}"

    # Latest-readings sensors: per phase + total, total is primary (name=None).
    for power_key in (
        "grid_power_total",
        "grid_power_l1",
        "grid_power_l2",
        "grid_power_l3",
    ):
        assert comps[power_key]["platform"] == "sensor"
        assert comps[power_key]["device_class"] == "power"
        assert comps[power_key]["state_class"] == "measurement"
        assert comps[power_key]["unit_of_measurement"] == "W"
    assert comps["grid_power_total"]["name"] is None
    assert comps["grid_power_l1"]["name"] == "Power L1"
    assert "value_json.grid_power.total" in comps["grid_power_total"]["value_template"]


def test_powermeter_device_discovery_capital_cases_multiword_section():
    _, payload = build_powermeter_device_discovery(
        "astrameter", "SMA_ENERGY_METER", "SMA_ENERGY_METER", "homeassistant"
    )
    assert payload["device"]["name"] == "AstraMeter Powermeter Sma Energy Meter"


def test_powermeter_device_discovery_omits_via_device_without_addon_slug():
    _, payload = build_powermeter_device_discovery(
        "astrameter", "HOMEWIZARD", "HOMEWIZARD", "homeassistant"
    )
    assert "via_device" not in payload["device"]


def test_meter_device_discovery_omits_via_device_without_addon_slug():
    _, ct002 = build_ct002_device_discovery("astrameter", "dev1", "homeassistant")
    assert "via_device" not in ct002["device"]


def test_addon_device_discovery_structure():
    topic, payload = build_addon_device_discovery(
        "astrameter", "34dea19a_astrameter", "homeassistant"
    )
    _assert_discovery_structure(topic, payload)
    assert "homeassistant/device/" in topic
    assert topic.endswith("/config")
    # identifiers == addon_slug so the meter devices' via_device resolves here.
    assert payload["device"]["identifiers"] == "34dea19a_astrameter"
    assert payload["device"]["name"] == "AstraMeter"

    comps = payload["components"]
    assert set(comps) == {"status", "version", "consumer_count"}

    # Connectivity status sensor reads the system LWT and must NOT carry an
    # availability block, or it would go unavailable instead of showing "off".
    status = comps["status"]
    assert status["platform"] == "binary_sensor"
    assert status["device_class"] == "connectivity"
    assert status["state_topic"] == "astrameter/status"
    assert status["payload_on"] == "online"
    assert status["payload_off"] == "offline"
    assert "availability" not in status

    # Diagnostics are fed from the retained bridge topic and grey out offline.
    for key, tmpl in (
        ("version", "{{ value_json.version }}"),
        ("consumer_count", "{{ value_json.consumer_count }}"),
    ):
        comp = comps[key]
        assert comp["state_topic"] == "astrameter/bridge"
        assert comp["value_template"] == tmpl
        assert comp["entity_category"] == "diagnostic"
        assert comp["availability"] == [
            {
                "topic": "astrameter/status",
                "payload_available": "online",
                "payload_not_available": "offline",
            }
        ]


def test_addon_device_discovery_links_meter_via_device():
    """The hub's identifiers must equal the slug used as meter via_device."""
    _, hub = build_addon_device_discovery(
        "astrameter", "abc123_astrameter", "homeassistant"
    )
    _, ct002 = build_ct002_device_discovery(
        "astrameter", "dev1", "homeassistant", addon_slug="abc123_astrameter"
    )
    assert ct002["device"]["via_device"] == hub["device"]["identifiers"]
    _, shelly = build_shelly_device_discovery("astrameter", "shelly1", "homeassistant")
    assert "via_device" not in shelly["device"]


def test_unique_ids_are_unique():
    """All unique_ids within a single discovery payload must be distinct."""
    _, payload = build_ct002_consumer_discovery(
        "astrameter", "dev1", "cons1", "homeassistant"
    )
    uids = [c["unique_id"] for c in payload["components"].values()]
    assert len(uids) == len(set(uids))


def test_sanitize_id():
    assert _sanitize_id("192.168.1.100") == "192_168_1_100"
    assert _sanitize_id("AA:BB:CC") == "AA_BB_CC"
    assert _sanitize_id("normal-id_123") == "normal-id_123"


# ── Config tests ──────────────────────────────────────────────────────────


def test_config_guard_mqtt_vs_mqtt_insights():
    """[MQTT] creates a powermeter, [MQTT_INSIGHTS] does not."""
    cfg = configparser.ConfigParser()
    cfg.read_string(
        """
[MQTT]
BROKER = localhost
PORT = 1883
TOPIC = test/power

[MQTT_INSIGHTS]
BROKER = localhost
PORT = 1883
"""
    )
    # MQTT section should create a powermeter
    pm = create_powermeter("MQTT", cfg)
    assert pm is not None

    # MQTT_INSIGHTS should NOT create a powermeter
    pm2 = create_powermeter("MQTT_INSIGHTS", cfg)
    assert pm2 is None


def test_read_mqtt_insights_config_present():
    cfg = configparser.ConfigParser()
    cfg.read_string(
        """
[MQTT_INSIGHTS]
BROKER = 10.0.0.1
PORT = 8883
USERNAME = user
PASSWORD = pass
TLS = true
BASE_TOPIC = my_topic
HA_DISCOVERY = true
HA_DISCOVERY_PREFIX = ha
ADDON_SLUG = 34dea19a_astrameter
"""
    )
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.broker == "10.0.0.1"
    assert result.port == 8883
    assert result.username == "user"
    assert result.password == "pass"
    assert result.tls is True
    assert result.base_topic == "my_topic"
    assert result.ha_discovery is True
    assert result.ha_discovery_prefix == "ha"
    assert result.addon_slug == "34dea19a_astrameter"


def test_read_mqtt_insights_config_defaults():
    cfg = configparser.ConfigParser()
    cfg.read_string(
        """
[MQTT_INSIGHTS]
BROKER = localhost
"""
    )
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.port == 1883
    assert result.tls is False
    assert result.base_topic == "astrameter"
    assert result.ha_discovery is True
    assert result.ha_discovery_prefix == "homeassistant"
    assert result.addon_slug is None


def test_read_mqtt_insights_config_empty_values():
    cfg = configparser.ConfigParser()
    cfg.read_string(
        """
[MQTT_INSIGHTS]
BROKER =
PORT =
USERNAME =
PASSWORD =
TLS =
BASE_TOPIC =
HA_DISCOVERY =
HA_DISCOVERY_PREFIX =
ADDON_SLUG =
"""
    )
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.broker == "localhost"
    assert result.port == 1883
    assert result.username is None
    assert result.password is None
    assert result.tls is False
    assert result.base_topic == "astrameter"
    assert result.ha_discovery is True
    assert result.ha_discovery_prefix == "homeassistant"
    assert result.addon_slug is None


def test_read_mqtt_insights_config_whitespace_addon_slug():
    """Whitespace-only ADDON_SLUG values must be normalised to None."""
    cfg = configparser.ConfigParser()
    cfg.add_section("MQTT_INSIGHTS")
    cfg.set("MQTT_INSIGHTS", "BROKER", "localhost")
    cfg.set("MQTT_INSIGHTS", "ADDON_SLUG", "   ")
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.addon_slug is None


def test_read_mqtt_insights_config_absent():
    cfg = configparser.ConfigParser()
    cfg.read_string("[GENERAL]\nDEVICE_TYPE=ct002\n")
    assert read_mqtt_insights_config(cfg) is None


def test_read_mqtt_insights_config_marstek_mqtt_default_true():
    cfg = configparser.ConfigParser()
    cfg.read_string("[MQTT_INSIGHTS]\nBROKER = localhost\n")
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.marstek_mqtt_enabled is True


def test_read_mqtt_insights_config_marstek_mqtt_opt_out():
    cfg = configparser.ConfigParser()
    cfg.read_string(
        "[MQTT_INSIGHTS]\nBROKER = localhost\nMARSTEK_MQTT_ENABLED = false\n"
    )
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.marstek_mqtt_enabled is False


def test_read_mqtt_insights_config_marstek_mqtt_interval_default():
    cfg = configparser.ConfigParser()
    cfg.read_string("[MQTT_INSIGHTS]\nBROKER = localhost\n")
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.marstek_mqtt_interval == 300


def test_read_mqtt_insights_config_marstek_mqtt_interval_custom():
    cfg = configparser.ConfigParser()
    cfg.read_string("[MQTT_INSIGHTS]\nBROKER = localhost\nMARSTEK_MQTT_INTERVAL = 60\n")
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.marstek_mqtt_interval == 60


def test_read_mqtt_insights_config_marstek_mqtt_interval_zero():
    cfg = configparser.ConfigParser()
    cfg.read_string("[MQTT_INSIGHTS]\nBROKER = localhost\nMARSTEK_MQTT_INTERVAL = 0\n")
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.marstek_mqtt_interval == 0


def test_read_mqtt_insights_config_powermeter_health_interval_default():
    cfg = configparser.ConfigParser()
    cfg.read_string("[MQTT_INSIGHTS]\nBROKER = localhost\n")
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.powermeter_health_interval == 30.0


def test_read_mqtt_insights_config_powermeter_health_interval_custom():
    cfg = configparser.ConfigParser()
    cfg.read_string(
        "[MQTT_INSIGHTS]\nBROKER = localhost\nPOWERMETER_HEALTH_INTERVAL = 15\n"
    )
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.powermeter_health_interval == 15.0


def test_read_mqtt_insights_config_powermeter_health_interval_zero():
    cfg = configparser.ConfigParser()
    cfg.read_string(
        "[MQTT_INSIGHTS]\nBROKER = localhost\nPOWERMETER_HEALTH_INTERVAL = 0\n"
    )
    result = read_mqtt_insights_config(cfg)
    assert result is not None
    assert result.powermeter_health_interval == 0


# ── Service unit tests (no broker) ───────────────────────────────────────


def test_queue_overflow_does_not_raise():
    """Overflowing the queue should not raise."""
    service = MqttInsightsService(MqttInsightsConfig(broker="localhost"))
    for i in range(200):
        service.on_ct002_response("dev1", f"consumer{i}", {"grid_power": {}})
    # No exception raised


# ── Powermeter health loop (no broker) ───────────────────────────────────


class _PushMeter(Powermeter):
    def __init__(self, online: bool | None, values: list[float] | None = None) -> None:
        self._online = online
        self._values = values if values is not None else [10.0, 20.0, 30.0]

    def stream_online(self) -> bool | None:
        return self._online

    async def get_powermeter_watts(self) -> list[float]:
        if self._values is None:
            raise ValueError("no data")
        return self._values


class _PullMeter(Powermeter):
    def __init__(self, values: list[float] | None = None, raises: bool = False) -> None:
        self._values = values if values is not None else [100.0]
        self._raises = raises
        self.probes = 0

    async def get_powermeter_watts(self) -> list[float]:
        self.probes += 1
        if self._raises:
            raise ValueError("boom")
        return self._values


def _health_service() -> MqttInsightsService:
    return MqttInsightsService(MqttInsightsConfig(broker="localhost"))


async def test_powermeter_status_push_reports_stream_state_and_readings():
    service = _health_service()
    online, values = await service._powermeter_status(_PushMeter(True, [1.0, 2.0, 3.0]))
    assert online is True
    assert values == [1.0, 2.0, 3.0]
    online, _ = await service._powermeter_status(_PushMeter(False))
    assert online is False


async def test_powermeter_status_push_exception_reports_offline():
    """A meter whose stream_online() raises must report offline, not crash the
    health loop (which would tear down the gather and force a reconnect)."""

    class _BrokenPushMeter(Powermeter):
        def stream_online(self) -> bool | None:
            raise RuntimeError("boom")

        async def get_powermeter_watts(self) -> list[float]:
            raise AssertionError("must not be probed after stream_online raised")

    service = _health_service()
    online, values = await service._powermeter_status(_BrokenPushMeter())
    assert online is False
    assert values is None


async def test_powermeter_status_pull_reuses_recent_control_read():
    """A pull meter read by the control loop within the idle window is reused
    without issuing a probe."""
    inner = _PullMeter([42.0])
    pm = HealthTrackingPowermeter(inner, name="SCRIPT_1")
    await pm.get_powermeter_watts()  # control-loop read: ok, recent
    assert inner.probes == 1

    service = _health_service()
    online, values = await service._powermeter_status(pm)
    assert online is True
    assert values == [42.0]
    assert inner.probes == 1  # reused, no extra probe


async def test_powermeter_status_pull_reuses_recent_failure():
    inner = _PullMeter(raises=True)
    pm = HealthTrackingPowermeter(inner, name="SCRIPT_1")
    with contextlib.suppress(ValueError):
        await pm.get_powermeter_watts()
    probes_after_control = inner.probes

    service = _health_service()
    online, values = await service._powermeter_status(pm)
    assert online is False
    assert values is None  # failed read cached no values
    assert inner.probes == probes_after_control  # reused failure, no probe


async def test_powermeter_status_idle_pull_is_probed_once():
    inner = _PullMeter([7.0])
    pm = HealthTrackingPowermeter(inner, name="SCRIPT_1")
    # No control-loop read recorded -> idle -> probe.
    service = _health_service()
    online, values = await service._powermeter_status(pm)
    assert online is True
    assert values == [7.0]
    assert inner.probes == 1


async def test_powermeter_status_idle_pull_probe_failure_is_offline():
    inner = _PullMeter(raises=True)
    pm = HealthTrackingPowermeter(inner, name="SCRIPT_1")
    service = _health_service()
    online, values = await service._powermeter_status(pm)
    assert online is False
    assert values is None
    assert inner.probes == 1


async def test_powermeter_status_stale_control_read_falls_back_to_probe():
    inner = _PullMeter([5.0])
    pm = HealthTrackingPowermeter(inner, name="SCRIPT_1")
    await pm.get_powermeter_watts()
    # Age the recorded attempt well past the idle window.
    pm._last_attempt = time.monotonic() - (POWERMETER_IDLE_THRESHOLD + 10)

    service = _health_service()
    online, values = await service._powermeter_status(pm)
    assert online is True
    assert values == [5.0]
    assert inner.probes == 2  # one control read + one fallback probe


def test_grid_power_payload_phase_counts():
    assert MqttInsightsService._grid_power_payload([1.0, 2.0, 3.0]) == {
        "l1": 1.0,
        "l2": 2.0,
        "l3": 3.0,
        "total": 6.0,
    }
    assert MqttInsightsService._grid_power_payload([100.0]) == {
        "l1": 100.0,
        "l2": None,
        "l3": None,
        "total": 100.0,
    }
    assert MqttInsightsService._grid_power_payload(None) == {
        "l1": None,
        "l2": None,
        "l3": None,
        "total": None,
    }


async def test_publish_powermeter_health_state_and_discovery_once():
    service = MqttInsightsService(
        MqttInsightsConfig(
            broker="localhost", base_topic="am", ha_discovery_prefix="ha"
        )
    )
    cfg = service._config
    client = AsyncMock()

    await service._publish_powermeter_health(
        client, "am", cfg, "MQTT_1", True, [1.0, 2.0, 3.0]
    )

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert "am/powermeter/MQTT_1" in topics
    assert "ha/device/astrameter_powermeter_MQTT_1/config" in topics
    state_call = next(
        c for c in client.publish.call_args_list if c.args[0] == "am/powermeter/MQTT_1"
    )
    assert json.loads(state_call.kwargs["payload"]) == {
        "online": True,
        "grid_power": {"l1": 1.0, "l2": 2.0, "l3": 3.0, "total": 6.0},
    }
    assert state_call.kwargs["retain"] is True

    # Second publish: state only, discovery is not repeated.
    client.publish.reset_mock()
    await service._publish_powermeter_health(client, "am", cfg, "MQTT_1", False, None)
    topics2 = [c.args[0] for c in client.publish.call_args_list]
    assert topics2 == ["am/powermeter/MQTT_1"]


def test_hub_identifier_uses_addon_slug_when_set():
    svc = MqttInsightsService(
        MqttInsightsConfig(
            broker="localhost", base_topic="am", addon_slug="34dea19a_astrameter"
        )
    )
    assert svc._hub_identifier() == "34dea19a_astrameter"


def test_hub_identifier_falls_back_to_base_topic_without_addon_slug():
    svc = MqttInsightsService(
        MqttInsightsConfig(broker="localhost", base_topic="astra")
    )
    assert svc._hub_identifier() == "astrameter_astra"


async def test_publish_powermeter_health_links_hub_via_fallback():
    """Without ADDON_SLUG the powermeter device still links to the AstraMeter
    hub via the base-topic fallback identifier (standalone/Docker)."""
    service = MqttInsightsService(
        MqttInsightsConfig(
            broker="localhost", base_topic="am", ha_discovery_prefix="ha"
        )
    )
    client = AsyncMock()
    await service._publish_powermeter_health(
        client, "am", service._config, "MQTT_1", True, [1.0]
    )
    disc = next(
        c for c in client.publish.call_args_list if c.args[0].endswith("/config")
    )
    payload = json.loads(disc.kwargs["payload"])
    assert payload["device"]["via_device"] == "astrameter_am"


# ── E2E helpers ──────────────────────────────────────────────────────────


async def _collect_messages(sub, target, *, timeout=5, stop=None):
    """Collect messages from *sub* into *target* list.

    *stop* is an optional callable(msg) → bool that ends collection early.
    Falls back to collecting a single message when *stop* is None.
    Compatible with Python 3.10 (no asyncio.timeout).
    """

    async def _inner():
        async for msg in sub.messages:
            target.append(msg)
            if stop is not None:
                if stop(msg):
                    return
            else:
                return  # single message

    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(_inner(), timeout=timeout)


async def _poll(predicate, *, timeout=5, interval=0.05):
    """Poll *predicate* until it returns True, or raise on timeout."""

    async def _inner():
        while not predicate():
            await asyncio.sleep(interval)

    await asyncio.wait_for(_inner(), timeout=timeout)


# ── E2E tests with Mosquitto ─────────────────────────────────────────────


_test_counter = 0


def _make_service(
    port: int, base_topic: str | None = None, addon_slug: str | None = None
) -> MqttInsightsService:
    global _test_counter
    _test_counter += 1
    if base_topic is None:
        base_topic = f"test_insights_{_test_counter}"
    return MqttInsightsService(
        MqttInsightsConfig(
            broker="127.0.0.1",
            port=port,
            base_topic=base_topic,
            ha_discovery=True,
            ha_discovery_prefix=f"ha_disc_{_test_counter}",
            addon_slug=addon_slug,
            # Broader E2E tests assert poll-only Marstek behaviour; periodic traffic
            # is covered by test_marstek_periodic_broadcast.
            marstek_mqtt_interval=0.0,
        )
    )


SAMPLE_CT002_DATA = {
    "grid_power": {"l1": 100.0, "l2": 200.0, "l3": 300.0, "total": 600.0},
    "target": {"l1": 50.0, "l2": 100.0, "l3": 150.0},
    "phase": "A",
    "reported_power": 42,
    "device_type": "HMG-50",
    "battery_ip": "192.168.1.10",
    "ct_type": "HME-4",
    "ct_mac": "AA:BB:CC:DD:EE:FF",
    "saturation": 0.5,
    "last_target": 300.0,
    "active": True,
    "poll_interval": 5.0,
    "last_seen": "2026-01-01T00:00:00+00:00",
    "manual_target": None,
    "auto_target": True,
    "distribution_weight": 1.5,
    "efficiency_window_weight": 0.5,
    "min_dc_output": 25.0,
    "smooth_target": 500.0,
    "active_control": True,
    "consumer_count": 2,
}

SAMPLE_SHELLY_DATA = {
    "grid_power": {"l1": 100.0, "l2": 200.0, "l3": 300.0, "total": 600.0},
    "active": True,
    "poll_interval": 5.0,
    "last_seen": "2026-01-01T00:00:00+00:00",
    "battery_count": 1,
}


@needs_mosquitto
async def test_publishes_state_on_ct002_event(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    await service.start()

    try:
        await service.wait_connected()

        received = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(f"{base}/ct002/+/consumer/+")
            service.on_ct002_response("dev1", "consumer1", SAMPLE_CT002_DATA)
            await _collect_messages(sub, received)

        assert len(received) == 1
        payload = json.loads(received[0].payload)
        assert payload["grid_power"]["total"] == 600.0
        assert payload["phase"] == "A"
        assert payload["battery_ip"] == "192.168.1.10"
        assert payload["ct_type"] == "HME-4"
        assert payload["ct_mac"] == "AA:BB:CC:DD:EE:FF"
        assert payload["active"] is True
        assert payload["poll_interval"] == 5.0
        assert payload["distribution_weight"] == 1.5
        assert payload["efficiency_window_weight"] == 0.5
        assert payload["min_dc_output"] == 25.0
        assert str(received[0].topic) == f"{base}/ct002/dev1/consumer/consumer1"
    finally:
        await service.stop()


@needs_mosquitto
async def test_publishes_device_status(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    await service.start()

    try:
        await service.wait_connected()

        received = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(f"{base}/ct002/+/status")
            service.on_ct002_response("dev1", "consumer1", SAMPLE_CT002_DATA)
            await _collect_messages(sub, received)

        assert len(received) == 1
        payload = json.loads(received[0].payload)
        assert payload["smooth_target"] == 500.0
        assert payload["active_control"] is True
        assert payload["consumer_count"] == 2
    finally:
        await service.stop()


@needs_mosquitto
async def test_publishes_ha_discovery_on_first_event(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    ha_prefix = service._config.ha_discovery_prefix
    await service.start()

    try:
        await service.wait_connected()

        discovery_msgs = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(f"{ha_prefix}/device/#")
            # First event for consumer1
            service.on_ct002_response("dev1", "consumer1", SAMPLE_CT002_DATA)
            # Second event for same consumer — should NOT trigger another discovery
            await _poll(lambda: "dev1/consumer1" in service._discovered_ct002_consumers)
            service.on_ct002_response("dev1", "consumer1", SAMPLE_CT002_DATA)
            # Third event for consumer2 — SHOULD trigger new discovery
            service.on_ct002_response("dev1", "consumer2", SAMPLE_CT002_DATA)

            await _collect_messages(
                sub,
                discovery_msgs,
                timeout=3,
                stop=lambda _: len(discovery_msgs) >= 4,
            )

        # Expect: AstraMeter hub device (retained, now always published on
        # connect with a base-topic fallback id) + CT002 device discovery +
        # consumer1 + consumer2 = 4 (no duplicate for the second consumer1 event)
        assert len(discovery_msgs) == 4
        topics = [str(m.topic) for m in discovery_msgs]
        # Hub device discovery (retained, delivered on subscribe)
        assert any("astrameter_addon_" in t for t in topics)
        # Device-level discovery
        assert any("astrameter_ct002_dev1/config" in t for t in topics)
        # Consumer-level discoveries
        assert any("consumer1" in t for t in topics)
        assert any("consumer2" in t for t in topics)
    finally:
        await service.stop()


@needs_mosquitto
async def test_publishes_addon_hub_device_and_bridge(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port, addon_slug="abc123_astrameter")
    cfg = service._config
    ha_prefix = cfg.ha_discovery_prefix
    base = cfg.base_topic
    await service.start()

    try:
        await service.wait_connected()

        # Hub discovery + bridge are published (retained) on connect, so a
        # late subscriber still receives them.
        hub_topic = f"{ha_prefix}/device/astrameter_addon_abc123_astrameter/config"
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(hub_topic)
            await sub.subscribe(f"{base}/bridge")

            hub_payload: dict = {}
            bridge_payloads: list[dict] = []

            async def _collect() -> None:
                async for m in sub.messages:
                    if str(m.topic) == hub_topic:
                        hub_payload.update(json.loads(m.payload))
                    else:
                        bridge_payloads.append(json.loads(m.payload))
                    # Fire a consumer event once we've seen the initial state,
                    # then wait for the updated bridge count.
                    if (
                        len(bridge_payloads) == 1
                        and not service._discovered_ct002_consumers
                    ):
                        service.on_ct002_response(
                            "dev1", "consumer1", SAMPLE_CT002_DATA
                        )
                    if hub_payload and any(
                        p.get("consumer_count") == 1 for p in bridge_payloads
                    ):
                        return

            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(_collect(), timeout=5)

        # Hub device: identifiers == slug (so meter via_device resolves here).
        assert hub_payload["device"]["identifiers"] == "abc123_astrameter"
        assert hub_payload["device"]["name"] == "AstraMeter"
        # Bridge state: initial count 0, then 1 after the consumer event.
        assert bridge_payloads[0]["consumer_count"] == 0
        assert bridge_payloads[-1]["consumer_count"] == 1
        assert all("version" in p for p in bridge_payloads)
    finally:
        await service.stop()


@needs_mosquitto
async def test_active_toggle_via_mqtt(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    handler_calls = []

    def mock_handler(consumer_id, active):
        handler_calls.append((consumer_id, active))

    service.register_active_handler("dev1", mock_handler)
    await service.start()

    try:
        await service.wait_connected()

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(
                f"{base}/ct002/dev1/consumer/consumer1/active/set",
                payload=b"false",
            )
            await _poll(lambda: len(handler_calls) >= 1)
            await pub.publish(
                f"{base}/ct002/dev1/consumer/consumer1/active/set",
                payload=b"true",
            )
            await _poll(lambda: len(handler_calls) >= 2)

        assert len(handler_calls) == 2
        assert handler_calls[0] == ("consumer1", False)
        assert handler_calls[1] == ("consumer1", True)
    finally:
        await service.stop()


@needs_mosquitto
async def test_consumer_removal_publishes_offline(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    await service.start()

    try:
        await service.wait_connected()

        # First fire an event so the consumer is "discovered"
        service.on_ct002_response("dev1", "consumer1", SAMPLE_CT002_DATA)
        await _poll(lambda: "dev1/consumer1" in service._discovered_ct002_consumers)

        received = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(f"{base}/ct002/dev1/consumer/consumer1/availability")
            service.on_ct002_consumer_removed("dev1", "consumer1")
            await _collect_messages(
                sub,
                received,
                timeout=3,
                stop=lambda m: m.payload == b"offline",
            )

        assert any(m.payload == b"offline" for m in received)
    finally:
        await service.stop()


@needs_mosquitto
async def test_lwt_online_offline(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    await service.start()

    try:
        await service.wait_connected()

        # Check online status
        received = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(f"{base}/status")
            # The retained "online" message should arrive
            await _collect_messages(sub, received, timeout=2)

        assert len(received) == 1
        assert received[0].payload == b"online"
    finally:
        await service.stop()


@needs_mosquitto
async def test_shelly_event_flow(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    await service.start()

    try:
        await service.wait_connected()

        received = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(f"{base}/shelly/+/battery/+")
            service.on_shelly_response("shelly1", "192.168.1.100", SAMPLE_SHELLY_DATA)
            await _collect_messages(sub, received)

        assert len(received) == 1
        payload = json.loads(received[0].payload)
        assert payload["grid_power"]["total"] == 600.0
        assert payload["active"] is True
        assert payload["poll_interval"] == 5.0
        assert "192_168_1_100" in str(received[0].topic)
    finally:
        await service.stop()


@needs_mosquitto
async def test_manual_target_command_via_mqtt(mqtt_broker) -> None:
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    handler_calls: list[tuple[str, float]] = []

    def mock_handler(consumer_id, target):
        handler_calls.append((consumer_id, target))

    service.register_manual_target_handler("dev1", mock_handler)
    await service.start()

    try:
        await service.wait_connected()

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(
                f"{base}/ct002/dev1/consumer/consumer1/manual_target/set",
                payload=b"150",
            )

        await _poll(lambda: len(handler_calls) >= 1)
        assert handler_calls[0] == ("consumer1", 150.0)
    finally:
        await service.stop()


@needs_mosquitto
async def test_auto_target_command_via_mqtt(mqtt_broker) -> None:
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    handler_calls: list[tuple[str, bool]] = []

    def mock_handler(consumer_id, auto):
        handler_calls.append((consumer_id, auto))

    service.register_auto_target_handler("dev1", mock_handler)
    await service.start()

    try:
        await service.wait_connected()

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(
                f"{base}/ct002/dev1/consumer/consumer1/auto_target/set",
                payload=b"false",
            )

        await _poll(lambda: len(handler_calls) >= 1)
        assert handler_calls[0] == ("consumer1", False)
    finally:
        await service.stop()


@needs_mosquitto
async def test_distribution_weight_command_via_mqtt(mqtt_broker) -> None:
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    handler_calls: list[tuple[str, float]] = []

    def mock_handler(consumer_id, weight):
        handler_calls.append((consumer_id, weight))

    service.register_distribution_weight_handler("dev1", mock_handler)
    await service.start()

    try:
        await service.wait_connected()

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(
                f"{base}/ct002/dev1/consumer/consumer1/distribution_weight/set",
                payload=b"1.5",
            )

        await _poll(lambda: len(handler_calls) >= 1)
        assert handler_calls[0] == ("consumer1", 1.5)
    finally:
        await service.stop()


def test_handle_consumer_field_command_dispatch() -> None:
    """Per-field command parsing routes scalar payloads to the right handler.

    Broker-free: exercises ``_handle_consumer_field_command`` directly.
    """
    service = MqttInsightsService(MqttInsightsConfig(broker="localhost"))
    calls: dict[str, object] = {}
    service.register_active_handler(
        "dev1", lambda cid, v: calls.__setitem__("active", v)
    )
    service.register_auto_target_handler(
        "dev1", lambda cid, v: calls.__setitem__("auto", v)
    )
    service.register_manual_target_handler(
        "dev1", lambda cid, v: calls.__setitem__("manual", v)
    )
    service.register_distribution_weight_handler(
        "dev1", lambda cid, v: calls.__setitem__("weight", v)
    )
    service.register_efficiency_window_weight_handler(
        "dev1", lambda cid, v: calls.__setitem__("eff_weight", v)
    )
    service.register_min_dc_output_handler(
        "dev1", lambda cid, v: calls.__setitem__("min_dc", v)
    )

    service._handle_consumer_field_command("dev1", "c1", "active", "false")
    service._handle_consumer_field_command("dev1", "c1", "auto_target", "true")
    service._handle_consumer_field_command("dev1", "c1", "manual_target", "250")
    service._handle_consumer_field_command("dev1", "c1", "distribution_weight", "2.5")
    # HA sends a percentage; the handler receives the 0-1 fraction.
    service._handle_consumer_field_command(
        "dev1", "c1", "efficiency_window_weight", "50"
    )
    service._handle_consumer_field_command("dev1", "c1", "min_dc_output", "25")
    assert calls == {
        "active": False,
        "auto": True,
        "manual": 250.0,
        "weight": 2.5,
        "eff_weight": 0.5,
        "min_dc": 25.0,
    }

    # 0.0 is a valid weight (battery takes no share / skipped for efficiency).
    calls.clear()
    service._handle_consumer_field_command("dev1", "c1", "distribution_weight", "0")
    service._handle_consumer_field_command(
        "dev1", "c1", "efficiency_window_weight", "0"
    )
    assert calls == {"weight": 0.0, "eff_weight": 0.0}

    # 100 % maps to the full 1.0 fraction.
    calls.clear()
    service._handle_consumer_field_command(
        "dev1", "c1", "efficiency_window_weight", "100"
    )
    assert calls == {"eff_weight": 1.0}

    # Out-of-range and unparseable values are dropped, not dispatched.
    calls.clear()
    service._handle_consumer_field_command("dev1", "c1", "distribution_weight", "11")
    service._handle_consumer_field_command("dev1", "c1", "manual_target", "nan")
    service._handle_consumer_field_command("dev1", "c1", "active", "maybe")
    service._handle_consumer_field_command("dev1", "c1", "min_dc_output", "-5")
    service._handle_consumer_field_command("dev1", "c1", "min_dc_output", "2000")
    # The efficiency window weight is a percentage: 101 % is out of range.
    service._handle_consumer_field_command(
        "dev1", "c1", "efficiency_window_weight", "101"
    )
    service._handle_consumer_field_command(
        "dev1", "c1", "efficiency_window_weight", "-1"
    )
    # An empty (cleared) retained payload is ignored silently.
    service._handle_consumer_field_command("dev1", "c1", "distribution_weight", "")
    service._handle_consumer_field_command("dev1", "c1", "efficiency_window_weight", "")
    assert calls == {}


@needs_mosquitto
async def test_force_rotation_command_via_mqtt(mqtt_broker) -> None:
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    handler_calls: list[str] = []

    def mock_handler():
        handler_calls.append("rotated")

    service.register_rotation_handler("dev1", mock_handler)
    await service.start()

    try:
        await service.wait_connected()

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(
                f"{base}/ct002/dev1/set",
                payload=json.dumps({"force_rotation": True}).encode(),
            )

        await _poll(lambda: len(handler_calls) >= 1)
        assert handler_calls[0] == "rotated"
    finally:
        await service.stop()


def test_active_control_device_command_dispatch():
    """The device-level active_control field routes booleans to the handler
    and rejects non-boolean payloads."""
    service = _make_service(1883)
    calls: list[bool] = []
    service.register_active_control_handler("dev1", calls.append)

    service._handle_device_command("dev1", {"active_control": False})
    service._handle_device_command("dev1", {"active_control": True})
    assert calls == [False, True]

    # Non-boolean is rejected (no dispatch); unknown device is a no-op.
    service._handle_device_command("dev1", {"active_control": "nope"})
    service._handle_device_command("other", {"active_control": False})
    assert calls == [False, True]

    service.unregister_handlers("dev1")
    service._handle_device_command("dev1", {"active_control": True})
    assert calls == [False, True]


@needs_mosquitto
async def test_active_control_toggle_via_mqtt(mqtt_broker) -> None:
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    handler_calls: list[bool] = []

    service.register_active_control_handler("dev1", handler_calls.append)
    await service.start()

    try:
        await service.wait_connected()

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(
                f"{base}/ct002/dev1/set",
                payload=json.dumps({"active_control": False}).encode(),
            )

        await _poll(lambda: len(handler_calls) >= 1)
        assert handler_calls[0] is False
    finally:
        await service.stop()


@needs_mosquitto
async def test_shelly_battery_removal_publishes_offline(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    base = service._config.base_topic
    await service.start()

    try:
        await service.wait_connected()

        # First fire an event so the battery is "discovered"
        service.on_shelly_response("shelly1", "192.168.1.100", SAMPLE_SHELLY_DATA)
        await _poll(
            lambda: "shelly1/192_168_1_100" in service._discovered_shelly_batteries
        )

        received = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(
                f"{base}/shelly/shelly1/battery/192_168_1_100/availability"
            )
            service.on_shelly_battery_removed("shelly1", "192.168.1.100")
            await _collect_messages(
                sub,
                received,
                timeout=3,
                stop=lambda m: m.payload == b"offline",
            )

        assert any(m.payload == b"offline" for m in received)
    finally:
        await service.stop()


def test_consumer_state_includes_manual_target_fields():
    """Consumer state published to MQTT includes manual_target and auto_target."""
    data = dict(SAMPLE_CT002_DATA)
    consumer_state = {
        "manual_target": data.get("manual_target"),
        "auto_target": data.get("auto_target", True),
    }
    assert consumer_state["manual_target"] is None
    assert consumer_state["auto_target"] is True


# ── Marstek MQTT responder tests ─────────────────────────────────────────

_MARSTEK_CD1_FULL = (
    b"pwr_a=100,pwr_b=200,pwr_c=300,pwr_t=600,wif_s=2,wif_r=-50,ver_v=148,slv_n=0,cur_d=0,"
    b"ble_s=0,fc4_v=202409090159,kwh=0.00,n_kwh=0.00,used_kwh=0.00,fed_kwh=0.00"
)


def _make_binding(
    *,
    device_id: str = "ct002-dev1",
    ct_type: str = "HME-4",
    mac: str = "02b250aabbcc",
    wifi_rssi: int = -50,
    values: list[float] | None = None,
    raises: BaseException | None = None,
    cd4_csv: str | None = None,
) -> tuple[MarstekMqttBinding, list[tuple[object, ...]]]:
    calls: list[tuple[object, ...]] = []
    vs = [100.0, 200.0, 300.0] if values is None else values

    async def _get() -> list[float]:
        calls.append(("meter",))
        if raises is not None:
            raise raises
        return list(vs)

    if cd4_csv is None:
        get_cd4_fn = None
    else:

        def _cd4() -> str:
            calls.append(("cd4",))
            return cd4_csv

        get_cd4_fn = _cd4

    return (
        MarstekMqttBinding(
            device_id=device_id,
            ct_type=ct_type,
            mac=mac,
            get_values=_get,
            wifi_rssi=wifi_rssi,
            get_cd4_slave_csv=get_cd4_fn,
        ),
        calls,
    )


def test_register_marstek_while_disconnected_stores_binding():
    """register_marstek before start() only populates the dict."""
    service = MqttInsightsService(MqttInsightsConfig(broker="localhost"))
    binding, _ = _make_binding()

    async def _run() -> None:
        await service.register_marstek(binding)

    asyncio.run(_run())
    assert service._marstek_bindings["ct002-dev1"] is binding


def test_register_marstek_no_op_when_disabled():
    service = MqttInsightsService(
        MqttInsightsConfig(broker="localhost", marstek_mqtt_enabled=False)
    )
    binding, _ = _make_binding()

    async def _run() -> None:
        await service.register_marstek(binding)

    asyncio.run(_run())
    assert service._marstek_bindings == {}


@needs_mosquitto
async def test_marstek_poll_responds_on_both_topics(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    binding, calls = _make_binding(values=[100.0, 200.0, 300.0])
    await service.register_marstek(binding)
    await service.start()

    try:
        await service.wait_connected()
        await _poll(lambda: service._client is not None)

        received = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as client:
            await client.subscribe(f"hame_energy/HME-4/device/{binding.mac}/ctrl")
            await client.subscribe(f"marstek_energy/HME-4/device/{binding.mac}/ctrl")
            await client.publish(
                f"hame_energy/HME-4/App/{binding.mac}/ctrl",
                payload=b"cd=1",
            )
            await _collect_messages(
                client, received, timeout=5, stop=lambda _: len(received) >= 2
            )

        assert len(received) == 2
        topics = sorted(str(m.topic) for m in received)
        assert topics == [
            f"hame_energy/HME-4/device/{binding.mac}/ctrl",
            f"marstek_energy/HME-4/device/{binding.mac}/ctrl",
        ]
        expected = _MARSTEK_CD1_FULL
        for msg in received:
            assert msg.payload == expected
        assert len(calls) == 1
    finally:
        await service.stop()


@needs_mosquitto
async def test_marstek_poll_cd4_responds_with_slave_list(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    inner = (
        "slv_t=HME-4,slv_id=bat-a,slv_ip=192.168.1.50,slv_p=a,"
        "slv_t=HMA-2,slv_id=bat-b,slv_ip=192.168.1.51,slv_p=b"
    )
    binding, calls = _make_binding(
        values=[100.0, 200.0, 300.0],
        cd4_csv=inner,
    )
    await service.register_marstek(binding)
    await service.start()

    try:
        await service.wait_connected()
        await _poll(lambda: service._client is not None)

        received = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as client:
            await client.subscribe(f"hame_energy/HME-4/device/{binding.mac}/ctrl")
            await client.subscribe(f"marstek_energy/HME-4/device/{binding.mac}/ctrl")
            await client.publish(
                f"hame_energy/HME-4/App/{binding.mac}/ctrl",
                payload=b"cd=4,p1=0",
            )
            await _collect_messages(
                client, received, timeout=5, stop=lambda _: len(received) >= 2
            )

        assert len(received) == 2
        expected = inner.encode()
        for msg in received:
            assert msg.payload == expected
        assert calls == [("cd4",)]
    finally:
        await service.stop()


@needs_mosquitto
async def test_marstek_ignores_non_poll_payload(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    binding, calls = _make_binding()
    await service.register_marstek(binding)
    await service.start()

    try:
        await service.wait_connected()
        await _poll(lambda: service._client is not None)

        received = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as client:
            await client.subscribe(f"hame_energy/HME-4/device/{binding.mac}/ctrl")
            await client.publish(
                f"hame_energy/HME-4/App/{binding.mac}/ctrl", payload=b"cd=0"
            )
            await _collect_messages(client, received, timeout=1)

        assert received == []
        assert calls == []
    finally:
        await service.stop()


@needs_mosquitto
async def test_marstek_unregister_stops_replies(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    binding, _ = _make_binding()
    await service.register_marstek(binding)
    await service.start()

    try:
        await service.wait_connected()
        await _poll(lambda: service._client is not None)

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as client:
            await client.subscribe(f"hame_energy/HME-4/device/{binding.mac}/ctrl")

            # Initial poll — expect one reply
            first = []
            await client.publish(
                f"hame_energy/HME-4/App/{binding.mac}/ctrl", payload=b"cd=1"
            )
            await _collect_messages(
                client, first, timeout=5, stop=lambda _: len(first) >= 1
            )
            assert len(first) == 1

            # Unregister and poll again — expect no reply
            await service.unregister_marstek(binding.device_id)
            second = []
            await client.publish(
                f"hame_energy/HME-4/App/{binding.mac}/ctrl", payload=b"cd=1"
            )
            await _collect_messages(client, second, timeout=1)
            assert second == []
    finally:
        await service.stop()


@needs_mosquitto
async def test_marstek_opt_out_disables_subscription(mqtt_broker):
    port = mqtt_broker
    global _test_counter
    _test_counter += 1
    service = MqttInsightsService(
        MqttInsightsConfig(
            broker="127.0.0.1",
            port=port,
            base_topic=f"test_insights_{_test_counter}",
            ha_discovery=True,
            ha_discovery_prefix=f"ha_disc_{_test_counter}",
            marstek_mqtt_enabled=False,
        )
    )
    binding, calls = _make_binding()
    await service.register_marstek(binding)  # no-op when disabled
    await service.start()

    try:
        await service.wait_connected()

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as client:
            await client.subscribe(f"hame_energy/HME-4/device/{binding.mac}/ctrl")
            await client.publish(
                f"hame_energy/HME-4/App/{binding.mac}/ctrl", payload=b"cd=1"
            )
            received = []
            await _collect_messages(client, received, timeout=1)
            assert received == []
            assert calls == []
    finally:
        await service.stop()


@needs_mosquitto
async def test_marstek_get_values_failure_suppressed(mqtt_broker):
    port = mqtt_broker
    service = _make_service(port)
    binding, calls = _make_binding(raises=RuntimeError("powermeter offline"))
    await service.register_marstek(binding)
    await service.start()

    try:
        await service.wait_connected()
        await _poll(lambda: service._client is not None)

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as client:
            await client.subscribe(f"hame_energy/HME-4/device/{binding.mac}/ctrl")
            await client.publish(
                f"hame_energy/HME-4/App/{binding.mac}/ctrl", payload=b"cd=1"
            )
            received = []
            await _collect_messages(client, received, timeout=1)
            assert received == []
        # get_values was called but no reply was published
        await _poll(lambda: binding.device_id in service._marstek_get_values_failed)
        assert calls == [("meter",)]
    finally:
        await service.stop()


@needs_mosquitto
async def test_marstek_register_before_start_subscribes_on_connect(mqtt_broker):
    """A binding registered before start() must get its App topics
    subscribed on the first connect."""
    port = mqtt_broker
    service = _make_service(port)
    binding, _ = _make_binding()
    # Register *before* start — the service must pick this up on connect.
    await service.register_marstek(binding)
    await service.start()

    try:
        await service.wait_connected()
        await _poll(lambda: service._client is not None)

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as client:
            await client.subscribe(f"hame_energy/HME-4/device/{binding.mac}/ctrl")
            await client.publish(
                f"hame_energy/HME-4/App/{binding.mac}/ctrl", payload=b"cd=1"
            )
            received = []
            await _collect_messages(
                client, received, timeout=5, stop=lambda _: len(received) >= 1
            )
        assert len(received) == 1
        assert received[0].payload.startswith(
            b"pwr_a=100,pwr_b=200,pwr_c=300,pwr_t=600,wif_s=2,wif_r=-50,ver_v=148,slv_n=0,cur_d=0,"
        )
    finally:
        await service.stop()


@needs_mosquitto
async def test_marstek_periodic_broadcast(mqtt_broker) -> None:
    """When marstek_mqtt_interval > 0, responses are published periodically
    without requiring a poll request from the app."""
    port = mqtt_broker
    global _test_counter
    _test_counter += 1
    service = MqttInsightsService(
        MqttInsightsConfig(
            broker="127.0.0.1",
            port=port,
            base_topic=f"test_insights_{_test_counter}",
            ha_discovery=True,
            ha_discovery_prefix=f"ha_disc_{_test_counter}",
            marstek_mqtt_interval=0.2,
        )
    )
    binding, calls = _make_binding(values=[100.0, 200.0, 300.0])
    await service.register_marstek(binding)

    async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
        await sub.subscribe(f"hame_energy/HME-4/device/{binding.mac}/ctrl")
        await sub.subscribe(f"marstek_energy/HME-4/device/{binding.mac}/ctrl")

        await service.start()
        try:
            await service.wait_connected()

            received: list[aiomqtt.Message] = []
            # At least 2 broadcast rounds -> 4 messages (2 topics x 2 rounds)
            await _collect_messages(
                sub, received, timeout=5, stop=lambda _: len(received) >= 4
            )

            assert len(received) >= 4
            expected = _MARSTEK_CD1_FULL
            for msg in received:
                assert msg.payload == expected
            assert len(calls) >= 2
        finally:
            await service.stop()


@needs_mosquitto
async def test_marstek_broadcast_disabled_when_interval_zero(mqtt_broker) -> None:
    """marstek_mqtt_interval=0 disables the periodic broadcast loop; only
    explicit poll requests trigger a response."""
    port = mqtt_broker
    global _test_counter
    _test_counter += 1
    service = MqttInsightsService(
        MqttInsightsConfig(
            broker="127.0.0.1",
            port=port,
            base_topic=f"test_insights_{_test_counter}",
            ha_discovery=True,
            ha_discovery_prefix=f"ha_disc_{_test_counter}",
            marstek_mqtt_interval=0,
        )
    )
    binding, calls = _make_binding(values=[100.0, 200.0, 300.0])
    await service.register_marstek(binding)
    await service.start()

    try:
        await service.wait_connected()
        await _poll(lambda: service._client is not None)

        received: list[aiomqtt.Message] = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(f"hame_energy/HME-4/device/{binding.mac}/ctrl")
            await _collect_messages(sub, received, timeout=1)

        assert received == []
        assert calls == []
    finally:
        await service.stop()


@needs_mosquitto
async def test_marstek_slow_handler_does_not_stall_listener(mqtt_broker):
    """A slow get_values for one binding must not block polls for another.

    With the offload-to-task design, the listener stays responsive even
    while a prior poll handler is still awaiting its powermeter.
    """
    port = mqtt_broker
    service = _make_service(port)

    slow_gate = asyncio.Event()

    async def _slow_values() -> list[float]:
        # Block until the test explicitly releases this handler.
        await slow_gate.wait()
        return [1.0, 2.0, 3.0]

    slow = MarstekMqttBinding(
        device_id="slow-ct",
        ct_type="HME-4",
        mac="02b250111111",
        get_values=_slow_values,
        wifi_rssi=-50,
    )
    fast, _ = _make_binding(
        device_id="fast-ct", mac="02b250222222", values=[10.0, 20.0, 30.0]
    )

    await service.register_marstek(slow)
    await service.register_marstek(fast)
    await service.start()

    try:
        await service.wait_connected()
        await _poll(lambda: service._client is not None)

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as client:
            await client.subscribe(f"hame_energy/HME-4/device/{fast.mac}/ctrl")

            # Trigger the slow poll first — its handler will block in get_values.
            await client.publish(
                f"hame_energy/HME-4/App/{slow.mac}/ctrl", payload=b"cd=1"
            )
            # Immediately trigger the fast poll — if the listener were
            # stalled, we'd never see its reply.
            await client.publish(
                f"hame_energy/HME-4/App/{fast.mac}/ctrl", payload=b"cd=1"
            )
            received = []
            await _collect_messages(
                client, received, timeout=5, stop=lambda _: len(received) >= 1
            )

        assert len(received) == 1
        assert received[0].payload.startswith(
            b"pwr_a=10,pwr_b=20,pwr_c=30,pwr_t=60,wif_s=2,wif_r=-50,ver_v=148,slv_n=0,cur_d=0,"
        )
    finally:
        slow_gate.set()
        await service.stop()
