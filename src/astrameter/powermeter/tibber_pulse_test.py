import asyncio
import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientResponseError, web
from aiohttp.test_utils import TestServer

from astrameter.powermeter import TibberPulse, tibber_pulse
from astrameter.powermeter.sml_test import _build_sml_frame
from astrameter.powermeter.tibber_pulse import split_push_frame


def _poller(*args: Any, **kwargs: Any) -> TibberPulse:
    """A source that only polls: these tests drive a mocked HTTP session."""
    return TibberPulse(*args, force_polling=True, **kwargs)


async def test_get_powermeter_watts_decodes_multiphase(
    mock_aiohttp_session: MagicMock,
) -> None:
    frame = _build_sml_frame(power_agg=1234, power_l1=400, power_l2=500, power_l3=334)
    mock_aiohttp_session.set_read(frame)
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        pm = _poller("127.0.0.1", "AD56-54BA")
        await pm.start()
        # Per-phase preferred over aggregate when all three phases are present.
        assert await pm.get_powermeter_watts() == [400.0, 500.0, 334.0]
        await pm.stop()


async def test_get_powermeter_watts_builds_authenticated_url(
    mock_aiohttp_session: MagicMock,
) -> None:
    frame = _build_sml_frame(power_l1=100, power_l2=200, power_l3=300)
    mock_aiohttp_session.set_read(frame)
    with patch(
        "aiohttp.ClientSession", return_value=mock_aiohttp_session
    ) as session_cls:
        pm = _poller("10.0.0.5", "pw", node_id="2")
        await pm.start()
        result = await pm.get_powermeter_watts()
        await pm.stop()

    assert result == [100.0, 200.0, 300.0]
    # Basic auth is configured on the session.
    _, kwargs = session_cls.call_args
    assert kwargs["auth"].login == "admin"
    assert kwargs["auth"].password == "pw"
    # The bridge webserver is slow, so no tight connect timeout — only the
    # configurable total applies (#551).
    assert kwargs["timeout"].total == 5.0
    assert kwargs["timeout"].connect is None
    # The requested URL carries the configured node id.
    url = mock_aiohttp_session.get.call_args[0][0]
    assert url == "http://10.0.0.5/node_data.json?node_id=2"


async def test_timeout_is_configurable(mock_aiohttp_session: MagicMock) -> None:
    mock_aiohttp_session.set_read(_build_sml_frame(power_agg=100))
    with patch(
        "aiohttp.ClientSession", return_value=mock_aiohttp_session
    ) as session_cls:
        pm = _poller("10.0.0.5", "pw", timeout=12.5)
        await pm.start()
        await pm.stop()
    _, kwargs = session_cls.call_args
    assert kwargs["timeout"].total == 12.5


async def test_get_powermeter_watts_raises_on_undecodable_telegram(
    mock_aiohttp_session: MagicMock,
) -> None:
    mock_aiohttp_session.set_read(b"not a valid sml frame")
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        pm = _poller("127.0.0.1", "pw")
        await pm.start()
        with pytest.raises(ValueError, match="decode SML"):
            await pm.get_powermeter_watts()
        await pm.stop()


async def test_transient_undecodable_reuses_last_good(
    mock_aiohttp_session: MagicMock,
) -> None:
    # A bridge serving a push source occasionally returns an undecodable
    # telegram; within the staleness window the last good reading is reused
    # instead of raising (#518).
    now = [1000.0]
    frame = _build_sml_frame(power_l1=100, power_l2=200, power_l3=300)
    mock_aiohttp_session.set_read(frame)
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        pm = _poller("127.0.0.1", "pw", clock=lambda: now[0])
        await pm.start()
        assert await pm.get_powermeter_watts() == [100.0, 200.0, 300.0]

        # Next poll a couple of seconds later returns garbage → reuse cached.
        now[0] += 2.0
        mock_aiohttp_session.set_read(b"not a valid sml frame")
        assert await pm.get_powermeter_watts() == [100.0, 200.0, 300.0]
        await pm.stop()


