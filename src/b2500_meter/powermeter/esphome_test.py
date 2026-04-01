from unittest.mock import patch

from b2500_meter.powermeter import ESPHome


async def test_esphome_get_powermeter_watts(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"value": 234})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        esphome = ESPHome("192.168.1.4", "80", "sensor", "power")
        await esphome.start()
        assert await esphome.get_powermeter_watts() == [234]
        await esphome.stop()
