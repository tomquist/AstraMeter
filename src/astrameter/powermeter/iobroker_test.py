from unittest.mock import patch

from astrameter.powermeter import IoBroker


async def test_get_powermeter_watts_no_calculate(mock_aiohttp_session):
    mock_aiohttp_session.set_json([{"id": "alias1", "val": 100}])
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        iobroker = IoBroker(
            "127.0.0.1",
            "8080",
            "alias1",
            power_calculate=False,
            power_input_alias="input_alias",
            power_output_alias="output_alias",
        )
        await iobroker.start()
        assert await iobroker.get_powermeter_watts() == [100]
        await iobroker.stop()


async def test_get_powermeter_watts_with_calculate(mock_aiohttp_session):
    mock_aiohttp_session.set_json(
        [
            {"id": "input_alias", "val": 300},
            {"id": "output_alias", "val": 150},
        ]
    )
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        iobroker = IoBroker(
            "127.0.0.1",
            "8080",
            "alias1",
            power_calculate=True,
            power_input_alias="input_alias",
            power_output_alias="output_alias",
        )
        await iobroker.start()
        assert await iobroker.get_powermeter_watts() == [150]
        await iobroker.stop()
