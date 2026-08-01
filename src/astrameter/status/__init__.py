"""Live-state surface backing the dashboard status API."""

from .mode import HA_ADVANCED, HA_SIMPLE, STANDALONE, detect_config_mode
from .registry import SCHEMA_VERSION, DeviceEntry, StatusRegistry

__all__ = [
    "HA_ADVANCED",
    "HA_SIMPLE",
    "SCHEMA_VERSION",
    "STANDALONE",
    "DeviceEntry",
    "StatusRegistry",
    "detect_config_mode",
]
