"""Diagnostics for the AstraMeter integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from . import const
from .coordinator import AstraMeterRuntime

# Diagnostics are user-downloadable and routinely pasted into bug reports, so
# never surface the Marstek account credentials in them.
TO_REDACT = {const.CONF_MARSTEK_MAILBOX, const.CONF_MARSTEK_PASSWORD}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime: AstraMeterRuntime | None = hass.data.get(const.DOMAIN, {}).get(
        entry.entry_id
    )
    diag: dict[str, Any] = {
        "entry": {
            "data": async_redact_data(entry.data, TO_REDACT),
            "options": dict(entry.options),
        },
    }
    if runtime is None:
        diag["runtime"] = None
        return diag
    diag["runtime"] = {
        "device_type": runtime.device_type,
        "device_id": runtime.device_id,
        "udp_port": runtime.udp_port,
        "grid_online": runtime.grid_online(),
        "last_grid_values": runtime.last_grid_values,
        "known_consumers": [list(k) for k in runtime.known_consumers],
        "consumer_state": {
            f"{did}/{cid}": data for (did, cid), data in runtime.consumer_state.items()
        },
    }
    return diag
