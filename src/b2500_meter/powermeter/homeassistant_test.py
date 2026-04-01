import json
from unittest.mock import AsyncMock, patch

import pytest

from .homeassistant import HomeAssistant


def _create_powermeter(**overrides):
    defaults = dict(
        ip="192.168.1.8",
        port="8123",
        use_https=False,
        access_token="token",
        current_power_entity="sensor.current_power",
        power_calculate=False,
        power_input_alias="",
        power_output_alias="",
        path_prefix=None,
    )
    defaults.update(overrides)
    return HomeAssistant(**defaults)


def _compressed_initial_payload(states: list[dict]) -> dict:
    """Build subscribe_entities initial `event.a` map (entity_id -> {s: ...})."""
    a: dict = {}
    for s in states:
        eid = s.get("entity_id")
        if not eid:
            continue
        a[eid] = {"s": s.get("state")}
    return {"a": a}


async def _simulate_auth_and_states(pm, states):
    ws = AsyncMock()
    await pm._handle_message(ws, json.dumps({"type": "auth_required"}))
    await pm._handle_message(ws, json.dumps({"type": "auth_ok"}))
    sid = pm._subscribe_entities_id
    await pm._handle_message(
        ws,
        json.dumps(
            {
                "id": sid,
                "type": "event",
                "event": _compressed_initial_payload(states),
            }
        ),
    )
    return ws


# Auth flow tests


async def test_auth_required_sends_token():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(ws, json.dumps({"type": "auth_required"}))
    ws.send_json.assert_called_once_with({"type": "auth", "access_token": "token"})


async def test_auth_ok_subscribes_entities():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(ws, json.dumps({"type": "auth_required"}))
    ws.send_json.reset_mock()

    await pm._handle_message(ws, json.dumps({"type": "auth_ok"}))

    calls = ws.send_json.call_args_list
    assert len(calls) == 1

    subscribe_msg = calls[0][0][0]
    assert subscribe_msg["type"] == "subscribe_entities"
    assert "sensor.current_power" in subscribe_msg["entity_ids"]


async def test_auth_invalid_does_not_crash():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(
        ws,
        json.dumps({"type": "auth_invalid", "message": "bad token"}),
    )
    # Should not raise


# subscribe_entities initial snapshot tests


async def test_initial_snapshot_populates_value():
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "1000"}]
    )
    assert await pm.get_powermeter_watts() == [1000.0]


async def test_no_initial_event_leaves_values_missing():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(ws, json.dumps({"type": "auth_required"}))
    await pm._handle_message(ws, json.dumps({"type": "auth_ok"}))

    with pytest.raises(ValueError):
        await pm.get_powermeter_watts()


async def test_initial_snapshot_only_updates_tracked_entities():
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm,
        [
            {"entity_id": "sensor.current_power", "state": "500"},
            {"entity_id": "sensor.temperature", "state": "22"},
        ],
    )
    assert await pm.get_powermeter_watts() == [500.0]


# Trigger event tests


async def test_trigger_event_updates_value():
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )
    assert await pm.get_powermeter_watts() == [100.0]

    ws = AsyncMock()
    await pm._handle_message(
        ws,
        json.dumps(
            {
                "id": 2,
                "type": "event",
                "event": {
                    "c": {
                        "sensor.current_power": {
                            "+": {"s": "200"},
                        }
                    }
                },
            }
        ),
    )
    assert await pm.get_powermeter_watts() == [200.0]


async def test_trigger_event_ignores_untracked_entity():
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )

    ws = AsyncMock()
    await pm._handle_message(
        ws,
        json.dumps(
            {
                "id": 2,
                "type": "event",
                "event": {
                    "c": {
                        "sensor.other": {
                            "+": {"s": "999"},
                        }
                    }
                },
            }
        ),
    )
    assert await pm.get_powermeter_watts() == [100.0]


# Error condition tests


async def test_sensor_has_no_state():
    pm = _create_powermeter()
    with pytest.raises(ValueError) as exc_info:
        await pm.get_powermeter_watts()

    assert (
        str(exc_info.value) == "Home Assistant sensor sensor.current_power has no state"
    )


async def test_sensor_state_none():
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": None}]
    )

    with pytest.raises(ValueError) as exc_info:
        await pm.get_powermeter_watts()

    assert (
        str(exc_info.value) == "Home Assistant sensor sensor.current_power has no state"
    )


async def test_sensor_state_not_numeric():
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm,
        [{"entity_id": "sensor.current_power", "state": "unavailable"}],
    )

    with pytest.raises(ValueError) as exc_info:
        await pm.get_powermeter_watts()

    assert (
        str(exc_info.value) == "Home Assistant sensor sensor.current_power has no state"
    )


async def test_malformed_json_message():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(ws, "not valid json")
    # Should not raise; value stays absent
    with pytest.raises(ValueError):
        await pm.get_powermeter_watts()


# Three-phase tests


async def test_three_phase_direct():
    pm = _create_powermeter(
        current_power_entity=[
            "sensor.power_phase1",
            "sensor.power_phase2",
            "sensor.power_phase3",
        ]
    )
    await _simulate_auth_and_states(
        pm,
        [
            {"entity_id": "sensor.power_phase1", "state": "100"},
            {"entity_id": "sensor.power_phase2", "state": "200"},
            {"entity_id": "sensor.power_phase3", "state": "300"},
        ],
    )
    assert await pm.get_powermeter_watts() == [100.0, 200.0, 300.0]


# Power calculate tests


