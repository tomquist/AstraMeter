"""Number platform for AstraMeter (CT002 consumer controls)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from astrameter import entity_model as em

from .entity import AstraMeterEntity, async_setup_platform_entities

_MODE = {"box": NumberMode.BOX, "slider": NumberMode.SLIDER}


class AstraMeterNumber(AstraMeterEntity, NumberEntity):
    def __init__(self, runtime, desc, **kwargs) -> None:
        super().__init__(runtime, desc, **kwargs)
        self._attr_device_class = desc.device_class
        self._attr_native_unit_of_measurement = desc.unit
        if desc.min is not None:
            self._attr_native_min_value = desc.min
        if desc.max is not None:
            self._attr_native_max_value = desc.max
        if desc.step is not None:
            self._attr_native_step = desc.step
        if desc.mode in _MODE:
            self._attr_mode = _MODE[desc.mode]

    @property
    def native_value(self) -> float | None:
        value = self._value()
        return float(value) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        consumer_id = self._consumer_key[1] if self._consumer_key else None
        self._runtime.call_setter(self._desc.setter, value, consumer_id)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await async_setup_platform_entities(
        hass, entry, async_add_entities, em.NUMBER, AstraMeterNumber
    )