async def test_stale_undecodable_raises_after_window(
    mock_aiohttp_session: MagicMock,
) -> None:
    now = [1000.0]
    frame = _build_sml_frame(power_l1=100, power_l2=200, power_l3=300)
    mock_aiohttp_session.set_read(frame)
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        pm = _poller("127.0.0.1", "pw", clock=lambda: now[0])
        await pm.start()
        assert await pm.get_powermeter_watts() == [100.0, 200.0, 300.0]

        # Past the staleness window a persistent failure surfaces as an error.
        now[0] += 60.0
        mock_aiohttp_session.set_read(b"not a valid sml frame")
        with pytest.raises(ValueError, match="decode SML"):
            await pm.get_powermeter_watts()
        await pm.stop()


async def test_get_powermeter_watts_raises_when_decoder_returns_no_powers(
    mock_aiohttp_session: MagicMock,
) -> None:
    # Defensive: an empty decode result is treated as a failed read, not 0 W.
    mock_aiohttp_session.set_read(b"frame-bytes-ignored-by-mock")
    with (
        patch("aiohttp.ClientSession", return_value=mock_aiohttp_session),
        patch("astrameter.powermeter.tibber_pulse.parse_sml_powers", return_value=[]),
    ):
        pm = _poller("127.0.0.1", "pw")
        await pm.start()
        with pytest.raises(ValueError, match="decode SML"):
            await pm.get_powermeter_watts()
        await pm.stop()


_FRAME = _build_sml_frame(power_l1=100, power_l2=200, power_l3=300)
_WATTS = [100.0, 200.0, 300.0]


def _bridge(
    served: set[str], frame: bytes, delays: dict[int, float] | None = None
) -> tuple[MagicMock, list[str]]:
    """A session whose bridge serves only the endpoint names in *served*.

    Returns the session and the list of URLs it was asked for; mutate *served*
    to simulate a firmware update between polls. *delays* maps a request's
    index to how long its response takes, to interleave concurrent polls.
    """
    requested: list[str] = []

    def get(url: str, **_: object) -> MagicMock:
        delay = (delays or {}).get(len(requested), 0.0)
        requested.append(url)
        endpoint = url.split("/")[3].split("?")[0]
        resp = MagicMock()
        resp.status = 200 if endpoint in served else 404
        resp.read = AsyncMock(return_value=frame)
        resp.raise_for_status = MagicMock()
        if endpoint not in served:
            resp.raise_for_status.side_effect = ClientResponseError(
                MagicMock(), (), status=404, message="Not Found"
            )

        async def enter() -> MagicMock:
            await asyncio.sleep(delay)
            return resp

        resp.__aenter__ = AsyncMock(side_effect=enter)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    session = MagicMock()
    session.get = MagicMock(side_effect=get)
    session.close = AsyncMock()
    return session, requested


async def test_new_firmware_uses_node_data_endpoint() -> None:
    session, requested = _bridge({"node_data.json"}, _FRAME)
    with patch("aiohttp.ClientSession", return_value=session):
        pm = _poller("10.0.0.5", "pw")
        await pm.start()
        assert await pm.get_powermeter_watts() == _WATTS
        assert await pm.get_powermeter_watts() == _WATTS
        await pm.stop()
    assert requested == ["http://10.0.0.5/node_data.json?node_id=1"] * 2


async def test_old_firmware_falls_back_to_data_json_once() -> None:
    # Firmware before ~1794 only serves /data.json (#685): the first poll
    # falls back after the 404, later polls go straight to the working path.
    session, requested = _bridge({"data.json"}, _FRAME)
    with patch("aiohttp.ClientSession", return_value=session):
        pm = _poller("10.0.0.5", "pw")
        await pm.start()
        assert await pm.get_powermeter_watts() == _WATTS
        assert await pm.get_powermeter_watts() == _WATTS
        await pm.stop()
    assert requested == [
        "http://10.0.0.5/node_data.json?node_id=1",
        "http://10.0.0.5/data.json?node_id=1",
        "http://10.0.0.5/data.json?node_id=1",
    ]


