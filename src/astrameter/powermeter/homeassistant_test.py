import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any
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


# wait_for_next_message tests


async def test_wait_for_next_message_blocks_until_new():
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )

    async def _push_later():
        await asyncio.sleep(0.05)
        pm._update_entity_value("sensor.current_power", "200")

    task = asyncio.create_task(_push_later())
    await pm.wait_for_next_message(timeout=2)
    await task
    assert await pm.get_powermeter_watts() == [200.0]


async def test_wait_for_next_message_timeout():
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )
    with pytest.raises(TimeoutError):
        await pm.wait_for_next_message(timeout=0)


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


# --- Staleness detection ---------------------------------------------------


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def test_stale_state_raises_in_get_powermeter_watts():
    """Regression: if the websocket feed goes silent (half-open TCP or
    stuck template sensor) the cached state age crosses
    ``max_state_age_seconds`` and :meth:`get_powermeter_watts` must
    raise instead of silently serving the frozen value.
    """
    clock = _FakeClock()
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )
    assert await pm.get_powermeter_watts() == [100.0]

    clock.advance(29.0)
    assert await pm.get_powermeter_watts() == [100.0]

    clock.advance(2.0)
    with pytest.raises(ValueError, match="stale"):
        await pm.get_powermeter_watts()


async def test_fresh_state_clears_staleness():
    clock = _FakeClock()
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )
    clock.advance(50.0)
    with pytest.raises(ValueError, match="stale"):
        await pm.get_powermeter_watts()

    ws = AsyncMock()
    await pm._handle_message(
        ws,
        json.dumps(
            {
                "type": "event",
                "event": {
                    "c": {
                        "sensor.current_power": {
                            "+": {"s": "250"},
                        }
                    }
                },
            }
        ),
    )
    assert await pm.get_powermeter_watts() == [250.0]


async def test_state_reported_event_refreshes_staleness():
    """Regression for issue #363: HA's ``subscribe_entities`` only includes
    ``s`` in the diff when the state value actually changes. For sensors
    whose value stays constant (e.g. solar production on an unused phase)
    HA still pushes state_reported events, but their compressed diff only
    carries an updated ``lu`` (last_updated timestamp). Treat those as
    keepalives so the staleness check does not falsely fire on legitimately
    constant sensors.
    """
    clock = _FakeClock()
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "0"}]
    )
    assert await pm.get_powermeter_watts() == [0.0]

    # 29 s in, an integration push reports the same value. HA emits a
    # state_reported diff with only ``lu`` populated.
    clock.advance(29.0)
    ws = AsyncMock()
    await pm._handle_message(
        ws,
        json.dumps(
            {
                "type": "event",
                "event": {
                    "c": {"sensor.current_power": {"+": {"lu": 1000.0}}},
                },
            }
        ),
    )

    # The keepalive refreshed liveness; another 29 s should still be fresh.
    clock.advance(29.0)
    assert await pm.get_powermeter_watts() == [0.0]

    # But continued silence past the threshold still trips staleness.
    clock.advance(2.0)
    with pytest.raises(ValueError, match="stale"):
        await pm.get_powermeter_watts()


async def test_state_reported_event_sets_message_event():
    """``wait_for_next_message`` must wake on state_reported keepalives —
    otherwise the Shelly / CT002 emulator's per-request fresh-push wait
    times out for sensors whose value doesn't change between polls.
    """
    pm = _create_powermeter()
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "0"}]
    )
    pm._message_event.clear()
    waiter = asyncio.create_task(pm.wait_for_next_message(timeout=1))
    await asyncio.sleep(0)

    ws = AsyncMock()
    await pm._handle_message(
        ws,
        json.dumps(
            {
                "type": "event",
                "event": {
                    "c": {"sensor.current_power": {"+": {"lu": 1000.0}}},
                },
            }
        ),
    )
    await waiter  # would raise TimeoutError if state_reported didn't wake it


