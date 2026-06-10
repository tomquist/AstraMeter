"""Diagnostics must never surface the Marstek account credentials."""

from __future__ import annotations

from custom_components.astrameter import const
from custom_components.astrameter.diagnostics import (
    async_get_config_entry_diagnostics,
)
from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry


async def test_credentials_are_redacted(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id="ct002_diag",
        data={
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_CT002,
            const.CONF_UDP_PORT: 25000,
            const.CONF_DEVICE_ID: "ct002_diag",
            const.CONF_PAIR_MODE: False,
            const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
            const.CONF_MARSTEK_MAILBOX: "secret@example.com",
            const.CONF_MARSTEK_PASSWORD: "supersecret",
        },
    )
    entry.add_to_hass(hass)

    diag = await async_get_config_entry_diagnostics(hass, entry)
    data = diag["entry"]["data"]
    assert data[const.CONF_MARSTEK_PASSWORD] == REDACTED
    assert data[const.CONF_MARSTEK_MAILBOX] == REDACTED
    # Non-sensitive config is still present.
    assert data[const.CONF_GRID_ENTITIES] == ["sensor.grid_power"]
    assert "supersecret" not in str(diag)