async def test_firmware_update_while_running_switches_endpoint() -> None:
    served = {"data.json"}
    session, requested = _bridge(served, _FRAME)
    with patch("aiohttp.ClientSession", return_value=session):
        pm = _poller("10.0.0.5", "pw")
        await pm.start()
        await pm.get_powermeter_watts()
        # The bridge updates over the air and drops the old path.
        served.clear()
        served.add("node_data.json")
        requested.clear()
        assert await pm.get_powermeter_watts() == _WATTS
        assert await pm.get_powermeter_watts() == _WATTS
        await pm.stop()
    assert requested == [
        "http://10.0.0.5/data.json?node_id=1",
        "http://10.0.0.5/node_data.json?node_id=1",
        "http://10.0.0.5/node_data.json?node_id=1",
    ]


async def test_overlapping_polls_both_fall_back() -> None:
    # Two polls in flight on old firmware: both 404 on /node_data.json, but
    # the second 404 arrives only after the first poll has already switched
    # the remembered endpoint. The second must still fall back to /data.json.
    session, requested = _bridge({"data.json"}, _FRAME, delays={1: 0.05})
    with patch("aiohttp.ClientSession", return_value=session):
        pm = _poller("10.0.0.5", "pw")
        await pm.start()
        results = await asyncio.gather(
            pm.get_powermeter_watts(), pm.get_powermeter_watts()
        )
        await pm.stop()
    assert results == [_WATTS, _WATTS]
    assert sorted(requested) == sorted(
        ["http://10.0.0.5/node_data.json?node_id=1"] * 2
        + ["http://10.0.0.5/data.json?node_id=1"] * 2
    )


async def test_404_on_both_endpoints_raises() -> None:
    session, requested = _bridge(set(), b"")
    with patch("aiohttp.ClientSession", return_value=session):
        pm = _poller("10.0.0.5", "pw")
        await pm.start()
        with pytest.raises(ClientResponseError) as exc:
            await pm.get_powermeter_watts()
        await pm.stop()
    assert exc.value.status == 404
    # One try per endpoint, no loop.
    assert len(requested) == 2


async def test_non_404_error_does_not_fall_back(
    mock_aiohttp_session: MagicMock,
) -> None:
    # A wrong password is not a firmware mismatch; surface it unchanged.
    mock_aiohttp_session.get.return_value.raise_for_status.side_effect = (
        ClientResponseError(MagicMock(), (), status=401, message="Unauthorized")
    )
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        pm = _poller("10.0.0.5", "pw")
        await pm.start()
        with pytest.raises(ClientResponseError) as exc:
            await pm.get_powermeter_watts()
        await pm.stop()
    assert exc.value.status == 401
    assert mock_aiohttp_session.get.call_count == 1


# -- push -------------------------------------------------------------------


