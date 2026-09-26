from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientResponseError

from astrameter.powermeter import TibberPulse
from astrameter.powermeter.sml_test import _build_sml_frame


async def test_get_powermeter_watts_decodes_multiphase(
    mock_aiohttp_session: MagicMock,
) -> None:
    frame = _build_sml_frame(power_agg=1234, power_l1=400, power_l2=500, power_l3=334)
    mock_aiohttp_session.set_read(frame)
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        pm = TibberPulse("127.0.0.1", "AD56-54BA")
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
        pm = TibberPulse("10.0.0.5", "pw", node_id="2")
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
        pm = TibberPulse("10.0.0.5", "pw", timeout=12.5)
        await pm.start()
        await pm.stop()
    _, kwargs = session_cls.call_args
    assert kwargs["timeout"].total == 12.5


async def test_get_powermeter_watts_raises_on_undecodable_telegram(
    mock_aiohttp_session: MagicMock,
) -> None:
    mock_aiohttp_session.set_read(b"not a valid sml frame")
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        pm = TibberPulse("127.0.0.1", "pw")
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
        pm = TibberPulse("127.0.0.1", "pw", clock=lambda: now[0])
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
        pm = TibberPulse("127.0.0.1", "pw", clock=lambda: now[0])
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
        pm = TibberPulse("127.0.0.1", "pw")
        await pm.start()
        with pytest.raises(ValueError, match="decode SML"):
            await pm.get_powermeter_watts()
        await pm.stop()


_FRAME = _build_sml_frame(power_l1=100, power_l2=200, power_l3=300)
_WATTS = [100.0, 200.0, 300.0]


def _bridge(served: set[str], frame: bytes) -> tuple[MagicMock, list[str]]:
    """A session whose bridge serves only the endpoint names in *served*.

    Returns the session and the list of URLs it was asked for; mutate *served*
    to simulate a firmware update between polls.
    """
    requested: list[str] = []

    def get(url: str, **_: object) -> MagicMock:
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
        resp.__aenter__ = AsyncMock(return_value=resp)
        resp.__aexit__ = AsyncMock(return_value=False)
        return resp

    session = MagicMock()
    session.get = MagicMock(side_effect=get)
    session.close = AsyncMock()
    return session, requested


async def test_new_firmware_uses_node_data_endpoint() -> None:
    session, requested = _bridge({"node_data.json"}, _FRAME)
    with patch("aiohttp.ClientSession", return_value=session):
        pm = TibberPulse("10.0.0.5", "pw")
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
        pm = TibberPulse("10.0.0.5", "pw")
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
        pm = TibberPulse("10.0.0.5", "pw")
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


async def test_404_on_both_endpoints_raises() -> None:
    session, requested = _bridge(set(), b"")
    with patch("aiohttp.ClientSession", return_value=session):
        pm = TibberPulse("10.0.0.5", "pw")
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
        pm = TibberPulse("10.0.0.5", "pw")
        await pm.start()
        with pytest.raises(ClientResponseError) as exc:
            await pm.get_powermeter_watts()
        await pm.stop()
    assert exc.value.status == 401
    assert mock_aiohttp_session.get.call_count == 1
