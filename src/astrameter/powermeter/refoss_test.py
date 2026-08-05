from unittest.mock import patch

import pytest

from astrameter.powermeter import Refoss
from astrameter.powermeter.refoss import parse_channels


def _status_response(channels: dict[int, float]):
    """Build an Em.Status.Get-shaped payload for the given channel powers."""
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
    """Accept positive ids and reject empty, non-integer, and zero values."""
    assert parse_channels("1") == [1]
    assert parse_channels("1,2,3") == [1, 2, 3]
    assert parse_channels(" 4 , 5 ") == [4, 5]
    with pytest.raises(ValueError, match="at least one"):
        parse_channels("")
    with pytest.raises(ValueError, match="empty"):
        parse_channels("1,")
    with pytest.raises(ValueError, match="empty"):
        parse_channels(",1")
    with pytest.raises(ValueError, match="empty"):
        parse_channels("1,,2")
    with pytest.raises(ValueError, match="integer"):
        parse_channels("a")
    with pytest.raises(ValueError, match="start at 1"):
        parse_channels("0")
    with pytest.raises(ValueError, match="duplicate"):
        parse_channels("1,1")
    with pytest.raises(ValueError, match="integer"):
        parse_channels("+1")
    with pytest.raises(ValueError, match="integer"):
        parse_channels("-1")


@pytest.mark.asyncio
async def test_get_powermeter_watts_single_channel(mock_aiohttp_session):
    """Return only the configured channel's signed power."""
    mock_aiohttp_session.set_json(_status_response({1: 421.5, 2: 10.0}))
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = Refoss("192.168.1.150", [1])
        await meter.start()
        assert await meter.get_powermeter_watts() == [421.5]
        await meter.stop()


@pytest.mark.asyncio
async def test_get_powermeter_watts_three_phase(mock_aiohttp_session):
    """Return watts in CHANNELS order even when the API lists channels out of order."""
    # Deliberately out of channel-id order so we catch implementations that
    # return response order instead of configured CHANNELS order.
    mock_aiohttp_session.set_json(
        _status_response({3: 25.5, 1: 100.0, 2: -50.0, 4: 999.0})
    )
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = Refoss("192.168.1.150", [1, 2, 3])
        await meter.start()
        assert await meter.get_powermeter_watts() == [100.0, -50.0, 25.5]
        await meter.stop()


@pytest.mark.asyncio
async def test_get_powermeter_watts_missing_channel(mock_aiohttp_session):
    """Raise when a configured channel id is absent from the device response."""
    mock_aiohttp_session.set_json(_status_response({1: 10.0}))
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = Refoss("192.168.1.150", [1, 2])
        await meter.start()
        with pytest.raises(ValueError, match="channel 2"):
            await meter.get_powermeter_watts()
        await meter.stop()


@pytest.mark.asyncio
async def test_get_powermeter_watts_missing_power_field(mock_aiohttp_session):
    """Raise when a status entry has an id but no power field."""
    mock_aiohttp_session.set_json(
        {"status": [{"id": 1, "current": 0.0, "voltage": 230.0}]}
    )
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = Refoss("192.168.1.150", [1])
        await meter.start()
        with pytest.raises(ValueError, match="missing power"):
            await meter.get_powermeter_watts()
        await meter.stop()


@pytest.mark.asyncio
async def test_get_powermeter_watts_rejects_non_object_json(mock_aiohttp_session):
    """Raise when the JSON root is not an object."""
    mock_aiohttp_session.set_json([{"id": 1, "power": 10.0}])
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = Refoss("192.168.1.150", [1])
        await meter.start()
        with pytest.raises(ValueError, match="must be an object"):
            await meter.get_powermeter_watts()
        await meter.stop()


@pytest.mark.asyncio
async def test_get_json_rejects_redirect(mock_aiohttp_session):
    """Do not follow or accept HTTP redirects when polling the meter."""
    mock_resp = mock_aiohttp_session.get.return_value
    # __aenter__ returns the response mock used inside the async with block.
    (await mock_resp.__aenter__()).status = 302
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        meter = Refoss("192.168.1.150", [1])
        await meter.start()
        with pytest.raises(ValueError, match="must not redirect"):
            await meter.get_json("/rpc/Em.Status.Get?id=65535")
        mock_aiohttp_session.get.assert_called_with(
            "http://192.168.1.150/rpc/Em.Status.Get?id=65535",
            allow_redirects=False,
        )
        await meter.stop()


@pytest.mark.asyncio
async def test_get_powermeter_watts_rejects_non_finite_power(mock_aiohttp_session):
    """Raise when power is NaN or ±infinity."""
    for bad in ("NaN", "inf", "-inf"):
        mock_aiohttp_session.set_json({"status": [{"id": 1, "power": bad}]})
        with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
            meter = Refoss("192.168.1.150", [1])
            await meter.start()
            with pytest.raises(ValueError, match="non-finite power"):
                await meter.get_powermeter_watts()
            await meter.stop()


def test_requires_ip():
    """Reject blank IP values before opening a session."""
    with pytest.raises(ValueError, match="IP"):
        Refoss("", [1])
    with pytest.raises(ValueError, match="IP"):
        Refoss("   ", [1])


def test_strips_ip():
    """Strip surrounding whitespace from the configured IP."""
    assert Refoss("  192.168.1.150  ", [1]).ip == "192.168.1.150"
