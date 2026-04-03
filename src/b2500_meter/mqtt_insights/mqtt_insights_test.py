"""Tests for MQTT Insights — discovery, service, and E2E with Mosquitto."""

from __future__ import annotations

import asyncio
import configparser
import contextlib
import json
import re

import aiomqtt

from b2500_meter.config.config_loader import (
    create_powermeter,
    read_mqtt_insights_config,
)
from b2500_meter.conftest import needs_mosquitto

from .discovery import (
    _sanitize_id,
    build_ct002_consumer_discovery,
    build_ct002_device_discovery,
    build_shelly_battery_discovery,
    build_shelly_device_discovery,
)
from .service import MqttInsightsConfig, MqttInsightsService

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
        "b2500_meter", "dev1", "consumer1", "homeassistant"
    )
    _assert_discovery_structure(topic, payload)

    assert "homeassistant/device/" in topic
    assert topic.endswith("/config")

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

    # Primary entity has name: null
    assert comps["grid_power_total"]["name"] is None

    # Switch has correct topics
    switch = comps["active"]
    assert switch["platform"] == "switch"
    assert "command_topic" in switch
    assert switch["state_on"] == "True"
    assert switch["state_off"] == "False"


def test_ct002_device_discovery_structure():
    topic, payload = build_ct002_device_discovery(
        "b2500_meter", "dev1", "homeassistant"
    )
    _assert_discovery_structure(topic, payload)
    comps = payload["components"]
    assert "smooth_target" in comps
    assert "active_control" in comps
    assert "consumer_count" in comps
    assert comps["smooth_target"]["name"] is None  # primary


def test_shelly_battery_discovery_structure():
    topic, payload = build_shelly_battery_discovery(
        "b2500_meter", "shelly1", "192.168.1.100", "homeassistant"
    )
    _assert_discovery_structure(topic, payload)
    comps = payload["components"]
    assert "grid_power_total" in comps
    assert "active" in comps
    assert "last_seen" in comps
    assert payload["availability_mode"] == "all"
    assert len(payload["availability"]) == 2


def test_shelly_device_discovery_structure():
    topic, payload = build_shelly_device_discovery(
        "b2500_meter", "shelly1", "homeassistant"
    )
    _assert_discovery_structure(topic, payload)
    assert "battery_count" in payload["components"]


def test_unique_ids_are_unique():
    """All unique_ids within a single discovery payload must be distinct."""
    _, payload = build_ct002_consumer_discovery(
        "b2500_meter", "dev1", "cons1", "homeassistant"
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
    assert result.base_topic == "b2500_meter"
    assert result.ha_discovery is True
    assert result.ha_discovery_prefix == "homeassistant"


def test_read_mqtt_insights_config_absent():
    cfg = configparser.ConfigParser()
    cfg.read_string("[GENERAL]\nDEVICE_TYPE=ct002\n")
    assert read_mqtt_insights_config(cfg) is None


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
    "last_seen": "2026-01-01T00:00:00+00:00",
    "smooth_target": 500.0,
    "active_control": True,
    "consumer_count": 2,
}

SAMPLE_SHELLY_DATA = {
    "grid_power": {"l1": 100.0, "l2": 200.0, "l3": 300.0, "total": 600.0},
    "active": True,
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
        # Give service time to connect
        await asyncio.sleep(0.5)

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
        await asyncio.sleep(0.5)

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
        await asyncio.sleep(0.5)

        discovery_msgs = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(f"{ha_prefix}/device/#")
            # First event for consumer1
            service.on_ct002_response("dev1", "consumer1", SAMPLE_CT002_DATA)
            # Second event for same consumer — should NOT trigger another discovery
            await asyncio.sleep(0.3)
            service.on_ct002_response("dev1", "consumer1", SAMPLE_CT002_DATA)
            # Third event for consumer2 — SHOULD trigger new discovery
            await asyncio.sleep(0.3)
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
        assert any("b2500_meter_ct002_dev1/config" in t for t in topics)
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
        await asyncio.sleep(0.5)

        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(
                f"{base}/ct002/dev1/consumer/consumer1/set",
                payload=json.dumps({"active": False}).encode(),
            )
            await asyncio.sleep(0.5)
            await pub.publish(
                f"{base}/ct002/dev1/consumer/consumer1/set",
                payload=json.dumps({"active": True}).encode(),
            )
            await asyncio.sleep(0.5)

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
        await asyncio.sleep(0.5)

        # First fire an event so the consumer is "discovered"
        service.on_ct002_response("dev1", "consumer1", SAMPLE_CT002_DATA)
        await asyncio.sleep(0.5)

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
        await asyncio.sleep(0.5)

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
        await asyncio.sleep(0.5)

        received = []
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as sub:
            await sub.subscribe(f"{base}/shelly/+/battery/+")
            service.on_shelly_response("shelly1", "192.168.1.100", SAMPLE_SHELLY_DATA)
            await _collect_messages(sub, received)

        assert len(received) == 1
        payload = json.loads(received[0].payload)
        assert payload["grid_power"]["total"] == 600.0
        assert payload["active"] is True
        assert "192_168_1_100" in str(received[0].topic)
    finally:
        await service.stop()