async def test_state_reported_before_initial_state_is_ignored():
    """If a state_reported keepalive arrives before any state value, the
    entity must remain ``None`` — we cannot claim liveness for an entity
    we have never received a value for.
    """
    clock = _FakeClock()
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    ws = AsyncMock()
    await pm._handle_message(ws, json.dumps({"type": "auth_required"}))
    await pm._handle_message(ws, json.dumps({"type": "auth_ok"}))
    await pm._handle_message(
        ws,
        json.dumps(
            {
                "id": pm._subscribe_entities_id,
                "type": "event",
                "event": {
                    "c": {"sensor.current_power": {"+": {"lu": 1000.0}}},
                },
            }
        ),
    )
    assert pm._entity_values.get("sensor.current_power") is None
    assert pm._entity_update_time.get("sensor.current_power") is None


async def test_max_state_age_zero_disables_check():
    clock = _FakeClock()
    pm = _create_powermeter(max_state_age_seconds=0.0, clock=clock)
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )
    clock.advance(100000.0)
    assert await pm.get_powermeter_watts() == [100.0]


async def test_reconnect_clears_entity_update_times_and_ready_flag():
    """A websocket disconnect must clear the entity update times and
    the ``_entities_ready`` event, so ``get_powermeter_watts`` raises
    and ``wait_for_message`` blocks again until fresh state arrives
    from the reconnect's ``subscribe_entities`` snapshot.
    """
    clock = _FakeClock()
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "100"}]
    )
    assert pm._entities_ready.is_set()
    assert await pm.get_powermeter_watts() == [100.0]

    # Simulate exactly what ``_ws_loop`` does in its reconnect block.
    pm._msg_id = 0
    pm._subscribe_entities_id = None
    for eid in list(pm._entity_update_time):
        pm._entity_update_time[eid] = None
    pm._entities_ready.clear()

    assert not pm._entities_ready.is_set()
    with pytest.raises(ValueError):
        await pm.get_powermeter_watts()

    # Fresh state arrives from the reconnected subscribe_entities
    # snapshot — both signals recover.
    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "250"}]
    )
    assert pm._entities_ready.is_set()
    assert await pm.get_powermeter_watts() == [250.0]


# --- REST fallback for state_reported -------------------------------------


class _FakeResponse:
    def __init__(self, status: int, data: Any) -> None:
        self.status = status
        self._data = data

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def json(self) -> Any:
        return self._data


class _FakeSession:
    """Minimal stand-in for ``aiohttp.ClientSession`` covering ``get``."""

    def __init__(self) -> None:
        self.responses: dict[str, _FakeResponse | Exception] = {}
        self.requested: list[str] = []
        self.delay: float = 0.0

    def set_state(
        self,
        url: str,
        *,
        state: str,
        last_reported: datetime | None,
        status: int = 200,
    ) -> None:
        payload: dict[str, Any] = {"state": state}
        if last_reported is not None:
            payload["last_reported"] = last_reported.isoformat()
        self.responses[url] = _FakeResponse(status, payload)

    def set_error(self, url: str, exc: Exception) -> None:
        self.responses[url] = exc

    def get(
        self, url: str, headers: dict[str, str] | None = None
    ) -> "_DelayedResponse":
        return _DelayedResponse(self, url)


class _DelayedResponse:
    """Awaitable context manager that records the URL and may sleep."""

    def __init__(self, session: _FakeSession, url: str) -> None:
        self._session = session
        self._url = url

    async def __aenter__(self) -> _FakeResponse:
        self._session.requested.append(self._url)
        if self._session.delay:
            await asyncio.sleep(self._session.delay)
        entry = self._session.responses.get(self._url)
        if isinstance(entry, Exception):
            raise entry
        if entry is None:
            return _FakeResponse(404, {})
        return entry

    async def __aexit__(self, *_: object) -> None:
        return None


def _state_url(pm: HomeAssistant, entity_id: str) -> str:
    return pm._build_rest_state_url(entity_id)


