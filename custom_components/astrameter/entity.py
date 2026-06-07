"""Base entity, device-registry tree, and generic platform setup.

Entities are built from the shared ``astrameter.entity_model`` catalog so the
native integration and MQTT Insights expose the same inventory. Values come from
the CT002/Shelly event ``data`` dict delivered to ``AstraMeterRuntime._on_event``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from astrameter import entity_model as em

from . import const
from .coordinator import AstraMeterRuntime

# Scope kinds for an entity's data source.
SCOPE_DEVICE = "device"  # device-level (read latest consumer event for device_id)
SCOPE_CONSUMER = "consumer"  # one CT002 consumer / Shelly battery
SCOPE_POWERMETER = "powermeter"  # the grid-power source health device

_ENTITY_CATEGORY = {
    "diagnostic": EntityCategory.DIAGNOSTIC,
    "config": EntityCategory.CONFIG,
}


def _sanitize(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)


# ── Device registry tree (mirrors discovery.py identifiers/names) ──────────


def ct002_device_info(entry: ConfigEntry, device_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(const.DOMAIN, f"ct002_{_sanitize(device_id)}")},
        name=f"AstraMeter CT002 {device_id}",
        manufacturer="astrameter",
    )


def ct002_consumer_device_info(
    entry: ConfigEntry, device_id: str, data: dict[str, Any]
) -> DeviceInfo:
    consumer_id = data.get("_consumer_id", "")
    device_type = data.get("device_type", "")
    mac_slug = _sanitize(consumer_id).lower().replace("-", "").replace("_", "")
    connections: set[tuple[str, str]] = set()
    if re.fullmatch(r"[0-9a-f]{12}", mac_slug):
        bt = ":".join(mac_slug[i : i + 2] for i in range(0, 12, 2)).upper()
        connections.add(("bluetooth", bt))
    if data.get("ct_mac"):
        connections.add(("mac", str(data["ct_mac"])))
    if data.get("battery_ip"):
        connections.add(("ip", str(data["battery_ip"])))
    name = (
        f"AstraMeter Consumer {device_type} {mac_slug}"
        if device_type
        else f"AstraMeter Consumer {mac_slug}"
    )
    info = DeviceInfo(
        identifiers={(const.DOMAIN, f"consumer_{mac_slug}")},
        name=name,
        manufacturer="Marstek",
        via_device=(const.DOMAIN, f"ct002_{_sanitize(device_id)}"),
    )
    if device_type:
        info["model"] = device_type
    if connections:
        info["connections"] = connections
    return info


def shelly_device_info(entry: ConfigEntry, device_id: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(const.DOMAIN, f"shelly_{_sanitize(device_id)}")},
        name=f"AstraMeter Shelly {device_id}",
        manufacturer="astrameter",
    )


def shelly_battery_device_info(
    entry: ConfigEntry, device_id: str, battery_ip: str
) -> DeviceInfo:
    return DeviceInfo(
        identifiers={
            (const.DOMAIN, f"shelly_{_sanitize(device_id)}_{_sanitize(battery_ip)}")
        },
        name=f"AstraMeter Shelly Battery {battery_ip}",
        manufacturer="astrameter",
        via_device=(const.DOMAIN, f"shelly_{_sanitize(device_id)}"),
    )


# ── Base entity ────────────────────────────────────────────────────────────


class AstraMeterEntity(Entity):
    """Common behaviour: dispatcher updates, availability, value access."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        runtime: AstraMeterRuntime,
        desc: em.EntityDescriptor,
        *,
        scope: str,
        device_info: DeviceInfo,
        unique_id: str,
        consumer_key: tuple[str, str] | None = None,
        device_id: str | None = None,
    ) -> None:
        self._runtime = runtime
        self._desc = desc
        self._scope = scope
        self._consumer_key = consumer_key
        self._device_id = device_id
        self._attr_unique_id = unique_id
        self._attr_device_info = device_info
        self._attr_entity_category = _ENTITY_CATEGORY.get(desc.entity_category)
        if desc.primary:
            self._attr_name = None
        else:
            self._attr_name = desc.name

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                const.signal_update(self._runtime.entry.entry_id),
                self._handle_update,
            )
        )
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                const.signal_health(self._runtime.entry.entry_id),
                self._handle_health,
            )
        )

    @callback
    def _handle_update(self, key: tuple[str, str] | None = None) -> None:
        self.async_write_ha_state()

    @callback
    def _handle_health(self) -> None:
        self.async_write_ha_state()

    def _data(self) -> dict[str, Any] | None:
        if self._scope == SCOPE_CONSUMER:
            return self._runtime.consumer_state.get(self._consumer_key or ("", ""))
        if self._scope == SCOPE_DEVICE:
            # Device-level fields ride on any consumer event for this device.
            for (did, _cid), data in self._runtime.consumer_state.items():
                if did == self._device_id and data is not None:
                    return data
            return None
        if self._scope == SCOPE_POWERMETER:
            return self._runtime.powermeter_state()
        return None

    @property
    def available(self) -> bool:
        if self._scope == SCOPE_POWERMETER:
            return True  # health device reports its own online state
        data = self._data()
        if data is None:
            return False
        # Consumer entities go unavailable when the upstream grid source is down.
        return not (self._scope == SCOPE_CONSUMER and not self._runtime.grid_online())

    def _value(self) -> Any:
        data = self._data()
        if data is None:
            return None
        return em.extract_value(data, self._desc)


