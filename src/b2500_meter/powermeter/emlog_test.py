from unittest.mock import patch

from b2500_meter.powermeter import Emlog


async def test_get_powermeter_watts_no_calculate(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"Leistung170": "200"})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        emlog = Emlog("127.0.0.1", "1", json_power_calculate=False)
        await emlog.start()
        assert await emlog.get_powermeter_watts() == [200]
        await emlog.stop()


async def test_get_powermeter_watts_with_calculate(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"Leistung170": "400", "Leistung270": "150"})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        emlog = Emlog("127.0.0.1", "1", json_power_calculate=True)
        await emlog.start()
        assert await emlog.get_powermeter_watts() == [250]
        await emlog.stop()