async def test_rest_fallback_refreshes_stale_entity_via_last_reported():
    """Regression for issue #363: a sensor whose value is constant (e.g.
    solar production on an unloaded phase) produces no ``state_changed``
    pushes, so the local push timer drifts past ``max_state_age_seconds``
    even though HA itself is current. ``get_powermeter_watts`` must REST-
    poll ``/api/states/{entity}`` and use HA's authoritative
    ``last_reported`` to confirm freshness.
    """
    clock = _FakeClock(start=10_000.0)
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    session = _FakeSession()
    pm._session = session  # type: ignore[assignment]

    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "0"}]
    )
    clock.advance(45.0)
    session.set_state(
        _state_url(pm, "sensor.current_power"),
        state="0",
        last_reported=datetime.now(timezone.utc),
    )

    assert await pm.get_powermeter_watts() == [0.0]
    assert session.requested == [_state_url(pm, "sensor.current_power")]


async def test_rest_fallback_raises_when_ha_last_reported_is_truly_stale():
    """If HA's own ``last_reported`` is older than the staleness window
    (the sensor's source has actually gone silent), don't refresh the
    local cache — let the staleness check raise.
    """
    clock = _FakeClock(start=10_000.0)
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    session = _FakeSession()
    pm._session = session  # type: ignore[assignment]

    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "0"}]
    )
    clock.advance(45.0)
    session.set_state(
        _state_url(pm, "sensor.current_power"),
        state="0",
        last_reported=datetime.now(timezone.utc) - timedelta(minutes=5),
    )

    with pytest.raises(ValueError, match="stale"):
        await pm.get_powermeter_watts()


async def test_rest_fallback_only_polls_stale_entities():
    """Fresh entities must not be re-fetched — only those past the local
    push threshold.
    """
    clock = _FakeClock(start=10_000.0)
    pm = _create_powermeter(
        power_calculate=True,
        power_input_alias=["sensor.in_a", "sensor.in_b"],
        power_output_alias=["sensor.out_a", "sensor.out_b"],
        max_state_age_seconds=30.0,
        clock=clock,
    )
    session = _FakeSession()
    pm._session = session  # type: ignore[assignment]

    await _simulate_auth_and_states(
        pm,
        [
            {"entity_id": "sensor.in_a", "state": "100"},
            {"entity_id": "sensor.in_b", "state": "200"},
            {"entity_id": "sensor.out_a", "state": "0"},
            {"entity_id": "sensor.out_b", "state": "50"},
        ],
    )

    # Only sensor.out_a stays silent; the other three get fresh pushes.
    clock.advance(20.0)
    await pm._handle_message(
        AsyncMock(),
        json.dumps(
            {
                "type": "event",
                "event": {
                    "c": {
                        "sensor.in_a": {"+": {"s": "110"}},
                        "sensor.in_b": {"+": {"s": "210"}},
                        "sensor.out_b": {"+": {"s": "60"}},
                    }
                },
            }
        ),
    )
    clock.advance(20.0)  # in_*/out_b: 20 s ago; out_a: 40 s ago

    session.set_state(
        _state_url(pm, "sensor.out_a"),
        state="0",
        last_reported=datetime.now(timezone.utc),
    )

    await pm.get_powermeter_watts()
    assert session.requested == [_state_url(pm, "sensor.out_a")]


async def test_rest_fallback_bounded_by_timeout():
    """The REST fallback budget is total wall-clock across all stale
    entities, not per-entity. If the network is slow enough that the
    deadline expires before any response, the local cache is left
    unrefreshed and the subsequent staleness check raises.
    """
    clock = _FakeClock(start=10_000.0)
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    session = _FakeSession()
    session.delay = 5.0  # well past the budget
    pm._session = session  # type: ignore[assignment]

    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "0"}]
    )
    clock.advance(45.0)
    session.set_state(
        _state_url(pm, "sensor.current_power"),
        state="0",
        last_reported=datetime.now(timezone.utc),
    )

    start = asyncio.get_event_loop().time()
    await pm._refresh_stale_via_rest(timeout=0.05)
    elapsed = asyncio.get_event_loop().time() - start
    # Bounded by ~0.05 s — definitely well under the response's 5 s delay.
    assert elapsed < 0.5
    # Cache wasn't refreshed (response never returned), so the staleness
    # check still raises with the original error.
    with pytest.raises(ValueError, match="stale"):
        await pm._refresh_stale_via_rest(timeout=0.05)
        # Don't call get_powermeter_watts here — it would re-trigger
        # the (slow) refresh with the default 1 s budget.
        pm._get_entity_value("sensor.current_power")


