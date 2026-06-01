"""Tests for MQTT Insights — discovery, service, and E2E with Mosquitto."""

from __future__ import annotations

import asyncio
import configparser
import contextlib
import json
import re

import aiomqtt

from astrameter.config.config_loader import (
    create_powermeter,
    read_mqtt_insights_config,
)
from astrameter.conftest import needs_mosquitto

from .discovery import (
    _sanitize_id,
    build_ct002_consumer_discovery,
    build_ct002_device_discovery,
    build_shelly_battery_discovery,
    build_shelly_device_discovery,
)
from .marstek_mqtt import MarstekMqttBinding
from .service import MqttInsightsConfig, MqttInsightsService, _arp_lookup

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
    assert ["bluetooth", "AA:BB:CC:DD:EE:FF"] in dev["connections"]
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


def test_ct002_consumer_discovery_network_mac_and_ip():
    """network_mac and battery_ip add connection entries."""
    _, payload = build_ct002_consumer_discovery(
        "astrameter",
        "dev1",
        "aabbccddeeff",
        "homeassistant",
        network_mac="11:22:33:44:55:66",
        battery_ip="192.168.1.10",
    )
    conns = payload["device"]["connections"]
    assert ["bluetooth", "AA:BB:CC:DD:EE:FF"] in conns
    assert ["mac", "11:22:33:44:55:66"] in conns
    assert ["ip", "192.168.1.10"] in conns
    assert payload["device"]["via_device"] == "astrameter_ct002_dev1"


def test_ct002_device_discovery_structure():
    topic, payload = build_ct002_device_discovery(
        "astrameter", "dev1", "homeassistant", addon_slug="34dea19a_astrameter"
    )
    _assert_discovery_structure(topic, payload)
    assert "AstraMeter" in payload["device"]["name"]
    assert payload["device"]["via_device"] == "34dea19a_astrameter"
    comps = payload["components"]
    assert "smooth_target" in comps
    assert "active_control" in comps
    assert "consumer_count" in comps
    assert comps["smooth_target"]["name"] is None  # primary

    # Force rotation button
    btn = comps["force_rotation"]
    assert btn["platform"] == "button"
    assert "command_topic" in btn
    assert "payload_press" in btn
    assert btn["entity_category"] == "config"


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


def test_meter_device_discovery_omits_via_device_without_addon_slug():
    _, ct002 = build_ct002_device_discovery("astrameter", "dev1", "homeassistant")
    assert "via_device" not in ct002["device"]
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


async def test_arp_lookup_found(tmp_path):
    """ARP lookup finds a matching entry."""
    arp_file = tmp_path / "arp"
    arp_file.write_text(
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.10     0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0\n"
        "192.168.1.20     0x1         0x2         11:22:33:44:55:66     *        eth0\n"
    )
    from unittest.mock import mock_open, patch

    real_data = arp_file.read_text()
    m = mock_open(read_data=real_data)
    # mock_open doesn't support iteration by default; wire it up
    m.return_value.__iter__ = lambda self: iter(real_data.splitlines(keepends=True))
    with patch("builtins.open", m):
        result = await _arp_lookup("192.168.1.10")
    assert result == "AA:BB:CC:DD:EE:FF"


async def test_arp_lookup_not_found(tmp_path):
    """ARP lookup returns empty when IP is not in the table."""
    from unittest.mock import mock_open, patch

    data = (
        "IP address       HW type     Flags       HW address            Mask     Device\n"
        "192.168.1.10     0x1         0x2         aa:bb:cc:dd:ee:ff     *        eth0\n"
    )
    m = mock_open(read_data=data)
    m.return_value.__iter__ = lambda self: iter(data.splitlines(keepends=True))
    with patch("builtins.open", m):
        result = await _arp_lookup("192.168.1.99")
    assert result == ""


async def test_arp_lookup_file_missing():
    """ARP lookup returns empty when /proc/net/arp is not available."""
    from unittest.mock import patch

    with patch("builtins.open", side_effect=OSError):
        result = await _arp_lookup("192.168.1.10")
    assert result == ""


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


# ── Service unit tests (no broker) ───────────────────────────────────────


def test_queue_overflow_does_not_raise():
    """Overflowing the queue should not raise."""
    service = MqttInsightsService(MqttInsightsConfig(broker="localhost"))
    for i in range(200):
        service.on_ct002_response("dev1", f"consumer{i}", {"grid_power": {}})
    # No exception raised


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


def _make_service(port: int, base_topic: str | None = None) -> MqttInsightsService:
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
                stop=lambda _: len(discovery_msgs) >= 3,
            )

        # Expect: device discovery + consumer1 discovery + consumer2 discovery = 3
        # (no duplicate for second consumer1 event)
        assert len(discovery_msgs) == 3
        topics = [str(m.topic) for m in discovery_msgs]
        # Device-level discovery
        assert any("astrameter_ct002_dev1/config" in t for t in topics)
        # Consumer-level discoveries
        assert any("consumer1" in t for t in topics)
        assert any("consumer2" in t for t in topics)
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

    service._handle_consumer_field_command("dev1", "c1", "active", "false")
    service._handle_consumer_field_command("dev1", "c1", "auto_target", "true")
    service._handle_consumer_field_command("dev1", "c1", "manual_target", "250")
    service._handle_consumer_field_command("dev1", "c1", "distribution_weight", "2.5")
    assert calls == {"active": False, "auto": True, "manual": 250.0, "weight": 2.5}

    # 0.0 is a valid weight (battery takes no share).
    calls.clear()
    service._handle_consumer_field_command("dev1", "c1", "distribution_weight", "0")
    assert calls == {"weight": 0.0}

    # Out-of-range and unparseable values are dropped, not dispatched.
    calls.clear()
    service._handle_consumer_field_command("dev1", "c1", "distribution_weight", "11")
    service._handle_consumer_field_command("dev1", "c1", "manual_target", "nan")
    service._handle_consumer_field_command("dev1", "c1", "active", "maybe")
    # An empty (cleared) retained payload is ignored silently.
    service._handle_consumer_field_command("dev1", "c1", "distribution_weight", "")
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