async def test_power_calculate_mode():
    pm = _create_powermeter(
        current_power_entity="",
        power_calculate=True,
        power_input_alias="sensor.power_input",
        power_output_alias="sensor.power_output",
    )
    await _simulate_auth_and_states(
        pm,
        [
            {"entity_id": "sensor.power_input", "state": "1000"},
            {"entity_id": "sensor.power_output", "state": "200"},
        ],
    )
    assert await pm.get_powermeter_watts() == [800.0]


async def test_three_phase_calculated():
    pm = _create_powermeter(
        current_power_entity="",
        power_calculate=True,
        power_input_alias=[
            "sensor.power_in_1",
            "sensor.power_in_2",
            "sensor.power_in_3",
        ],
        power_output_alias=[
            "sensor.power_out_1",
            "sensor.power_out_2",
            "sensor.power_out_3",
        ],
    )
    await _simulate_auth_and_states(
        pm,
        [
            {"entity_id": "sensor.power_in_1", "state": "1000"},
            {"entity_id": "sensor.power_out_1", "state": "200"},
            {"entity_id": "sensor.power_in_2", "state": "2000"},
            {"entity_id": "sensor.power_out_2", "state": "300"},
            {"entity_id": "sensor.power_in_3", "state": "3000"},
            {"entity_id": "sensor.power_out_3", "state": "400"},
        ],
    )
    assert await pm.get_powermeter_watts() == [800.0, 1700.0, 2600.0]


async def test_power_alias_length_mismatch():
    pm = _create_powermeter(
        current_power_entity="",
        power_calculate=True,
        power_input_alias=["sensor.power_in_1", "sensor.power_in_2"],
        power_output_alias=["sensor.power_out_1"],
    )
    await _simulate_auth_and_states(
        pm,
        [
            {"entity_id": "sensor.power_in_1", "state": "100"},
            {"entity_id": "sensor.power_in_2", "state": "200"},
            {"entity_id": "sensor.power_out_1", "state": "50"},
        ],
    )

    with pytest.raises(ValueError) as exc_info:
        await pm.get_powermeter_watts()

    assert (
        str(exc_info.value)
        == "Home Assistant power_input_alias and power_output_alias lengths differ"
    )


# WebSocket URL tests


def test_ws_url_http():
    pm = _create_powermeter()
    assert pm._build_ws_url() == "ws://192.168.1.8:8123/api/websocket"


def test_ws_url_https():
    pm = _create_powermeter(use_https=True)
    assert pm._build_ws_url() == "wss://192.168.1.8:8123/api/websocket"


def test_ws_url_with_path_prefix():
    pm = _create_powermeter(path_prefix="/prefix")
    assert pm._build_ws_url() == "ws://192.168.1.8:8123/prefix/api/websocket"


# wait_for_message tests


async def test_wait_for_message_returns_when_data_available():
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )
    # Should return immediately, not raise
    await pm.wait_for_message(timeout=1)


async def test_wait_for_message_timeout():
    pm = _create_powermeter()
    with pytest.raises(TimeoutError):
        await pm.wait_for_message(timeout=0)


# subscribe_entities entity list test


async def test_subscribe_entities_contains_all_entities_calculate_mode():
    pm = _create_powermeter(
        current_power_entity="",
        power_calculate=True,
        power_input_alias="sensor.power_input",
        power_output_alias="sensor.power_output",
    )
    ws = AsyncMock()
    await pm._handle_message(ws, json.dumps({"type": "auth_required"}))
    ws.send_json.reset_mock()
    await pm._handle_message(ws, json.dumps({"type": "auth_ok"}))

    subscribe_msg = ws.send_json.call_args_list[0][0][0]
    entity_ids = subscribe_msg["entity_ids"]
    assert "sensor.power_input" in entity_ids
    assert "sensor.power_output" in entity_ids


# Lifecycle tests


async def test_start_creates_session_and_task():
    pm = _create_powermeter()
    with patch.object(pm, "_ws_loop", new_callable=AsyncMock) as mock_loop:
        mock_loop.return_value = None
        await pm.start()
        assert pm._session is not None
        assert pm._ws_task is not None
        await pm.stop()


async def test_start_is_idempotent():
    pm = _create_powermeter()
    with patch.object(pm, "_ws_loop", new_callable=AsyncMock) as mock_loop:
        mock_loop.return_value = None
        await pm.start()
        session1 = pm._session
        await pm.start()
        assert pm._session is session1
        await pm.stop()


async def test_stop_closes_session():
    pm = _create_powermeter()
    with patch.object(pm, "_ws_loop", new_callable=AsyncMock) as mock_loop:
        mock_loop.return_value = None
        await pm.start()
        await pm.stop()
        assert pm._session is None
        assert pm._ws_task is None


async def test_stop_without_start():
    pm = _create_powermeter()
    # Should not raise
    await pm.stop()


# entities_ready event tests


async def test_entities_ready_set_when_all_present():
    pm = _create_powermeter()
    assert not pm._entities_ready.is_set()
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )
    assert pm._entities_ready.is_set()


async def test_entities_ready_cleared_when_value_becomes_none():
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )
    assert pm._entities_ready.is_set()

    ws = AsyncMock()
    await pm._handle_message(
        ws,
        json.dumps(
            {
                "type": "event",
                "event": {
                    "c": {
                        "sensor.current_power": {
                            "+": {"s": "unavailable"},
                        }
                    }
                },
            }
        ),
    )
    assert not pm._entities_ready.is_set()
