from unittest.mock import AsyncMock, MagicMock

from astrameter.powermeter import (
    Shelly1PM,
    Shelly3EMPro,
    ShellyEM,
    ShellyPlus1PM,
)


def _mock_session(json_data: dict) -> MagicMock:
    """Create a mock aiohttp.ClientSession whose .get() returns *json_data*."""
    mock_response = AsyncMock()
    mock_response.raise_for_status = MagicMock()
    mock_response.json = AsyncMock(return_value=json_data)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=mock_response)
    ctx.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get.return_value = ctx
    return session


async def test_shelly1pm_get_powermeter_watts() -> None:
    shelly = Shelly1PM("192.168.1.2", "user", "pass", "")
    shelly.session = _mock_session({"meters": [{"power": 456}]})

    assert await shelly.get_powermeter_watts() == [456]


async def test_shellyem_get_powermeter_watts() -> None:
    shelly = ShellyEM("192.168.1.3", "user", "pass", "")
    shelly.session = _mock_session(
        {"emeters": [{"power": 789}, {"power": 1011}, {"power": 1213}]}
    )

    assert await shelly.get_powermeter_watts() == [789, 1011, 1213]


async def test_shellyplus1pm_get_powermeter_watts() -> None:
    shelly = ShellyPlus1PM("192.168.1.11", "user", "pass", "")
    shelly._rpc_session = _mock_session({"apower": 150})

    assert await shelly.get_powermeter_watts() == [150]


async def test_shelly1pm_get_powermeter_watts_indexed() -> None:
    shelly = Shelly1PM("192.168.1.2", "user", "pass", "0")
    shelly.session = _mock_session({"power": 789})

    assert await shelly.get_powermeter_watts() == [789]


async def test_shellyem_get_powermeter_watts_indexed() -> None:
    shelly = ShellyEM("192.168.1.3", "user", "pass", "1")
    shelly.session = _mock_session({"power": 555})

    assert await shelly.get_powermeter_watts() == [555]


async def test_shelly3empro_get_powermeter_watts() -> None:
    shelly = Shelly3EMPro("192.168.1.13", "user", "pass", "")
    shelly._rpc_session = _mock_session({"total_act_power": 450})

    assert await shelly.get_powermeter_watts() == [450]
