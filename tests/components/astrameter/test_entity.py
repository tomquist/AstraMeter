"""Device-registry wiring for the native entities.

Guards the consumer→device mapping. Regression: a CT002 reports many batteries
through one CT clamp; the clamp's MAC must not be used as a per-battery device
connection or Home Assistant merges every battery into a single device.
"""

from __future__ import annotations

from custom_components.astrameter import const
from custom_components.astrameter.entity import ct002_consumer_device_info
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant_custom_component.common import MockConfigEntry


def _event(consumer_id: str, ip: str) -> dict:
    # Same shared CT clamp MAC for both batteries (as in a real install).
    return {
        "grid_power": {"l1": 250.0, "l2": 0.0, "l3": 0.0, "total": 250.0},
        "device_type": "HMG-50",
        "ct_mac": "112233445566",
        "battery_ip": ip,
        "_consumer_id": consumer_id,
        "phase": "A",
        "active": True,
    }


def test_consumer_device_info_does_not_share_ct_mac() -> None:
    """Two batteries on one CT must not share any device connection tuple."""
    a = ct002_consumer_device_info(
        None, "ct002_1", _event("aabbccddeeff", "192.168.1.10")
    )
    b = ct002_consumer_device_info(
        None, "ct002_1", _event("aabbccdd1122", "192.168.1.11")
    )

    assert a["identifiers"] != b["identifiers"]
    ca = a.get("connections", set())
    cb = b.get("connections", set())
    # The shared CT clamp MAC must never appear as a per-battery connection.
    assert ("mac", "112233445566") not in ca
    assert ("mac", "112233445566") not in cb
    # No overlap → HA keeps them as two distinct devices.
    assert not (ca & cb), f"shared connections would merge the devices: {ca & cb}"
    # Each battery still carries its own bluetooth MAC + IP.
    assert ("bluetooth", "AA:BB:CC:DD:EE:FF") in ca
    assert ("ip", "192.168.1.10") in ca


async def test_two_consumers_create_two_devices(
    hass: HomeAssistant, socket_enabled
) -> None:
    """End to end: two batteries on one CT yield two consumer devices."""
    hass.states.async_set("sensor.grid_power", "250")
    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id="ct002_e2e",
        data={
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_CT002,
            const.CONF_UDP_PORT: 0,  # ephemeral; we drive events directly
            const.CONF_DEVICE_ID: "ct002_e2e",
            const.CONF_PAIR_MODE: False,
            const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
        },
        options={const.CONF_ACTIVE_CONTROL: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    runtime = hass.data[const.DOMAIN][entry.entry_id]
    runtime._on_event(
        "ct002_e2e", "aabbccddeeff", _event("aabbccddeeff", "192.168.1.10")
    )
    runtime._on_event(
        "ct002_e2e", "aabbccdd1122", _event("aabbccdd1122", "192.168.1.11")
    )
    await hass.async_block_till_done()

    devreg = dr.async_get(hass)
    consumer_devs = [
        d
        for d in dr.async_entries_for_config_entry(devreg, entry.entry_id)
        if any("consumer_" in i[1] for i in d.identifiers)
    ]
    assert len(consumer_devs) == 2, (
        f"expected 2 consumer devices, got {len(consumer_devs)}"
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
