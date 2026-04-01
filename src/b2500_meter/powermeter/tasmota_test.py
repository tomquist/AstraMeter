from unittest.mock import patch

from b2500_meter.powermeter import Tasmota


async def test_tasmota_get_powermeter_watts(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"StatusSNS": {"ENERGY": {"Power": 123}}})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        tasmota = Tasmota(
            "192.168.1.1",
            "user",
            "pass",
            "StatusSNS",
            "ENERGY",
            "Power",
            "",
            "",
            False,
        )
        await tasmota.start()
        assert await tasmota.get_powermeter_watts() == [123]
        mock_aiohttp_session.get.assert_called_with(
            "http://192.168.1.1/cm?user=user&password=pass&cmnd=status+10"
        )
        await tasmota.stop()


async def test_tasmota_unauthenticated(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"StatusSNS": {"ENERGY": {"Power": 123}}})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        tasmota = Tasmota(
            "192.168.1.1",
            "",
            "",
            "StatusSNS",
            "ENERGY",
            "Power",
            "",
            "",
            False,
        )
        await tasmota.start()
        assert await tasmota.get_powermeter_watts() == [123]
        mock_aiohttp_session.get.assert_called_with(
            "http://192.168.1.1/cm?cmnd=status%2010"
        )
        await tasmota.stop()
