"""Binary sensor platform for AstraMeter."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from astrameter import entity_model as em

from .entity import AstraMeterEntity, async_setup_platform_entities


class AstraMeterBinarySensor(AstraMeterEntity, BinarySensorEntity):
    def __init__(self, runtime, desc, **kwargs) -> None:
        super().__init__(runtime, desc, **kwargs)
        self._attr_device_class = desc.device_class

    @property
    def is_on(self) -> bool | None:
        value = self._value()
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "on")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await async_setup_platform_entities(
        hass, entry, async_add_entities, em.BINARY_SENSOR, AstraMeterBinarySensor
    )