# ── Generic platform setup ─────────────────────────────────────────────────

EntityFactory = Callable[..., AstraMeterEntity]


def _kinds_for(device_type: str) -> tuple[str, str]:
    """Return (device_level_kind, per_consumer_kind) for the entry device type."""
    if device_type in const.CT002_DEVICE_TYPES:
        return em.CT002_DEVICE, em.CT002_CONSUMER
    return em.SHELLY_DEVICE, em.SHELLY_BATTERY


async def async_setup_platform_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    platform: str,
    factory: EntityFactory,
) -> None:
    """Build all entities of ``platform`` for this entry from the catalog."""
    runtime: AstraMeterRuntime = hass.data[const.DOMAIN][entry.entry_id]
    device_kind, consumer_kind = _kinds_for(runtime.device_type)
    added: set[str] = set()

    # Static device-level + powermeter-health entities.
    static: list[AstraMeterEntity] = []
    for desc in em.ENTITIES_BY_KIND[device_kind]:
        if desc.platform != platform:
            continue
        uid = f"astrameter_{device_kind}_{_sanitize(runtime.device_id)}_{desc.key}"
        if device_kind == em.CT002_DEVICE:
            info = ct002_device_info(entry, runtime.device_id)
        else:
            info = shelly_device_info(entry, runtime.device_id)
        static.append(
            factory(
                runtime,
                desc,
                scope=SCOPE_DEVICE,
                device_info=info,
                unique_id=uid,
                device_id=runtime.device_id,
            )
        )
        added.add(uid)

    # The grid source is a Home Assistant sensor whose own availability is
    # already visible, so we deliberately do NOT expose a separate "powermeter"
    # health device natively (em.POWERMETER_ENTITIES stays for MQTT parity only).
    if static:
        async_add_entities(static)

    @callback
    def _add_consumer(key: tuple[str, str]) -> None:
        device_id, consumer_id = key
        data = runtime.consumer_state.get(key) or {}
        data = {**data, "_consumer_id": consumer_id}
        new: list[AstraMeterEntity] = []
        for desc in em.ENTITIES_BY_KIND[consumer_kind]:
            if desc.platform != platform:
                continue
            if not desc.present_for(str(data.get("device_type", ""))):
                continue
            uid = (
                f"astrameter_{consumer_kind}_{_sanitize(device_id)}_"
                f"{_sanitize(consumer_id)}_{desc.key}"
            )
            if uid in added:
                continue
            added.add(uid)
            if consumer_kind == em.CT002_CONSUMER:
                info = ct002_consumer_device_info(entry, device_id, data)
            else:
                info = shelly_battery_device_info(entry, device_id, consumer_id)
            new.append(
                factory(
                    runtime,
                    desc,
                    scope=SCOPE_CONSUMER,
                    device_info=info,
                    unique_id=uid,
                    consumer_key=key,
                    device_id=device_id,
                )
            )
        if new:
            async_add_entities(new)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass, const.signal_new_consumer(entry.entry_id), _add_consumer
        )
    )
    # Replay consumers already seen (startup race).
    for key in list(runtime.known_consumers):
        _add_consumer(key)
