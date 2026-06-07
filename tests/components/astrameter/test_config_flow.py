"""Config flow tests for the AstraMeter integration."""

from __future__ import annotations

from custom_components.astrameter import const
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType


async def test_user_flow_single_entity(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_CT002,
            const.CONF_UDP_PORT: 23456,
            const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
            const.CONF_PAIR_MODE: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][const.CONF_DEVICE_TYPE] == const.DEVICE_TYPE_CT002
    assert result["data"][const.CONF_GRID_ENTITIES] == ["sensor.grid_power"]


async def test_duplicate_port_aborts(hass: HomeAssistant) -> None:
    first = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    await hass.config_entries.flow.async_configure(
        first["flow_id"],
        {
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_CT002,
            const.CONF_UDP_PORT: 23457,
            const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
            const.CONF_PAIR_MODE: False,
        },
    )
    second = await hass.config_entries.flow.async_init(
        const.DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        second["flow_id"],
        {
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_CT002,
            const.CONF_UDP_PORT: 23457,
            const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
            const.CONF_PAIR_MODE: False,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
