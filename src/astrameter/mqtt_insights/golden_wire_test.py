"""Byte-level golden of every MQTT topic and payload this package emits.

Home Assistant dedupes entities on the discovery topic, the ``unique_id`` and
the ``value_template`` strings, and ``esphome/components/ct002/ha_discovery.cpp``
emits the same ones for the firmware stack — so a string that shifts here
silently splits one device into two on a broker both stacks share.  The
structure tests next door check that the required fields are present; this one
pins every character of every payload, plus the order and the ``qos``/``retain``
flags of the publishes a full session makes.

A failure here is a wire change, not a test that needs updating.  When the
change is intended, regenerate with ``UPDATE_GOLDEN=1 pytest`` and review the
diff to ``golden_wire.json`` as you would review a protocol change.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import aiomqtt
import pytest

from . import discovery as discovery_module
from . import service as service_module
from .discovery import (
    build_addon_device_discovery,
    build_ct002_consumer_discovery,
    build_ct002_device_discovery,
    build_powermeter_device_discovery,
    build_retirement_payload,
    build_shelly_battery_discovery,
    build_shelly_device_discovery,
)
from .service import MqttInsightsConfig, MqttInsightsService

GOLDEN_PATH = Path(__file__).with_name("golden_wire.json")

GOLDEN_SHA = "0123456789abcdef"
GOLDEN_VERSION = "9.9.9-golden"

CT002_DATA: dict[str, Any] = {
    "grid_power": {"l1": 100.0, "l2": 200.0, "l3": 300.0, "total": 600.0},
    "target": {"l1": 50.0, "l2": 100.0, "l3": 150.0},
    "phase": "A",
    "reported_power": 42,
    "device_type": "HMJ-1",
    "battery_ip": "192.168.1.10",
    "ct_type": "HME-4",
    "ct_mac": "AA:BB:CC:DD:EE:FF",
    "saturation": 0.5,
    "last_target": 300.0,
    "active": True,
    "poll_interval": 5.0,
    "answer_interval": 7.5,
    "last_seen": "2026-01-01T00:00:00+00:00",
    "manual_target": None,
    "auto_target": True,
    "distribution_weight": 1.5,
    "efficiency_window_weight": 0.5,
    "min_dc_output": 25.0,
    "efficiency_rotation": True,
    "smooth_target": 500.0,
    "active_control": True,
    "consumer_count": 2,
    "control_quality": "off_target",
    "control_quality_score": 41.5,
    "control_quality_error_w": 214.0,
    "control_quality_in_band_pct": 11.0,
    "control_quality_crossings_per_min": 3.4,
    "control_quality_band_w": 25.0,
}

SHELLY_DATA: dict[str, Any] = {
    "grid_power": {"l1": 100.0, "l2": 200.0, "l3": 300.0, "total": 600.0},
    "active": True,
    "poll_interval": 5.0,
    "last_seen": "2026-01-01T00:00:00+00:00",
    "battery_count": 1,
}


@pytest.fixture(autouse=True)
def _pinned_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze the two build stamps that travel in the payloads."""
    monkeypatch.setattr(discovery_module, "get_git_commit_sha", lambda: GOLDEN_SHA)
    monkeypatch.setattr(service_module, "get_version", lambda: GOLDEN_VERSION)


def _decode(payload: Any) -> Any:
    if isinstance(payload, bytes):
        text = payload.decode()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return payload


class _RecordingClient:
    """aiomqtt.Client stand-in that appends every wire action to a list."""

    def __init__(self, records: list[dict], **kwargs: Any) -> None:
        self._records = records
        will = kwargs.get("will")
        if will is not None:
            self._records.append(
                {
                    "will": str(will.topic),
                    "payload": _decode(will.payload),
                    "qos": will.qos,
                    "retain": will.retain,
                }
            )

    async def __aenter__(self) -> _RecordingClient:
        return self

    async def __aexit__(self, *exc: Any) -> bool:
        return False

    async def publish(
        self,
        topic: str,
        payload: Any = None,
        qos: int = 0,
        retain: bool = False,
    ) -> None:
        body = _decode(payload)
        if str(topic).endswith("/config") and isinstance(body, dict):
            # Every byte of a discovery payload is pinned once in the
            # "discovery" section; what the session has to prove is which
            # entities each event publishes — the gated ones especially — and
            # that the retirement update goes out before the current payload.
            body = {"components": sorted(body.get("components", {}))}
        self._records.append(
            {
                "publish": str(topic),
                "payload": body,
                "qos": qos,
                "retain": retain,
            }
        )

    async def subscribe(self, topic: str, **kwargs: Any) -> None:
        self._records.append({"subscribe": str(topic)})

    async def unsubscribe(self, topic: str, **kwargs: Any) -> None:
        self._records.append({"unsubscribe": str(topic)})

    @property
    def messages(self) -> Any:
        async def _forever() -> Any:
            await asyncio.Event().wait()
            yield  # pragma: no cover - unreachable, keeps this a generator

        return _forever()


