from unittest.mock import patch

from b2500_meter.powermeter import AmisReader


async def test_amisreader_get_powermeter_watts(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"saldo": 1200})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        amisreader = AmisReader("192.168.1.10")
        await amisreader.start()
        assert await amisreader.get_powermeter_watts() == [1200]
        await amisreader.stop()
