"""Diagnostics for the AstraMeter integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import const
from .coordinator import AstraMeterRuntime


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime: AstraMeterRuntime | None = hass.data.get(const.DOMAIN, {}).get(
        entry.entry_id
    )
    diag: dict[str, Any] = {
        "entry": {
            "data": dict(entry.data),
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
