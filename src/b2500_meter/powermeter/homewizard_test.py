import asyncio
import json
import ssl
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from .homewizard import HomeWizardPowermeter


def _create_powermeter(**overrides):
    defaults = dict(
        ip="192.168.1.1",
        token="ABCD1234",
        serial="aabbccddee",
    )
    defaults.update(overrides)
    return HomeWizardPowermeter(**defaults)


def _ws_text(data: dict) -> aiohttp.WSMessage:
    return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, json.dumps(data), None)


# --- Category A: Measurement parsing ---


def test_measurement_three_phase():
    pm = _create_powermeter()
    pm._handle_measurement(
        {"power_w": -543, "power_l1_w": -200, "power_l2_w": -143, "power_l3_w": -200}
    )
    assert pm.values == [-200, -143, -200]


def test_measurement_single_phase():
    pm = _create_powermeter()
    pm._handle_measurement({"power_w": 500})
    assert pm.values == [500]


def test_measurement_missing_phases():
    pm = _create_powermeter()
    pm._handle_measurement({"power_w": -543, "power_l1_w": -543})
    assert pm.values == [-543, 0, 0]


def test_measurement_no_power_fields():
    pm = _create_powermeter()
    pm._handle_measurement({"energy_import_kwh": 1234.5})
    assert pm.values is None


def test_negative_power_preserved():
    pm = _create_powermeter()
    pm._handle_measurement({"power_w": -1500})
    assert pm.values == [-1500]


def test_measurement_sets_event():
    pm = _create_powermeter()
    assert not pm._message_event.is_set()
    pm._handle_measurement({"power_w": 100})
    assert pm._message_event.is_set()


# --- Category B: Auth/subscribe flow ---


async def test_authorization_requested_sends_token():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(
        ws,
        json.dumps(
            {"type": "authorization_requested", "data": {"api_version": "2.0.0"}}
        ),
    )
    ws.send_json.assert_called_once_with({"type": "authorization", "data": "ABCD1234"})


async def test_authorized_subscribes_to_measurements():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(ws, json.dumps({"type": "authorized"}))
    ws.send_json.assert_called_once_with({"type": "subscribe", "data": "measurement"})


async def test_error_message_does_not_crash():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(
        ws,
        json.dumps({"type": "error", "data": {"message": "user:not-authorized"}}),
    )
    assert pm.values is None


async def test_malformed_json_does_not_crash():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(ws, "not valid json")
    assert pm.values is None


async def test_unknown_message_type_does_not_crash():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(ws, json.dumps({"type": "unknown_type"}))
    assert pm.values is None


async def test_non_dict_json_does_not_crash():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(ws, json.dumps([1, 2, 3]))
    assert pm.values is None
    await pm._handle_message(ws, json.dumps("just a string"))
    assert pm.values is None


async def test_measurement_non_dict_data_ignored():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(
        ws, json.dumps({"type": "measurement", "data": "not a dict"})
    )
    assert pm.values is None


async def test_measurement_message_stores_values():
    pm = _create_powermeter()
    ws = AsyncMock()
    await pm._handle_message(
        ws,
        json.dumps({"type": "measurement", "data": {"power_w": 500}}),
    )
    assert await pm.get_powermeter_watts() == [500]


# --- Category C: SSL context ---


def test_ssl_context_verify_enabled():
    pm = _create_powermeter()
    ctx = pm._build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_ssl_context_verify_disabled():
    pm = _create_powermeter(verify_ssl=False)
    ctx = pm._build_ssl_context()
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


# --- Category D: get_powermeter_watts ---


async def test_get_watts_no_data_raises():
    pm = _create_powermeter()
    with pytest.raises(ValueError):
        await pm.get_powermeter_watts()


async def test_get_watts_returns_copy():
    pm = _create_powermeter()
    pm._handle_measurement({"power_w": 100})
    result = await pm.get_powermeter_watts()
    result.append(999)
    assert await pm.get_powermeter_watts() == [100]


# --- Category E: wait_for_message ---


