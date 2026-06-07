"""MQTT Insights — publish internal state to MQTT with HA Discovery.

Kept import-light at the package level: the pure Marstek responder helpers
(``marstek_mqtt``) must be importable without dragging in ``.service`` and its
heavy ``aiomqtt`` dependency. The native Home Assistant integration reuses these
helpers but talks MQTT through Home Assistant's own broker, never ``aiomqtt``.
``MqttInsightsConfig`` / ``MqttInsightsService`` are therefore exposed lazily.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .marstek_mqtt import (
    MarstekMqttBinding,
    format_cd4_slave_csv,
    normalize_mac,
    ver_v_from_marstek_api_version,
)

__all__ = [
    "MarstekMqttBinding",
    "MqttInsightsConfig",
    "MqttInsightsService",
    "format_cd4_slave_csv",
    "normalize_mac",
    "ver_v_from_marstek_api_version",
]

if TYPE_CHECKING:
    from .service import MqttInsightsConfig, MqttInsightsService

_LAZY = {"MqttInsightsConfig", "MqttInsightsService"}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from . import service

        value = getattr(service, name)
        globals()[name] = value  # cache for subsequent lookups
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
