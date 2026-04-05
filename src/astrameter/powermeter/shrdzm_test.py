from unittest.mock import patch

from astrameter.powermeter import Shrdzm


async def test_shrdzm_get_powermeter_watts(mock_aiohttp_session):
    mock_aiohttp_session.set_json({"1.7.0": 5000, "2.7.0": 2000})
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        shrdzm = Shrdzm("192.168.1.5", "user", "pass")
        await shrdzm.start()
        assert await shrdzm.get_powermeter_watts() == [3000]
        await shrdzm.stop()
