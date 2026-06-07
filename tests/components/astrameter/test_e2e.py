"""End-to-end: real HA Core + a real CT002 + the BatterySimulator over UDP.

No mocks on the device path. Sets up the integration on an ephemeral UDP port so
the real ``CT002`` binds it, drives the repo's ``BatterySimulator`` against it
over loopback, and asserts native entities appear with correct values and that
controls take effect.
"""

from __future__ import annotations

import asyncio
import socket

import pytest
from custom_components.astrameter import const
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from astrameter.simulator.battery import BatterySimulator

pytestmark = pytest.mark.astrameter_e2e


def _free_udp_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _drive(
    hass: HomeAssistant, battery: BatterySimulator, steps: int = 12
) -> list[str] | None:
    """Drive the battery while a background task refreshes the grid sensor, so
    the push-based HAStatePowermeter's ``wait_for_next_message`` returns promptly
    (a real grid sensor changes continuously; a static value would stall it for
    the 2s cap, past the simulator's recv timeout)."""
    stop = asyncio.Event()

    async def _vary() -> None:
        i = 0
        while not stop.is_set():
            hass.states.async_set("sensor.grid_power", str(250 + (i % 2)))
            i += 1
            await asyncio.sleep(0.1)

    vary = asyncio.create_task(_vary())
    last: list[str] | None = None
    try:
        for _ in range(steps):
            result = await battery.step(dt=0.1)
            if result is not None:
                last = result
            await asyncio.sleep(0.02)
    finally:
        stop.set()
        await vary
    return last


def _find_state(hass: HomeAssistant, entry_id: str, suffix: str) -> str | None:
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry_id):
        if ent.unique_id.endswith(suffix) and "consumer" in ent.unique_id:
            state = hass.states.get(ent.entity_id)
            return state.state if state else None
    return None


async def test_ct002_e2e_entities_and_control(
    hass: HomeAssistant, socket_enabled
) -> None:
    port = _free_udp_port()
    hass.states.async_set("sensor.grid_power", "250")

    entry = MockConfigEntry(
        domain=const.DOMAIN,
        unique_id=f"ct002_{port}",
        data={
            const.CONF_DEVICE_TYPE: const.DEVICE_TYPE_CT002,
            const.CONF_UDP_PORT: port,
            const.CONF_DEVICE_ID: f"ct002_{port}",
            const.CONF_PAIR_MODE: False,
            const.CONF_GRID_ENTITIES: ["sensor.grid_power"],
        },
        options={const.CONF_ACTIVE_CONTROL: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state.recoverable is False or entry.state.name == "LOADED"

    battery = BatterySimulator(
        mac="AABBCCDDEEFF",
        phase="A",
        ct_mac="112233445566",
        ct_host="127.0.0.1",
        ct_port=port,
        poll_interval=1.0,
        startup_delay=0.0,
    )
    fields = await _drive(hass, battery)
    await hass.async_block_till_done()
    assert fields is not None, "BatterySimulator never received a response"

    # A consumer device and its primary grid-power sensor should now exist.
    grid_total = _find_state(hass, entry.entry_id, "_grid_power_total")
    assert grid_total is not None, "consumer grid_power_total sensor not created"
    assert float(grid_total) == pytest.approx(250.0, abs=1.5)

    # No standalone "powermeter" health device is exposed natively: the grid
    # source is a HA sensor whose availability is already visible.
    registry = er.async_get(hass)
    online_ids = [
        e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id.endswith("_online")
    ]
    assert not online_ids, "powermeter device should not be created natively"

    # Control: turn the consumer's Active switch off via the service.
    switch_ids = [
        e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
        if e.unique_id.endswith("_active") and "consumer" in e.unique_id
    ]
    assert switch_ids, "consumer active switch not created"
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": switch_ids[0]}, blocking=True
    )
    await hass.async_block_till_done()

    # Unload cleanly.
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