class FakeBridge:
    """An in-process Pulse Bridge: ``/ws`` push, ``/nodes.json``, HTTP data.

    Queue frames with :meth:`push` (``None`` closes the socket, as the real
    bridge does every 30-60 s). ``ws_status`` makes the handshake fail with
    that status instead; ``poll_frame`` is what the HTTP data endpoint serves.
    """

    AUTH = "Basic " + base64.b64encode(b"admin:pw").decode()

    def __init__(self) -> None:
        self.ws_status: int | None = None
        self.poll_frame = _build_sml_frame(power_l1=1, power_l2=2, power_l3=3)
        self.nodes: Any = [{"node_id": 1, "eui": "AABBCCDD"}]
        self.polls = 0
        self.ws_connects = 0
        self._frames: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.server: TestServer | None = None

    def push(self, frame: bytes | None) -> None:
        self._frames.put_nowait(frame)

    @staticmethod
    def frame(body: bytes, *, device: str = "aabbccdd", topic: str = "sml") -> bytes:
        return f'<device:{device} topic:"pulse/{topic}">'.encode() + body

    @property
    def ip(self) -> str:
        assert self.server is not None
        return f"127.0.0.1:{self.server.port}"

    async def __aenter__(self) -> "FakeBridge":
        app = web.Application(middlewares=[self._auth])
        app.router.add_get("/ws", self._ws)
        app.router.add_get("/nodes.json", self._nodes)
        app.router.add_get("/node_data.json", self._data)
        self.server = TestServer(app)
        await self.server.start_server()
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.push(None)
        assert self.server is not None
        await self.server.close()

    @web.middleware
    async def _auth(self, request: web.Request, handler: Any) -> web.StreamResponse:
        if request.headers.get("Authorization") != self.AUTH:
            return web.Response(status=401)
        return await handler(request)

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        if self.ws_status is not None:
            return web.Response(status=self.ws_status)
        self.ws_connects += 1
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        # Read alongside sending, as a real server does, so a client that
        # leaves gets its close answered.
        reader = asyncio.ensure_future(self._drain(ws))
        try:
            while True:
                frame = asyncio.ensure_future(self._frames.get())
                await asyncio.wait({frame, reader}, return_when="FIRST_COMPLETED")
                data = frame.result() if frame.done() else None
                if data is None:
                    frame.cancel()
                    break
                await ws.send_bytes(data)
        finally:
            reader.cancel()
            await ws.close()
        return ws

    @staticmethod
    async def _drain(ws: web.WebSocketResponse) -> None:
        async for _ in ws:
            pass

    async def _nodes(self, request: web.Request) -> web.StreamResponse:
        return web.json_response(self.nodes)

    async def _data(self, request: web.Request) -> web.StreamResponse:
        self.polls += 1
        return web.Response(body=self.poll_frame)


_PUSHED = _build_sml_frame(power_l1=10, power_l2=20, power_l3=30)


async def _until(condition: Any, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.01)


async def test_push_is_the_default_and_replaces_polling() -> None:
    async with FakeBridge() as bridge:
        pm = TibberPulse(bridge.ip, "pw")
        await pm.start()
        try:
            await _until(lambda: bridge.ws_connects == 1)
            # Before the first push arrives, a read polls.
            assert pm.stream_online() is None
            assert await pm.get_powermeter_watts() == [1.0, 2.0, 3.0]
            polls = bridge.polls

            bridge.push(bridge.frame(_PUSHED))
            await _until(lambda: pm.stream_online() is True)
            assert await pm.get_powermeter_watts() == [10.0, 20.0, 30.0]
            assert bridge.polls == polls  # served from push, no HTTP round trip

            # wait_for_next_message blocks until the next telegram.
            waiter = asyncio.create_task(pm.wait_for_next_message(timeout=3))
            await asyncio.sleep(0.05)
            assert not waiter.done()
            bridge.push(
                bridge.frame(_build_sml_frame(power_l1=7, power_l2=8, power_l3=9))
            )
            await waiter
            assert await pm.get_powermeter_watts() == [7.0, 8.0, 9.0]
        finally:
            await pm.stop()


async def test_push_reconnects_after_the_bridge_drops_the_socket() -> None:
    async with FakeBridge() as bridge:
        pm = TibberPulse(bridge.ip, "pw")
        await pm.start()
        try:
            await _until(lambda: bridge.ws_connects == 1)
            bridge.push(None)
            await _until(lambda: bridge.ws_connects == 2)
            bridge.push(bridge.frame(_PUSHED))
            await _until(lambda: pm.stream_online() is True)
        finally:
            await pm.stop()


async def test_push_ignores_other_pulses_and_other_topics() -> None:
    async with FakeBridge() as bridge:
        pm = TibberPulse(bridge.ip, "pw")
        await pm.start()
        try:
            await _until(lambda: bridge.ws_connects == 1)
            bridge.push(bridge.frame(_PUSHED, device="11223344"))  # another node
            bridge.push(bridge.frame(_PUSHED, topic="metrics"))
            bridge.push(b"no header at all")
            bridge.push(bridge.frame(_PUSHED, device="AABBCCDD"))  # ours
            await _until(lambda: pm.stream_online() is True)
            assert await pm.get_powermeter_watts() == [10.0, 20.0, 30.0]
            assert pm._push_powers == [10.0, 20.0, 30.0]
        finally:
            await pm.stop()


