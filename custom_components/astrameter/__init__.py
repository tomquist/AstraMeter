"""The AstraMeter integration."""

from __future__ import annotations

import os
import sys

# The ``astrameter`` package is the shared core. In development it is installed
# (editable) and importable directly. In a HACS install it is *vendored* next to
# this file as ``custom_components/astrameter/astrameter/`` (assembled into the
# release zip), so add this directory to ``sys.path`` to make it importable as a
# top-level package when it isn't already installed.
try:  # pragma: no cover - exercised by the shipped artifact
    import astrameter
except ImportError:  # pragma: no cover
    sys.path.insert(0, os.path.dirname(__file__))
    import astrameter  # noqa: F401

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import const
from .coordinator import AstraMeterRuntime

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AstraMeter from a config entry."""
    runtime = AstraMeterRuntime(hass, entry)
    await runtime.async_start()  # raises ConfigEntryNotReady on bind failure

    hass.data.setdefault(const.DOMAIN, {})[entry.entry_id] = runtime
    await hass.config_entries.async_forward_entry_setups(entry, const.PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, const.PLATFORMS)
    if unloaded:
        runtime: AstraMeterRuntime = hass.data[const.DOMAIN].pop(entry.entry_id)
        await runtime.async_stop()
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Hot-swap filter-only option changes; reload on structural changes."""
    runtime: AstraMeterRuntime | None = hass.data.get(const.DOMAIN, {}).get(
        entry.entry_id
    )
    if runtime is None:
        return
    changed = {
        k
        for k in set(entry.options) | set(runtime.options_snapshot)
        if entry.options.get(k) != runtime.options_snapshot.get(k)
    }
    if changed and changed <= const.FILTER_OPTION_KEYS:
        runtime.options_snapshot = dict(entry.options)
        runtime.apply_filter_options()
    else:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate old config entries (stub for VERSION 1)."""
    return True
