import asyncio
import configparser
import os
import struct
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from b2500_meter.config.config_loader import create_sml_powermeter
from b2500_meter.powermeter.sml import (
    _OBIS_POWER_CURRENT,
    _OBIS_POWER_L1,
    _OBIS_POWER_L2,
    _OBIS_POWER_L3,
    EnergyStats,
    Sml,
    parse_sml_obis_config,
)


def _obis_value(obis: str, value: int, unit: int) -> SimpleNamespace:
    return SimpleNamespace(obis=obis, value=value, unit=unit)


def _defaults():
    return (_OBIS_POWER_CURRENT, _OBIS_POWER_L1, _OBIS_POWER_L2, _OBIS_POWER_L3)


class TestEnergyStatsFromSmlFrame(unittest.TestCase):
    def test_aggregate_only(self):
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_CURRENT, 1500, 27),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [1500])

    def test_multiphase_when_all_phases_present(self):
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_L1, 100, 27),
            _obis_value(_OBIS_POWER_L2, 200, 27),
            _obis_value(_OBIS_POWER_L3, 300, 27),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [100, 200, 300])

    def test_prefers_multiphase_when_aggregate_also_present(self):
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_CURRENT, 9999, 27),
            _obis_value(_OBIS_POWER_L1, 100, 27),
            _obis_value(_OBIS_POWER_L2, 200, 27),
            _obis_value(_OBIS_POWER_L3, 300, 27),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [100, 200, 300])

    def test_falls_back_to_aggregate_if_incomplete_phases(self):
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_CURRENT, 1500, 27),
            _obis_value(_OBIS_POWER_L1, 100, 27),
            _obis_value(_OBIS_POWER_L2, 200, 27),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [1500])

    def test_wrong_unit_raises(self):
        frame = MagicMock()
        frame.get_obis.return_value = [_obis_value(_OBIS_POWER_CURRENT, 1500, 30)]
        with self.assertRaises(ValueError) as ctx:
            EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertIn("aggregate power", str(ctx.exception).lower())
        self.assertIn("1500", str(ctx.exception))


class TestParseSmlObisConfig(unittest.TestCase):
    def test_defaults_when_empty(self):
        config = configparser.ConfigParser()
        config.read_string("[SML]\n")
        t = parse_sml_obis_config("SML", config)
        self.assertEqual(t, _defaults())

    def test_override_normalized(self):
        config = configparser.ConfigParser()
        config.read_string("[SML]\nOBIS_POWER_CURRENT = 0100100700FF\n")
        oc, o1, _o2, _o3 = parse_sml_obis_config("SML", config)
        self.assertEqual(oc, "0100100700ff")
        self.assertEqual(o1, _OBIS_POWER_L1)

    def test_invalid_length_raises(self):
        config = configparser.ConfigParser()
        config.read_string("[SML]\nOBIS_POWER_CURRENT = deadbeef\n")
        with self.assertRaises(ValueError):
            parse_sml_obis_config("SML", config)


class TestCreateSmlPowermeter(unittest.TestCase):
    def test_missing_serial_raises(self):
        config = configparser.ConfigParser()
        config.read_string("[SML]\n")
        with self.assertRaises(ValueError) as ctx:
            create_sml_powermeter("SML", config)
        self.assertIn("SERIAL", str(ctx.exception))

    def test_serial_trimmed(self):
        config = configparser.ConfigParser()
        config.read_string("[SML]\nSERIAL = /dev/ttyAMA0\n")
        pm = create_sml_powermeter("SML", config)
        self.assertEqual(pm._serial_device, "/dev/ttyAMA0")

    def test_custom_obis_passed_to_sml(self):
        config = configparser.ConfigParser()
        config.read_string(
            "[SML]\n"
            "SERIAL = /dev/ttyUSB0\n"
            "OBIS_POWER_CURRENT = 0100100700ff\n"
            "OBIS_POWER_L1 = 0100240700ff\n"
            "OBIS_POWER_L2 = 0100380700ff\n"
            "OBIS_POWER_L3 = 01004c0700ff\n"
        )
        pm = create_sml_powermeter("SML", config)
        self.assertEqual(pm._obis_current, "0100100700ff")
        self.assertEqual(pm._obis_l1, "0100240700ff")


# --- Async tests for the migrated Sml class ---


async def test_async_read_returns_updated_powers():
    sml = Sml("/dev/ttyUSB0")

    async def fake_read():
        sml._current = EnergyStats(powers=[500])

    with patch.object(sml, "_read_serial", side_effect=fake_read):
        result = await sml.get_powermeter_watts()
    assert result == [500.0]


async def test_async_skip_if_busy_returns_cached():
    sml = Sml("/dev/ttyUSB0")
    sml._current = EnergyStats(powers=[999])

    read_called = False

    async def fake_read():
        nonlocal read_called
        read_called = True

    with patch.object(sml, "_read_serial", side_effect=fake_read):
        # Hold the lock externally to simulate a read in progress
        await sml._lock.acquire()
        try:
            result = await sml.get_powermeter_watts()
        finally:
            sml._lock.release()

    assert result == [999.0]
    assert not read_called


async def test_async_lock_released_on_exception():
    sml = Sml("/dev/ttyUSB0")
    original_powers = sml._current.powers[:]

    async def failing_read():
        raise OSError("serial port error")

    async def successful_read():
        sml._current = EnergyStats(powers=[42])

    with (
        patch.object(sml, "_read_serial", side_effect=failing_read),
        pytest.raises(OSError, match="serial port error"),
    ):
        await sml.get_powermeter_watts()

    # Lock should be released; current should be unchanged
    assert not sml._lock.locked()
    assert sml._current.powers == original_powers

    # Subsequent call should succeed
    with patch.object(sml, "_read_serial", side_effect=successful_read):
        result = await sml.get_powermeter_watts()
    assert result == [42.0]


