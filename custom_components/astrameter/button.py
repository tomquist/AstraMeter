"""Button platform for AstraMeter (CT002 device controls)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from astrameter import entity_model as em

from .entity import AstraMeterEntity, async_setup_platform_entities


class AstraMeterButton(AstraMeterEntity, ButtonEntity):
    @property
    def available(self) -> bool:
        return self._runtime.device is not None

    async def async_press(self) -> None:
        self._runtime.call_setter(self._desc.setter)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    await async_setup_platform_entities(
        hass, entry, async_add_entities, em.BUTTON, AstraMeterButton
    )
