"""Config flow tests for the AstraMeter integration."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

from custom_components.astrameter import const
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry


@contextmanager
def _no_runtime_start():
    """Stub the runtime so the post-reconfigure reload doesn't bind a UDP port."""
    with patch(
        "custom_components.astrameter.coordinator.AstraMeterRuntime.async_start",
        return_value=None,
    ):
        yield


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


async def test_reconfigure_updates_grid_entities(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id="ct002_24000",
        data={
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_CT002,
            const.CONF_UDP_PORT: 24000,
            const.CONF_DEVICE_ID: "ct002_24000",
            const.CONF_PAIR_MODE: False,
            const.CONF_GRID_ENTITIES: ["sensor.old_grid"],
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with _no_runtime_start():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_GRID_ENTITIES: ["sensor.new_grid"],
                const.CONF_PAIR_MODE: False,
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[const.CONF_GRID_ENTITIES] == ["sensor.new_grid"]


async def test_reconfigure_updates_credentials(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id="ct002_24001",
        data={
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_CT002,
            const.CONF_UDP_PORT: 24001,
            const.CONF_DEVICE_ID: "ct002_24001",
            const.CONF_PAIR_MODE: False,
            const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
            const.CONF_MARSTEK_MAILBOX: "old@example.com",
            const.CONF_MARSTEK_PASSWORD: "oldpass",
            const.CONF_MARSTEK_MAC: "02b25000aabb",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    # Change the email; leave the password blank to keep the existing one.
    with _no_runtime_start():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
                const.CONF_PAIR_MODE: False,
                const.CONF_MARSTEK_MAILBOX: "new@example.com",
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[const.CONF_MARSTEK_MAILBOX] == "new@example.com"
    # Blank password keeps the stored one; cached MAC survives the edit.
    assert entry.data[const.CONF_MARSTEK_PASSWORD] == "oldpass"
    assert entry.data[const.CONF_MARSTEK_MAC] == "02b25000aabb"


async def test_reconfigure_clears_credentials(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id="ct002_24002",
        data={
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_CT002,
            const.CONF_UDP_PORT: 24002,
            const.CONF_DEVICE_ID: "ct002_24002",
            const.CONF_PAIR_MODE: False,
            const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
            const.CONF_MARSTEK_MAILBOX: "old@example.com",
            const.CONF_MARSTEK_PASSWORD: "oldpass",
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    with _no_runtime_start():
        # Emptying the email field clears the stored credentials.
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
                const.CONF_PAIR_MODE: False,
                const.CONF_MARSTEK_MAILBOX: "",
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert const.CONF_MARSTEK_MAILBOX not in entry.data
    assert const.CONF_MARSTEK_PASSWORD not in entry.data


async def test_reconfigure_into_pair_mode(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id="ct002_24003",
        data={
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_CT002,
            const.CONF_UDP_PORT: 24003,
            const.CONF_DEVICE_ID: "ct002_24003",
            const.CONF_PAIR_MODE: False,
            const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
        },
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {const.CONF_PAIR_MODE: True},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure_pair"

    with _no_runtime_start():
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                const.CONF_INPUT_ENTITIES: ["sensor.import"],
                const.CONF_OUTPUT_ENTITIES: ["sensor.export"],
            },
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[const.CONF_PAIR_MODE] is True
    assert entry.data[const.CONF_INPUT_ENTITIES] == ["sensor.import"]
    assert entry.data[const.CONF_OUTPUT_ENTITIES] == ["sensor.export"]
    assert entry.data[const.CONF_GRID_ENTITIES] == []