async def test_rest_fallback_ignores_unavailable_state():
    """``state: "unavailable"`` (or "unknown") from REST is not a fresh
    value — leave the local cache stale and raise.
    """
    clock = _FakeClock(start=10_000.0)
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    session = _FakeSession()
    pm._session = session  # type: ignore[assignment]

    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "0"}]
    )
    clock.advance(45.0)
    session.set_state(
        _state_url(pm, "sensor.current_power"),
        state="unavailable",
        last_reported=datetime.now(timezone.utc),
    )

    with pytest.raises(ValueError, match="stale"):
        await pm.get_powermeter_watts()


async def test_rest_fallback_swallows_http_errors():
    """Network/HTTP errors during the REST fallback must not propagate —
    the staleness check raises with the original "stale" message instead,
    so the caller's exception handling stays simple.
    """
    clock = _FakeClock(start=10_000.0)
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    session = _FakeSession()
    pm._session = session  # type: ignore[assignment]
    import aiohttp  # local import: only this test needs the type

    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "0"}]
    )
    clock.advance(45.0)
    session.set_error(
        _state_url(pm, "sensor.current_power"),
        aiohttp.ClientConnectionError("boom"),
    )

    with pytest.raises(ValueError, match="stale"):
        await pm.get_powermeter_watts()


async def test_rest_fallback_does_not_clobber_concurrent_websocket_push():
    """If a websocket push lands while a REST refresh for the same entity
    is in flight, the WS value (which is at least as fresh as anything
    REST can return) must win — REST must not overwrite it.
    """
    clock = _FakeClock(start=10_000.0)
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=clock)
    session = _FakeSession()
    pm._session = session  # type: ignore[assignment]

    await _simulate_auth_and_states(
        pm, [{"entity_id": "sensor.current_power", "state": "0"}]
    )
    clock.advance(45.0)  # entity is now locally stale

    # REST response advertises a fresh last_reported, but a websocket
    # push will arrive while the REST round-trip is in flight.
    session.delay = 0.05
    session.set_state(
        _state_url(pm, "sensor.current_power"),
        state="0",
        last_reported=datetime.now(timezone.utc),
    )

    refresh_task = asyncio.create_task(pm._refresh_stale_via_rest(timeout=1.0))
    # Spin until _fetch_rest_state has snapshotted pre_update and is
    # parked inside the FakeSession's delay (URL recorded).
    for _ in range(50):
        if session.requested:
            break
        await asyncio.sleep(0)
    assert session.requested, "REST request did not start"
    # Concurrent WS push with a different value.
    await pm._handle_message(
        AsyncMock(),
        json.dumps(
            {
                "type": "event",
                "event": {
                    "c": {"sensor.current_power": {"+": {"s": "123"}}},
                },
            }
        ),
    )
    await refresh_task

    # REST returned "0" but the WS push of 123 happened during the
    # round-trip; the guard must skip the REST apply.
    assert pm._entity_values["sensor.current_power"] == 123.0


async def test_rest_fallback_url_respects_path_prefix_and_scheme():
    pm = _create_powermeter(
        ip="example.test",
        port="8123",
        use_https=True,
        path_prefix="/core",
    )
    assert (
        pm._build_rest_state_url("sensor.foo")
        == "https://example.test:8123/core/api/states/sensor.foo"
    )


async def test_rest_fallback_noop_when_no_session():
    """Without a live session (no ``start()``), the REST refresh is a
    silent no-op so unit tests that drive the WebSocket handlers directly
    don't accidentally hit the network.
    """
    pm = _create_powermeter(max_state_age_seconds=30.0, clock=_FakeClock())
    # _session is None by default.
    await pm._refresh_stale_via_rest()  # must not raise
