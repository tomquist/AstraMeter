from unittest.mock import patch

import pytest

from astrameter.powermeter import Tasmota


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


async def test_tasmota_three_phase(mock_aiohttp_session):
    mock_aiohttp_session.set_json(
        {"StatusSNS": {"eBZ": {"Power_L1": 100, "Power_L2": 200, "Power_L3": 300}}}
    )
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        tasmota = Tasmota(
            "192.168.1.1",
            "",
            "",
            "StatusSNS",
            "eBZ",
            ["Power_L1", "Power_L2", "Power_L3"],
            "",
            "",
            False,
        )
        await tasmota.start()
        assert await tasmota.get_powermeter_watts() == [100, 200, 300]
        await tasmota.stop()


async def test_tasmota_three_phase_calculate(mock_aiohttp_session):
    mock_aiohttp_session.set_json(
        {
            "StatusSNS": {
                "SML": {
                    "In_L1": 1000,
                    "In_L2": 2000,
                    "In_L3": 3000,
                    "Out_L1": 100,
                    "Out_L2": 200,
                    "Out_L3": 300,
                }
            }
        }
    )
    with patch("aiohttp.ClientSession", return_value=mock_aiohttp_session):
        tasmota = Tasmota(
            "192.168.1.1",
            "",
            "",
            "StatusSNS",
            "SML",
            "",
            ["In_L1", "In_L2", "In_L3"],
            ["Out_L1", "Out_L2", "Out_L3"],
            True,
        )
        await tasmota.start()
        assert await tasmota.get_powermeter_watts() == [900, 1800, 2700]
        await tasmota.stop()


def test_tasmota_mismatched_calculate_labels():
    with pytest.raises(ValueError, match="same number of entries"):
        Tasmota(
            "192.168.1.1",
            "",
            "",
            "StatusSNS",
            "SML",
            "",
            ["In_L1", "In_L2"],
            ["Out_L1"],
            True,
        )


def test_tasmota_empty_calculate_labels():
    with pytest.raises(ValueError, match="cannot be empty"):
        Tasmota(
            "192.168.1.1",
            "",
            "",
            "StatusSNS",
            "SML",
            "",
            "",
            "",
            True,
        )
