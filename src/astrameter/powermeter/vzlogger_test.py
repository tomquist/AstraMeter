from unittest.mock import patch

from astrameter.powermeter import VZLogger


async def test_vzlogger_get_powermeter_watts(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"data": [{"tuples": [[None, 900]]}]})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        vzlogger = VZLogger("192.168.1.9", "8088", "uuid")
        await vzlogger.start()
        assert await vzlogger.get_powermeter_watts() == [900]
        await vzlogger.stop()


async def test_vzlogger_three_phase(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"data": [{"tuples": [[None, 900]]}]})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        vzlogger = VZLogger("192.168.1.9", "8088", ["uuid-l1", "uuid-l2", "uuid-l3"])
        await vzlogger.start()
        assert await vzlogger.get_powermeter_watts() == [900, 900, 900]
        urls = [c.args[0] for c in mock_aiohttp_session.get.call_args_list]
        assert urls == [
            "http://192.168.1.9:8088/uuid-l1",
            "http://192.168.1.9:8088/uuid-l2",
            "http://192.168.1.9:8088/uuid-l3",
        ]
        await vzlogger.stop()
