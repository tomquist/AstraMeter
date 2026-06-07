"""Switch platform for AstraMeter (CT002 consumer controls)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from astrameter import entity_model as em

from .entity import AstraMeterEntity, async_setup_platform_entities


class AstraMeterSwitch(AstraMeterEntity, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        value = self._value()
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("true", "1", "on")

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        self._set(False)

    def _set(self, value: bool) -> None:
        consumer_id = self._consumer_key[1] if self._consumer_key else None
        self._runtime.call_setter(self._desc.setter, value, consumer_id)
        self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await async_setup_platform_entities(
        hass, entry, async_add_entities, em.SWITCH, AstraMeterSwitch
    )
