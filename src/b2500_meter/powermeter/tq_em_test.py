from unittest.mock import AsyncMock, MagicMock, patch

from b2500_meter.powermeter.tq_em import TQEnergyManager


def _make_resp(data, status=200):
    """Create a mock aiohttp response with async context manager support."""
    resp = MagicMock()
    resp.json = AsyncMock(return_value=data)
    resp.raise_for_status = MagicMock()
    resp.status = status
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _make_session(get_responses, post_responses=None):
    """Create a mock aiohttp session with sequenced GET/POST responses."""
    session = MagicMock()
    session.get = MagicMock(side_effect=[_make_resp(d) for d in get_responses])
    if post_responses:
        session.post = MagicMock(side_effect=[_make_resp(d) for d in post_responses])
    else:
        session.post = MagicMock()
    session.close = AsyncMock()
    return session


async def test_three_phase():
    session = _make_session(
        get_responses=[
            {"serial": "123", "authentication": False},
            {
                "1-0:21.4.0*255": 1,
                "1-0:22.4.0*255": 0,
                "1-0:41.4.0*255": 2,
                "1-0:42.4.0*255": 0,
                "1-0:61.4.0*255": 3,
                "1-0:62.4.0*255": 0,
            },
        ],
        post_responses=[{"authentication": True}],
    )
    with patch("aiohttp.ClientSession", return_value=session):
        meter = TQEnergyManager("192.168.0.10")
        await meter.start()
        assert await meter.get_powermeter_watts_async() == [1.0, 2.0, 3.0]
        await meter.stop()


async def test_total_only():
    session = _make_session(
        get_responses=[
            {"serial": "321", "authentication": False},
            {"1-0:1.4.0*255": 9, "1-0:2.4.0*255": 0},
        ],
        post_responses=[{"authentication": True}],
    )
    with patch("aiohttp.ClientSession", return_value=session):
        meter = TQEnergyManager("192.168.0.12")
        await meter.start()
        assert await meter.get_powermeter_watts_async() == [9.0]
        await meter.stop()


async def test_relogin_on_expired_session():
    session = _make_session(
        get_responses=[
            {"serial": "123", "authentication": False},
            {"status": 901},
            {"serial": "123", "authentication": False},
            {"1-0:1.4.0*255": 5, "1-0:2.4.0*255": 0},
        ],
        post_responses=[
            {"authentication": True},
            {"authentication": True},
        ],
    )
    with patch("aiohttp.ClientSession", return_value=session):
        meter = TQEnergyManager("192.168.0.11")
        await meter.start()
        assert await meter.get_powermeter_watts_async() == [5.0]
        await meter.stop()


async def test_missing_export():
    session = _make_session(
        get_responses=[
            {"serial": "111", "authentication": False},
            {"1-0:1.4.0*255": 4},
        ],
        post_responses=[{"authentication": True}],
    )
    with patch("aiohttp.ClientSession", return_value=session):
        meter = TQEnergyManager("192.168.0.15")
        await meter.start()
        assert await meter.get_powermeter_watts_async() == [4.0]
        await meter.stop()


async def test_three_phase_missing_export():
    session = _make_session(
        get_responses=[
            {"serial": "777", "authentication": False},
            {
                "1-0:21.4.0*255": 1,
                "1-0:41.4.0*255": 2,
                "1-0:61.4.0*255": 3,
            },
        ],
        post_responses=[{"authentication": True}],
    )
    with patch("aiohttp.ClientSession", return_value=session):
        meter = TQEnergyManager("192.168.0.16")
        await meter.start()
        assert await meter.get_powermeter_watts_async() == [1.0, 2.0, 3.0]
        await meter.stop()
