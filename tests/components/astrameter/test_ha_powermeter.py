"""Unit tests for HAStatePowermeter (grid power from HA entity state)."""

from __future__ import annotations

import asyncio

import pytest
from custom_components.astrameter.ha_powermeter import HAStatePowermeter
from homeassistant.core import HomeAssistant


async def test_single_entity(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.grid", "123.5")
    pm = HAStatePowermeter(hass, ["sensor.grid"])
    await pm.start()
    try:
        assert await pm.get_powermeter_watts() == [123.5]
        assert pm.stream_online() is True
    finally:
        await pm.stop()


async def test_three_phase(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.l1", "10")
    hass.states.async_set("sensor.l2", "20")
    hass.states.async_set("sensor.l3", "30")
    pm = HAStatePowermeter(hass, ["sensor.l1", "sensor.l2", "sensor.l3"])
    await pm.start()
    try:
        assert await pm.get_powermeter_watts() == [10.0, 20.0, 30.0]
    finally:
        await pm.stop()


async def test_input_output_pair(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.imp", "500")
    hass.states.async_set("sensor.exp", "200")
    pm = HAStatePowermeter(
        hass,
        power_calculate=True,
        input_entities=["sensor.imp"],
        output_entities=["sensor.exp"],
    )
    await pm.start()
    try:
        assert await pm.get_powermeter_watts() == [300.0]
    finally:
        await pm.stop()


async def test_unavailable_has_no_value(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.grid", "unavailable")
    pm = HAStatePowermeter(hass, ["sensor.grid"])
    await pm.start()
    try:
        assert pm.stream_online() is False
        with pytest.raises(ValueError):
            await pm.get_powermeter_watts()
    finally:
        await pm.stop()


async def test_state_change_wakes_next_message(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.grid", "100")
    pm = HAStatePowermeter(hass, ["sensor.grid"])
    await pm.start()
    try:
        waiter = asyncio.create_task(pm.wait_for_next_message(timeout=2))
        await asyncio.sleep(0)
        hass.states.async_set("sensor.grid", "101")
        await waiter  # should not raise TimeoutError
        assert await pm.get_powermeter_watts() == [101.0]
    finally:
        await pm.stop()


async def test_going_unavailable_flips_online(hass: HomeAssistant) -> None:
    hass.states.async_set("sensor.grid", "100")
    pm = HAStatePowermeter(hass, ["sensor.grid"])
    await pm.start()
    try:
        assert pm.stream_online() is True
        hass.states.async_set("sensor.grid", "unavailable")
        await hass.async_block_till_done()
        assert pm.stream_online() is False
    finally:
        await pm.stop()