async def test_async_cold_start_skip_returns_zero():
    sml = Sml("/dev/ttyUSB0")
    await sml._lock.acquire()
    try:
        result = await sml.get_powermeter_watts()
    finally:
        sml._lock.release()
    assert result == [0.0]


async def test_async_concurrent_callers():
    sml = Sml("/dev/ttyUSB0")
    sml._current = EnergyStats(powers=[100])
    read_count = 0
    read_started = asyncio.Event()

    async def slow_read():
        nonlocal read_count
        read_count += 1
        read_started.set()
        await asyncio.sleep(0.1)
        sml._current = EnergyStats(powers=[200])

    with patch.object(sml, "_read_serial", side_effect=slow_read):
        # Launch 3 concurrent callers
        async def caller():
            return await sml.get_powermeter_watts()

        task1 = asyncio.create_task(caller())
        # Wait for the first caller to acquire the lock and start reading
        await read_started.wait()
        # These two should see the lock as busy and return cached
        r2 = await caller()
        r3 = await caller()
        r1 = await task1

    assert read_count == 1  # Only one actual read
    assert r1 == [200.0]  # Fresh data from the read
    assert r2 == [100.0]  # Cached value (before read completed)
    assert r3 == [100.0]  # Cached value (before read completed)


async def test_read_serial_retries_on_crc_error():
    sml = Sml("/dev/ttyUSB0")

    mock_reader = AsyncMock()
    mock_reader.read = AsyncMock(return_value=b"\x00" * 64)
    sml._reader = mock_reader

    call_count = 0
    mock_frame = MagicMock()
    mock_frame.get_obis.return_value = [
        _obis_value(_OBIS_POWER_CURRENT, 750, 27),
    ]

    import smllib.errors

    def patched_get_frame(stream_self):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise smllib.errors.CrcError(b"", 0, 0)
        return mock_frame

    with patch("smllib.SmlStreamReader.get_frame", patched_get_frame):
        await sml._read_serial()

    assert sml._current.powers == [750]
    assert call_count == 2


async def test_read_serial_timeout():
    sml = Sml("/dev/ttyUSB0")

    mock_reader = AsyncMock()
    mock_reader.read = AsyncMock(side_effect=asyncio.TimeoutError())
    sml._reader = mock_reader

    original_powers = sml._current.powers[:]
    # Should not raise — timeout is handled gracefully
    await sml._read_serial()
    assert sml._current.powers == original_powers


# --- E2E test using PTY virtual serial port ---


def _build_sml_frame(
    power_agg: int = 1500,
    power_l1: int = 500,
    power_l2: int = 600,
    power_l3: int = 400,
) -> bytes:
    """Construct a valid SML binary frame with power OBIS entries."""
    from smllib.crc.x25 import get_crc

    def make_list_entry(obis_hex: str, value: int) -> bytes:
        entry = b"\x77\x07" + bytes.fromhex(obis_hex)
        entry += b"\x01\x01\x62\x1b\x52\x00"
        entry += b"\x55" + struct.pack(">i", value) + b"\x01"
        return entry

    def make_message(choice_tag: int, body: bytes) -> bytes:
        msg = b"\x76\x05\x00\x00\x00\x01\x62\x00\x62\x00\x72"
        msg += b"\x63" + struct.pack(">H", choice_tag) + body
        msg += b"\x63\x00\x00\x00"
        return msg

    open_resp = (
        b"\x76\x01\x01\x05\x00\x00\x00\x01"
        b"\x0b\x0a\x01ISK\x00\x05\x00\x9f\x5c\xe5\x01\x01"
    )
    entries = [
        make_list_entry("0100100700ff", power_agg),
        make_list_entry("0100240700ff", power_l1),
        make_list_entry("0100380700ff", power_l2),
        make_list_entry("01004c0700ff", power_l3),
    ]
    get_list_body = b"\x77\x01\x01\x01" + bytes([0x70 | len(entries)])
    for e in entries:
        get_list_body += e
    get_list_body += b"\x01\x01"
    close_resp = b"\x71\x01"

    payload = (
        make_message(0x0101, open_resp)
        + make_message(0x0701, get_list_body)
        + make_message(0x0201, close_resp)
    )

    start = b"\x1b\x1b\x1b\x1b\x01\x01\x01\x01"
    end_marker = b"\x1b\x1b\x1b\x1b\x1a"
    total = len(start) + len(payload) + len(end_marker) + 3
    padding = (4 - (total % 4)) % 4
    frame_no_crc = start + payload + b"\x00" * padding + end_marker + bytes([padding])
    crc = get_crc(frame_no_crc)
    return frame_no_crc + struct.pack(">H", crc)


async def test_e2e_pty_serial_read():
    """Full E2E test: PTY pair → async serial read → SML parse → power values."""
    master_fd, slave_fd = os.openpty()
    slave_name = os.ttyname(slave_fd)

    frame_data = _build_sml_frame(
        power_agg=1234, power_l1=400, power_l2=500, power_l3=334
    )

    def writer():
        time.sleep(0.2)
        os.write(master_fd, frame_data)

    t = threading.Thread(target=writer, daemon=True)
    t.start()

    sml = Sml(slave_name)
    try:
        await sml.start()
        result = await sml.get_powermeter_watts()
        # 3-phase preferred over aggregate when all three present
        assert result == [400.0, 500.0, 334.0]
    finally:
        await sml.stop()
        os.close(master_fd)
        os.close(slave_fd)
