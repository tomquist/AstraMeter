from unittest.mock import patch

from aiohttp import BasicAuth, ClientTimeout

from astrameter.powermeter import JsonHttpPowermeter


async def test_single_phase(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"power": 100})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = JsonHttpPowermeter("http://localhost", "$.power")
        await meter.start()
        assert await meter.get_powermeter_watts() == [100.0]
        await meter.stop()


async def test_three_phase(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"p1": 100, "p2": 200, "p3": 300})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = JsonHttpPowermeter("http://localhost", ["$.p1", "$.p2", "$.p3"])
        await meter.start()
        assert await meter.get_powermeter_watts() == [100.0, 200.0, 300.0]
        await meter.stop()


async def test_headers_and_auth(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"power": 50})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session) as mock_cls:
        meter = JsonHttpPowermeter(
            "http://localhost",
            "$.power",
            username="user",
            password="pass",
            headers={"X-Test": "1"},
        )
        await meter.start()
        mock_cls.assert_called_once_with(
            auth=BasicAuth("user", "pass"),
            headers={"X-Test": "1"},
            timeout=ClientTimeout(total=10),
        )
        result = await meter.get_powermeter_watts()
        assert result == [50.0]
        mock_aiohttp_session.get.assert_called_once_with("http://localhost")
        await meter.stop()
