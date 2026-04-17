"""Marstek MQTT responder — answer CT002/CT003 poll requests on the local broker.

Pure helpers (topic formatting, payload building, poll detection) plus a
binding dataclass that the :class:`MqttInsightsService` stores per device.

The wire protocol (topics + CSV key=value payload) matches what the real
Marstek CT002 uses against the Marstek cloud broker. When hame-relay is
bridging the local broker to the cloud, responses produced here reach the
Marstek mobile app as if they came from a real CT002.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

APP_TOPIC_TEMPLATES = (
    "hame_energy/{ct_type}/App/{mac}/ctrl",
    "marstek_energy/{ct_type}/App/{mac}/ctrl",
)
DEVICE_TOPIC_TEMPLATES = (
    "hame_energy/{ct_type}/device/{mac}/ctrl",
    "marstek_energy/{ct_type}/device/{mac}/ctrl",
)

# Matches observed real-device values; included so hm2mqtt-style parsers
# recognise the message as a well-formed runtime-info frame.
DEFAULT_VER_V = 148

_APP_TOPIC_RE = re.compile(
    r"^(?:hame|marstek)_energy/(?P<ct_type>[^/]+)/App/(?P<mac>[^/]+)/ctrl$"
)
_MAC_HEX_RE = re.compile(r"^[0-9a-f]{12}$")


@dataclass(frozen=True)
class MarstekMqttBinding:
    """Per-device registration used by the MQTT Insights service."""

    device_id: str
    ct_type: str
    mac: str
    get_values: Callable[[], Awaitable[list[float]]]
    wifi_rssi: int
    ver_v: int = DEFAULT_VER_V


def normalize_mac(raw: str) -> str:
    """Lowercase, strip ``:``/``-``; return ``""`` if not 12 hex chars."""
    if not raw:
        return ""
    cleaned = raw.replace(":", "").replace("-", "").strip().lower()
    return cleaned if _MAC_HEX_RE.fullmatch(cleaned) else ""


def is_poll_payload(body: bytes) -> bool:
    """Return True iff *body* is a CSV containing ``cd=1``."""
    if not body:
        return False
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    for chunk in text.split(","):
        key, sep, value = chunk.partition("=")
        if not sep:
            continue
        if key.strip().lower() == "cd" and value.strip() == "1":
            return True
    return False


def parse_app_topic(topic: str) -> tuple[str, str] | None:
    """Return ``(ct_type, mac)`` for a Marstek App topic, else ``None``."""
    match = _APP_TOPIC_RE.match(topic)
    if not match:
        return None
    return match.group("ct_type"), match.group("mac").lower()


def app_topics_for(binding: MarstekMqttBinding) -> tuple[str, str]:
    return tuple(  # type: ignore[return-value]
        t.format(ct_type=binding.ct_type, mac=binding.mac) for t in APP_TOPIC_TEMPLATES
    )


def device_topics_for(binding: MarstekMqttBinding) -> tuple[str, str]:
    return tuple(  # type: ignore[return-value]
        t.format(ct_type=binding.ct_type, mac=binding.mac)
        for t in DEVICE_TOPIC_TEMPLATES
    )


def build_response(binding: MarstekMqttBinding, watts: list[float]) -> bytes:
    """Build the CSV ``k=v`` response body for a runtime-info frame."""
    vs = list(watts) + [0.0] * (3 - len(watts))
    a, b, c = (round(v) for v in vs[:3])
    total = a + b + c
    payload = (
        f"pwr_a={a},pwr_b={b},pwr_c={c},pwr_t={total},"
        f"wif_r={binding.wifi_rssi},ver_v={binding.ver_v},wif_s=2"
    )
    return payload.encode("utf-8")
