"""Sensor platform for AstraMeter."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from astrameter import entity_model as em

from .entity import AstraMeterEntity, async_setup_platform_entities


class AstraMeterSensor(AstraMeterEntity, SensorEntity):
    """A native sensor backed by an entity_model descriptor."""

    def __init__(self, runtime, desc, **kwargs) -> None:
        super().__init__(runtime, desc, **kwargs)
        self._attr_device_class = desc.device_class
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_state_class = desc.state_class
        if desc.options:
            self._attr_options = list(desc.options)

    @property
    def native_value(self) -> Any:
        value = self._value()
        if value is None:
            return None
        if self._desc.device_class == "timestamp" and isinstance(value, str):
            return dt_util.parse_datetime(value)
        if self._desc.device_class == "timestamp" and isinstance(value, datetime):
            return value
        return value


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await async_setup_platform_entities(
        hass, entry, async_add_entities, em.SENSOR, AstraMeterSensor
    )