async def test_wait_for_message_returns_when_data_available():
    pm = _create_powermeter()
    pm._handle_measurement({"power_w": 100})
    await pm.wait_for_message(timeout=1)


async def test_wait_for_message_timeout():
    pm = _create_powermeter()
    with pytest.raises(TimeoutError):
        await pm.wait_for_message(timeout=0)


# --- Category F: Lifecycle ---


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
    await pm.stop()


async def test_start_resets_stale_state():
    pm = _create_powermeter()
    # Simulate leftover state from a previous session
    pm.values = [999.0]
    pm._message_event.set()

    with patch.object(pm, "_ws_loop", new_callable=AsyncMock) as mock_loop:
        mock_loop.return_value = None
        await pm.start()
        assert pm.values is None
        assert not pm._message_event.is_set()
        await pm.stop()


# --- Category G: Full WS flow ---


class _FakeWs:
    """A fake ws that yields given messages and records send_json calls."""

    def __init__(self, messages):
        self._messages = messages
        self.send_json = AsyncMock()

    async def __aiter__(self):
        for msg in self._messages:
            yield msg


async def test_full_auth_subscribe_measurement_flow():
    pm = _create_powermeter()

    messages = [
        _ws_text({"type": "authorization_requested", "data": {"api_version": "2.0.0"}}),
        _ws_text({"type": "authorized"}),
        _ws_text({"type": "measurement", "data": {"power_w": 500}}),
    ]
    ws = _FakeWs(messages)

    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await pm._handle_message(ws, msg.data)

    ws.send_json.assert_any_call({"type": "authorization", "data": "ABCD1234"})
    ws.send_json.assert_any_call({"type": "subscribe", "data": "measurement"})
    assert await pm.get_powermeter_watts() == [500]


async def test_close_message_exits_iteration():
    pm = _create_powermeter()

    messages = [
        _ws_text({"type": "authorized"}),
        aiohttp.WSMessage(aiohttp.WSMsgType.CLOSE, None, None),
        _ws_text({"type": "measurement", "data": {"power_w": 500}}),
    ]
    ws = _FakeWs(messages)

    processed = []
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            await pm._handle_message(ws, msg.data)
            processed.append(msg)
        elif msg.type in (
            aiohttp.WSMsgType.CLOSE,
            aiohttp.WSMsgType.CLOSING,
            aiohttp.WSMsgType.CLOSED,
            aiohttp.WSMsgType.ERROR,
        ):
            break

    assert len(processed) == 1
    assert pm.values is None


# --- Category H: Reconnection ---


def _mock_ws_context(ws):
    """Create an async context manager that yields *ws*."""
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=ws)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx


def _empty_ws():
    """Create a mock ws whose async iteration ends immediately."""
    ws = AsyncMock()

    async def empty_aiter():
        return
        yield  # pragma: no cover — makes this an async generator

    ws.__aiter__ = empty_aiter
    return ws


async def test_ws_loop_reconnects_after_disconnect():
    pm = _create_powermeter()
    pm._session = MagicMock(spec=aiohttp.ClientSession)

    call_count = 0

    def fake_ws_connect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise asyncio.CancelledError
        return _mock_ws_context(_empty_ws())

    pm._session.ws_connect = fake_ws_connect

    with (
        patch(
            "b2500_meter.powermeter.homewizard.asyncio.sleep", new_callable=AsyncMock
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await pm._ws_loop()

    assert call_count == 2


async def test_ws_loop_reconnects_on_client_error():
    pm = _create_powermeter()
    pm._session = MagicMock(spec=aiohttp.ClientSession)

    call_count = 0

    def fake_ws_connect(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise aiohttp.ClientError("connection failed")
        raise asyncio.CancelledError

    pm._session.ws_connect = fake_ws_connect

    with (
        patch(
            "b2500_meter.powermeter.homewizard.asyncio.sleep", new_callable=AsyncMock
        ) as mock_sleep,
        pytest.raises(asyncio.CancelledError),
    ):
        await pm._ws_loop()

    assert call_count == 2
    mock_sleep.assert_called_with(5)
