from unittest.mock import patch

import pytest

from astrameter.powermeter import Refoss
from astrameter.powermeter.refoss import parse_channels


def _status_response(channels: dict[int, float]):
    return {
        "status": [
            {
                "id": channel_id,
                "current": 0.0,
                "voltage": 230.0,
                "power": power,
                "pf": 1.0,
            }
            for channel_id, power in channels.items()
        ]
    }


def test_parse_channels():
    assert parse_channels("1") == [1]
    assert parse_channels("1,2,3") == [1, 2, 3]
    assert parse_channels(" 4 , 5 ") == [4, 5]
    with pytest.raises(ValueError, match="at least one"):
        parse_channels("")
    with pytest.raises(ValueError, match="integer"):
        parse_channels("a")
    with pytest.raises(ValueError, match="start at 1"):
        parse_channels("0")


async def test_get_powermeter_watts_single_channel(mock_aiohttp_session):
    mock_aiohttp_session.set_json(_status_response({1: 421.5, 2: 10.0}))
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = Refoss("192.168.1.150", [1])
        await meter.start()
        assert await meter.get_powermeter_watts() == [421.5]
        await meter.stop()


async def test_get_powermeter_watts_three_phase(mock_aiohttp_session):
    mock_aiohttp_session.set_json(
        _status_response({1: 100.0, 2: -50.0, 3: 25.5, 4: 999.0})
    )
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = Refoss("192.168.1.150", [1, 2, 3])
        await meter.start()
        assert await meter.get_powermeter_watts() == [100.0, -50.0, 25.5]
        await meter.stop()


async def test_get_powermeter_watts_missing_channel(mock_aiohttp_session):
    mock_aiohttp_session.set_json(_status_response({1: 10.0}))
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = Refoss("192.168.1.150", [1, 2])
        await meter.start()
        with pytest.raises(ValueError, match="channel 2"):
            await meter.get_powermeter_watts()
        await meter.stop()


async def test_get_powermeter_watts_missing_power_field(mock_aiohttp_session):
    mock_aiohttp_session.set_json(
        {"status": [{"id": 1, "current": 0.0, "voltage": 230.0}]}
    )
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = Refoss("192.168.1.150", [1])
        await meter.start()
        with pytest.raises(ValueError, match="missing power"):
            await meter.get_powermeter_watts()
        await meter.stop()


async def test_requires_ip():
    with pytest.raises(ValueError, match="IP"):
        Refoss("", [1])
    with pytest.raises(ValueError, match="IP"):
        Refoss("   ", [1])


def test_strips_ip():
    assert Refoss("  192.168.1.150  ", [1]).ip == "192.168.1.150"
