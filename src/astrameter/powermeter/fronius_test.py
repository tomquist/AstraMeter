from unittest.mock import patch

import pytest

from astrameter.powermeter import Fronius


def _meter_response(power):
    return {
        "Head": {"Status": {"Code": 0, "Reason": "", "UserMessage": ""}},
        "Body": {"Data": {"PowerReal_P_Sum": power}},
    }


async def test_get_powermeter_watts_import(mock_aiohttp_session):
    mock_aiohttp_session.set_json(_meter_response(562.93))
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        fronius = Fronius("127.0.0.1")
        await fronius.start()
        assert await fronius.get_powermeter_watts() == [562.93]
        await fronius.stop()


async def test_get_powermeter_watts_export(mock_aiohttp_session):
    mock_aiohttp_session.set_json(_meter_response(-834.13))
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        fronius = Fronius("127.0.0.1", device_id="1")
        await fronius.start()
        assert await fronius.get_powermeter_watts() == [-834.13]
        await fronius.stop()


async def test_get_powermeter_watts_per_phase(mock_aiohttp_session):
    response = _meter_response(600.0)
    response["Body"]["Data"].update(
        {
            "PowerReal_P_Phase_1": 100.0,
            "PowerReal_P_Phase_2": 200.0,
            "PowerReal_P_Phase_3": 300.0,
        }
    )
    mock_aiohttp_session.set_json(response)
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        fronius = Fronius("127.0.0.1", per_phase=True)
        await fronius.start()
        assert await fronius.get_powermeter_watts() == [100.0, 200.0, 300.0]
        await fronius.stop()


async def test_get_powermeter_watts_per_phase_missing_phase_defaults_zero(
    mock_aiohttp_session,
):
    response = _meter_response(150.0)
    response["Body"]["Data"]["PowerReal_P_Phase_1"] = 150.0
    mock_aiohttp_session.set_json(response)
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        fronius = Fronius("127.0.0.1", per_phase=True)
        await fronius.start()
        assert await fronius.get_powermeter_watts() == [150.0, 0.0, 0.0]
        await fronius.stop()


async def test_get_powermeter_watts_raises_on_api_error(mock_aiohttp_session):
    mock_aiohttp_session.set_json(
        {
            "Head": {"Status": {"Code": 1, "Reason": "device not available"}},
            "Body": {"Data": {}},
        }
    )
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        fronius = Fronius("127.0.0.1")
        await fronius.start()
        with pytest.raises(ValueError, match="device not available"):
            await fronius.get_powermeter_watts()
        await fronius.stop()