async def test_push_stale_reading_falls_back_to_polling() -> None:
    now = [1000.0]
    async with FakeBridge() as bridge:
        pm = TibberPulse(bridge.ip, "pw", clock=lambda: now[0])
        await pm.start()
        try:
            bridge.push(bridge.frame(_PUSHED))
            await _until(lambda: pm.stream_online() is True)
            now[0] += 60  # the stream stalls without the socket closing
            assert pm.stream_online() is None
            await pm.wait_for_next_message(timeout=3)  # doesn't block
            assert await pm.get_powermeter_watts() == [1.0, 2.0, 3.0]
        finally:
            await pm.stop()


async def test_old_firmware_without_push_polls_for_good() -> None:
    async with FakeBridge() as bridge:
        bridge.ws_status = 404
        pm = TibberPulse(bridge.ip, "pw")
        await pm.start()
        try:
            await _until(lambda: pm._push_task is not None and pm._push_task.done())
            assert not pm._push_enabled
            assert await pm.get_powermeter_watts() == [1.0, 2.0, 3.0]
        finally:
            await pm.stop()


async def test_silent_push_gives_up_once_polling_works(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Some firmware accepts /ws but never streams the meter over it.
    monkeypatch.setattr(tibber_pulse, "_PUSH_GRACE_S", 0.2)
    monkeypatch.setattr(tibber_pulse, "_PUSH_IDLE_S", 0.1)
    async with FakeBridge() as bridge:
        pm = TibberPulse(bridge.ip, "pw")
        await pm.start()
        try:
            assert await pm.get_powermeter_watts() == [1.0, 2.0, 3.0]
            await _until(lambda: pm._push_task is not None and pm._push_task.done())
            assert not pm._push_enabled
        finally:
            await pm.stop()


async def test_silent_push_is_kept_while_the_bridge_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Without a working poll there's no evidence push is the problem.
    monkeypatch.setattr(tibber_pulse, "_PUSH_GRACE_S", 0.1)
    monkeypatch.setattr(tibber_pulse, "_PUSH_IDLE_S", 0.1)
    async with FakeBridge() as bridge:
        pm = TibberPulse(bridge.ip, "pw")
        await pm.start()
        try:
            await _until(lambda: bridge.ws_connects >= 2)
            assert pm._push_enabled
        finally:
            await pm.stop()


async def test_force_polling_never_opens_push() -> None:
    async with FakeBridge() as bridge:
        pm = TibberPulse(bridge.ip, "pw", force_polling=True)
        await pm.start()
        try:
            assert pm._push_task is None
            assert await pm.get_powermeter_watts() == [1.0, 2.0, 3.0]
            await asyncio.sleep(0.1)
            assert bridge.ws_connects == 0
        finally:
            await pm.stop()


async def test_push_unresolved_node_accepts_no_named_frames() -> None:
    # Without /nodes.json there's no telling this Pulse's frames from another's.
    async with FakeBridge() as bridge:
        bridge.nodes = {"unexpected": "shape"}
        pm = TibberPulse(bridge.ip, "pw")
        await pm.start()
        try:
            await _until(lambda: bridge.ws_connects == 1)
            bridge.push(bridge.frame(_PUSHED))
            await asyncio.sleep(0.1)
            assert pm.stream_online() is None
        finally:
            await pm.stop()


def test_split_push_frame() -> None:
    header, body = split_push_frame(b'<device:AB topic:"a b:c" len:3>x>y') or ({}, b"")
    assert header == {"device": "AB", "topic": "a b:c", "len": "3"}
    assert body == b"x>y"
    assert split_push_frame(b"topic:x>body") is None
    assert split_push_frame(b"<topic:x") is None
