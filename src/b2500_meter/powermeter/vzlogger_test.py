from unittest.mock import patch

from b2500_meter.powermeter import VZLogger


async def test_vzlogger_get_powermeter_watts(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"data": [{"tuples": [[None, 900]]}]})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        vzlogger = VZLogger("192.168.1.9", "8088", "uuid")
        await vzlogger.start()
        assert await vzlogger.get_powermeter_watts_async() == [900]
        await vzlogger.stop()
