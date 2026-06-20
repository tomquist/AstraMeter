from unittest.mock import patch

import pytest

from astrameter.powermeter import TibberPulse
from astrameter.powermeter.sml_test import _build_sml_frame


async def test_get_powermeter_watts_decodes_multiphase(mock_aiohttp_session):
    frame = _build_sml_frame(power_agg=1234, power_l1=400, power_l2=500, power_l3=334)
    mock_aiohttp_session.set_read(frame)
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        pm = TibberPulse("127.0.0.1", "AD56-54BA")
        await pm.start()
        # Per-phase preferred over aggregate when all three phases are present.
        assert await pm.get_powermeter_watts() == [400.0, 500.0, 334.0]
        await pm.stop()


async def test_get_powermeter_watts_builds_authenticated_url(mock_aiohttp_session):
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
    # The requested URL carries the configured node id.
    url = mock_aiohttp_session.get.call_args[0][0]
    assert url == "http://10.0.0.5/data.json?node_id=2"


async def test_get_powermeter_watts_raises_on_undecodable_telegram(
    mock_aiohttp_session,
):
    mock_aiohttp_session.set_read(b"not a valid sml frame")
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        pm = TibberPulse("127.0.0.1", "pw")
        await pm.start()
        with pytest.raises(ValueError, match="decode SML"):
            await pm.get_powermeter_watts()
        await pm.stop()


async def test_get_powermeter_watts_raises_when_decoder_returns_no_powers(
    mock_aiohttp_session,
):
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