async def _session_records(
    monkeypatch: pytest.MonkeyPatch, expected: int | None
) -> list[dict]:
    """Everything one service session puts on the wire, in order."""
    records: list[dict] = []
    monkeypatch.setattr(
        aiomqtt,
        "Client",
        lambda **kwargs: _RecordingClient(records, **kwargs),
    )

    service = MqttInsightsService(
        MqttInsightsConfig(
            broker="broker.invalid",
            base_topic="am",
            ha_discovery_prefix="ha",
            addon_slug="abc123_astrameter",
            marstek_mqtt_interval=0.0,
        )
    )
    await service.start()
    await service.wait_connected()

    service.on_ct002_response("dev1", "c1", CT002_DATA)
    # A repeat for the same consumer must not re-publish discovery.
    service.on_ct002_response("dev1", "c1", CT002_DATA)
    service.on_ct002_response("dev1", "c2", CT002_DATA)
    service.on_ct002_consumer_removed("dev1", "c1")
    service.on_shelly_response("shelly1", "192.168.1.100", SHELLY_DATA)
    service.on_shelly_battery_removed("shelly1", "192.168.1.100")

    await _drain(service, records, expected)
    # ``stop()`` publishes the trailing offline status through a second client.
    await service.stop()
    return records


async def _drain(
    service: MqttInsightsService,
    records: list[dict],
    expected: int | None,
    timeout: float = 20.0,
) -> None:
    """Wait for the publish loop to emit everything the events will produce.

    The golden itself says how many wire actions to expect, so a busy machine
    cannot cut the recording short and turn a slow scheduler into a diff.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    idle = 0
    while loop.time() < deadline:
        before = len(records)
        await asyncio.sleep(0.02)
        if expected is not None and len(records) >= expected:
            return
        idle = idle + 1 if len(records) == before and service._queue.empty() else 0
        if idle >= 10:
            return


def _discovery_cases() -> dict[str, Any]:
    """Every discovery builder, over the variants that gate an entity."""
    cases: dict[str, tuple[str, dict]] = {
        "addon": build_addon_device_discovery("am", "abc123_astrameter", "ha"),
        "ct002_device": build_ct002_device_discovery(
            "am", "dev1", "ha", addon_slug="abc123_astrameter"
        ),
        "ct002_device_efficiency_rotation": build_ct002_device_discovery(
            "am",
            "dev1",
            "ha",
            addon_slug="abc123_astrameter",
            efficiency_rotation=True,
        ),
        # HMJ-1 is an external-inverter DC family, so it carries min_dc_output.
        "ct002_consumer_dc_rotation": build_ct002_consumer_discovery(
            "am",
            "dev1",
            "aa:bb:cc:dd:ee:ff",
            "ha",
            device_type="HMJ-1",
            efficiency_rotation=True,
        ),
        # Venus has a built-in inverter and no rotation: neither optional entity.
        "ct002_consumer_plain": build_ct002_consumer_discovery(
            "am", "dev1", "aabbccddeeff", "ha", device_type="HMG-50"
        ),
        "ct002_consumer_no_device_type": build_ct002_consumer_discovery(
            "am", "dev1", "aabbccddeeff", "ha"
        ),
        "powermeter": build_powermeter_device_discovery(
            "am", "SMA_ENERGY_METER", "SMA_ENERGY_METER", "ha", addon_slug="hub_id"
        ),
        "shelly_device": build_shelly_device_discovery(
            "am", "shelly1", "ha", addon_slug="abc123_astrameter"
        ),
        "shelly_battery": build_shelly_battery_discovery(
            "am", "shelly1", "192.168.1.100", "ha"
        ),
    }
    out: dict[str, Any] = {
        name: {"topic": topic, "payload": payload}
        for name, (topic, payload) in cases.items()
    }
    retired_topic, retired_payload = cases["ct002_consumer_plain"]
    out["ct002_consumer_retirement"] = {
        "topic": retired_topic,
        "payload": build_retirement_payload(retired_payload),
    }
    return out


async def test_wire_format_matches_golden(monkeypatch: pytest.MonkeyPatch) -> None:
    golden = json.loads(GOLDEN_PATH.read_text()) if GOLDEN_PATH.exists() else {}
    # Everything but the trailing offline status, which ``stop()`` publishes.
    expected = len(golden["session"]) - 1 if golden.get("session") else None

    actual = {
        "discovery": _discovery_cases(),
        "session": await _session_records(monkeypatch, expected),
    }
    rendered = json.dumps(actual, indent=2, sort_keys=False) + "\n"
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN_PATH.write_text(rendered)
    # Compare the serialized form, not the parsed one: JSON object key order is
    # part of the bytes on the wire, and ``dict`` equality would not see it move.
    assert rendered == GOLDEN_PATH.read_text()
