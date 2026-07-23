import math

import pytest
from aioesphomeapi import SensorInfo, SensorState

from .esphome_native import ESPHomeNative

# ---------------------------------------------------------------------------
# ESPHomeNative async unit tests (no device needed)
#
# The class is push-based: ReconnectLogic drives connect/disconnect and the API
# client pushes SensorState updates into ``change_callback``. We drive those
# callbacks directly instead of standing up a real ESPHome device.
# ---------------------------------------------------------------------------

OBJECT_ID = "grid_power"
ENTITY_KEY = 42


def _make_pm(object_id: str = OBJECT_ID) -> ESPHomeNative:
    return ESPHomeNative(
        address="device.local",
        port="6053",
        api_key="",
        object_id=object_id,
        client_info="AstraMeter-Test",
    )


def _subscribed_pm(object_id: str = OBJECT_ID) -> ESPHomeNative:
    """A meter that has already 'discovered' its entity, as connect_callback would."""
    pm = _make_pm(object_id)
    pm.is_connected = True
    pm.entity_info = SensorInfo(key=ENTITY_KEY, object_id=object_id)  # type: ignore[call-arg]
    return pm


def _state(value: float, missing_state: bool = False) -> SensorState:
    return SensorState(  # type: ignore[call-arg]
        key=ENTITY_KEY, state=value, missing_state=missing_state
    )


async def test_no_value_before_message():
    pm = _subscribed_pm()
    assert await pm.get_powermeter_watts() == []


async def test_get_powermeter_watts_returns_latest():
    pm = _subscribed_pm()
    pm.change_callback(_state(123.0))
    assert await pm.get_powermeter_watts() == [123.0]
    pm.change_callback(_state(456.0))
    assert await pm.get_powermeter_watts() == [456.0]


async def test_change_callback_ignores_other_entities():
    pm = _subscribed_pm()
    pm.change_callback(SensorState(key=ENTITY_KEY + 1, state=999.0))  # type: ignore[call-arg]
    assert await pm.get_powermeter_watts() == []


async def test_change_callback_ignores_before_subscribe():
    pm = _make_pm()  # entity_info is None until connect_callback runs
    pm.change_callback(_state(123.0))
    assert await pm.get_powermeter_watts() == []


async def test_change_callback_drops_missing_state():
    pm = _subscribed_pm()
    pm.change_callback(_state(100.0))
    pm.change_callback(_state(math.nan, missing_state=True))
    # The unavailable update is dropped; the last good value is kept.
    assert await pm.get_powermeter_watts() == [100.0]


async def test_change_callback_drops_nan_without_missing_flag():
    pm = _subscribed_pm()
    pm.change_callback(_state(100.0))
    pm.change_callback(_state(math.nan))
    assert await pm.get_powermeter_watts() == [100.0]


async def test_wait_for_message_returns_after_message():
    pm = _subscribed_pm()
    pm.change_callback(_state(10.0))
    await pm.wait_for_message(timeout=0.1)


async def test_wait_for_message_times_out_without_message():
    pm = _subscribed_pm()
    with pytest.raises(TimeoutError):
        await pm.wait_for_message(timeout=0.05)


async def test_wait_for_next_message_blocks_until_new():
    pm = _subscribed_pm()
    pm.change_callback(_state(10.0))
    # wait_for_next_message must wait for the *next* update, not return on the
    # already-received one.
    with pytest.raises(TimeoutError):
        await pm.wait_for_next_message(timeout=0.05)


async def test_disconnect_clears_value():
    pm = _subscribed_pm()
    pm.change_callback(_state(50.0))
    assert await pm.get_powermeter_watts() == [50.0]

    await pm.disconnect_callback(expected_disconnect=True)

    # After a disconnect the meter reports no value and is offline.
    assert await pm.get_powermeter_watts() == []
    assert pm.stream_online() is False
    assert pm.entity_info is None


async def test_stream_online_reflects_connection():
    pm = _make_pm()
    assert pm.stream_online() is False
    pm.is_connected = True
    assert pm.stream_online() is True


async def test_connect_error_resets_state():
    pm = _subscribed_pm()
    pm.change_callback(_state(50.0))
    await pm.connect_error_callback(RuntimeError("boom"))
    assert pm.stream_online() is False
    assert await pm.get_powermeter_watts() == []
