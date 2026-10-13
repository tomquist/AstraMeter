import asyncio
import configparser
import os
import struct
import threading
import time
import unittest
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from astrameter.config.config_loader import create_sml_powermeter
from astrameter.powermeter.sml import (
    _OBIS_POWER_CURRENT,
    _OBIS_POWER_L1,
    _OBIS_POWER_L2,
    _OBIS_POWER_L3,
    EnergyStats,
    Sml,
    parse_sml_obis_config,
    parse_sml_powers,
)


def _obis_value(
    obis: str, value: int, unit: int, scaler: int | None = None
) -> SimpleNamespace:
    return SimpleNamespace(obis=obis, value=value, unit=unit, scaler=scaler)


def _defaults() -> tuple[str, str, str, str]:
    return (_OBIS_POWER_CURRENT, _OBIS_POWER_L1, _OBIS_POWER_L2, _OBIS_POWER_L3)


class TestEnergyStatsFromSmlFrame(unittest.TestCase):
    def test_aggregate_only(self) -> None:
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_CURRENT, 1500, 27),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [1500])

    def test_multiphase_when_all_phases_present(self) -> None:
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_L1, 100, 27),
            _obis_value(_OBIS_POWER_L2, 200, 27),
            _obis_value(_OBIS_POWER_L3, 300, 27),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [100, 200, 300])

    def test_prefers_multiphase_when_aggregate_also_present(self) -> None:
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_CURRENT, 9999, 27),
            _obis_value(_OBIS_POWER_L1, 100, 27),
            _obis_value(_OBIS_POWER_L2, 200, 27),
            _obis_value(_OBIS_POWER_L3, 300, 27),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [100, 200, 300])

    def test_falls_back_to_aggregate_if_incomplete_phases(self) -> None:
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_CURRENT, 1500, 27),
            _obis_value(_OBIS_POWER_L1, 100, 27),
            _obis_value(_OBIS_POWER_L2, 200, 27),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [1500])

    def test_applies_negative_scaler_aggregate(self) -> None:
        # Meters like the Apator Picus eHZ send watts with scaler -3, so the
        # raw integer is 1000x too large; the scaler must be applied (#519).
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_CURRENT, 170000, 27, scaler=-3),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [170.0])

    def test_applies_scaler_per_phase(self) -> None:
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_L1, 100000, 27, scaler=-3),
            _obis_value(_OBIS_POWER_L2, 200000, 27, scaler=-3),
            _obis_value(_OBIS_POWER_L3, -50000, 27, scaler=-3),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [100.0, 200.0, -50.0])

    def test_scaler_zero_or_absent_unchanged(self) -> None:
        # scaler 0 and a missing scaler both mean "already in watts".
        frame = MagicMock()
        frame.get_obis.return_value = [
            _obis_value(_OBIS_POWER_CURRENT, 1500, 27, scaler=0),
        ]
        stats = EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertEqual(stats.powers, [1500])

    def test_wrong_unit_raises(self) -> None:
        frame = MagicMock()
        frame.get_obis.return_value = [_obis_value(_OBIS_POWER_CURRENT, 1500, 30)]
        with self.assertRaises(ValueError) as ctx:
            EnergyStats.from_sml_frame(frame, *_defaults())
        self.assertIn("aggregate power", str(ctx.exception).lower())
        self.assertIn("1500", str(ctx.exception))


class TestParseSmlObisConfig(unittest.TestCase):
    def test_defaults_when_empty(self) -> None:
        config = configparser.ConfigParser()
        config.read_string("[SML]\n")
        t = parse_sml_obis_config("SML", config)
        self.assertEqual(t, _defaults())

    def test_override_normalized(self) -> None:
        config = configparser.ConfigParser()
        config.read_string("[SML]\nOBIS_POWER_CURRENT = 0100100700FF\n")
        oc, o1, _o2, _o3 = parse_sml_obis_config("SML", config)
        self.assertEqual(oc, "0100100700ff")
        self.assertEqual(o1, _OBIS_POWER_L1)

    def test_invalid_length_raises(self) -> None:
        config = configparser.ConfigParser()
        config.read_string("[SML]\nOBIS_POWER_CURRENT = deadbeef\n")
        with self.assertRaises(ValueError):
            parse_sml_obis_config("SML", config)


class TestCreateSmlPowermeter(unittest.TestCase):
    def test_missing_serial_raises(self) -> None:
        config = configparser.ConfigParser()
        config.read_string("[SML]\n")
        with self.assertRaises(ValueError) as ctx:
            create_sml_powermeter("SML", config)
        self.assertIn("SERIAL", str(ctx.exception))

    def test_serial_trimmed(self) -> None:
        config = configparser.ConfigParser()
        config.read_string("[SML]\nSERIAL = /dev/ttyAMA0\n")
        pm = create_sml_powermeter("SML", config)
        assert isinstance(pm, Sml)
        self.assertEqual(pm._serial_device, "/dev/ttyAMA0")

    def test_custom_obis_passed_to_sml(self) -> None:
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
        assert isinstance(pm, Sml)
        self.assertEqual(pm._obis_current, "0100100700ff")
        self.assertEqual(pm._obis_l1, "0100240700ff")


# --- E2E test using PTY virtual serial port ---


def _build_sml_frame(
    power_agg: int = 1500,
    power_l1: int = 500,
    power_l2: int = 600,
    power_l3: int = 400,
    scaler: int = 0,
) -> bytes:
    """Construct a valid SML binary frame with power OBIS entries."""
    from smllib.crc.x25 import get_crc

    def make_list_entry(obis_hex: str, value: int) -> bytes:
        entry = b"\x77\x07" + bytes.fromhex(obis_hex)
        # status, val_time, unit (0x1b = W), then a signed-byte scaler.
        entry += b"\x01\x01\x62\x1b\x52" + struct.pack(">b", scaler)
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


def test_parse_sml_powers_decodes_full_telegram() -> None:
    """parse_sml_powers decodes a complete SML telegram (bytes) into watts."""
    frame = _build_sml_frame(power_agg=1234, power_l1=400, power_l2=500, power_l3=334)
    # All three phases present → per-phase preferred over the aggregate.
    assert parse_sml_powers(frame) == [400, 500, 334]


def test_parse_sml_powers_applies_scaler() -> None:
    """A meter sending watts with scaler -3 is decoded to true watts (#519)."""
    frame = _build_sml_frame(
        power_agg=170000, power_l1=170000, power_l2=0, power_l3=0, scaler=-3
    )
    # Per-phase preferred; the raw 170000 mW becomes 170 W, not 170000 W.
    assert parse_sml_powers(frame) == [170.0, 0.0, 0.0]


def test_parse_sml_powers_returns_none_on_garbage() -> None:
    """parse_sml_powers returns None when no valid frame can be parsed."""
    assert parse_sml_powers(b"not an sml telegram") is None


async def test_e2e_pty_serial_read() -> None:
    """Full E2E test: PTY pair -> background reader -> SML parse -> power values."""
    master_fd, slave_fd = os.openpty()
    slave_name = os.ttyname(slave_fd)

    frame_data = _build_sml_frame(
        power_agg=1234, power_l1=400, power_l2=500, power_l3=334
    )

    def writer() -> None:
        time.sleep(0.2)
        os.write(master_fd, frame_data)

    t = threading.Thread(target=writer, daemon=True)
    t.start()

    sml = Sml(slave_name)
    try:
        await sml.start()
        await sml.wait_for_message(timeout=5)
        # 3-phase preferred over aggregate when all three present
        assert await sml.get_powermeter_watts() == [400.0, 500.0, 334.0]
        assert sml.stream_online() is True
    finally:
        await sml.stop()
        os.close(master_fd)
        os.close(slave_fd)
    assert sml.stream_online() is False


class _ChunkedReader:
    """Serial stand-in that hands out *data* a few bytes per read, as a
    9600-baud port does; returns ``b""`` (EOF) once exhausted."""

    def __init__(self, data: bytes, chunk: int) -> None:
        self._data = data
        self._chunk = chunk

    async def read(self, n: int) -> bytes:
        out = self._data[: min(n, self._chunk)]
        self._data = self._data[len(out) :]
        return out


def _build_single_frame(watts: int) -> bytes:
    """A telegram reading *watts* on L1 and nothing on L2/L3."""
    return _build_sml_frame(power_agg=watts, power_l1=watts, power_l2=0, power_l3=0)


async def _feed(sml: Sml, data: bytes, chunk: int = 8) -> None:
    await sml._read_stream(cast("asyncio.StreamReader", _ChunkedReader(data, chunk)))


async def test_frames_assembled_from_small_reads() -> None:
    """A telegram arriving a few bytes per read, starting mid-telegram, still
    decodes (#680: a ~400-byte signed EMH telegram)."""
    frame = _build_single_frame(110)
    sml = Sml("/dev/ttyUSB0")
    await _feed(sml, frame[len(frame) // 2 :] + frame)
    assert await sml.get_powermeter_watts() == [110.0, 0.0, 0.0]


async def test_newest_frame_wins() -> None:
    """Every telegram is published as it completes, so a reading is the latest
    one rather than the oldest still buffered."""
    sml = Sml("/dev/ttyUSB0")
    await _feed(sml, _build_single_frame(100) + _build_single_frame(200), chunk=512)
    assert await sml.get_powermeter_watts() == [200.0, 0.0, 0.0]


async def test_wait_for_next_message_resolves_on_next_frame() -> None:
    sml = Sml("/dev/ttyUSB0")
    await _feed(sml, _build_single_frame(100))
    waiter = asyncio.create_task(sml.wait_for_next_message(timeout=5))
    await asyncio.sleep(0)
    assert not waiter.done()
    await _feed(sml, _build_single_frame(300))
    await waiter
    assert await sml.get_powermeter_watts() == [300.0, 0.0, 0.0]


async def test_crc_error_skipped() -> None:
    """A corrupt telegram is dropped and the next intact one still decodes."""
    bad = bytearray(_build_single_frame(100))
    bad[-1] ^= 0xFF
    sml = Sml("/dev/ttyUSB0")
    await _feed(sml, bytes(bad) + _build_single_frame(250))
    assert await sml.get_powermeter_watts() == [250.0, 0.0, 0.0]


async def test_no_reading_before_first_frame() -> None:
    sml = Sml("/dev/ttyUSB0")
    with pytest.raises(ValueError, match="No value received"):
        await sml.get_powermeter_watts()
    assert sml.stream_online() is False


async def test_stale_reading_raises() -> None:
    """A reading older than the age limit is refused, not served."""
    now = 1000.0
    sml = Sml("/dev/ttyUSB0")
    sml._clock = lambda: now
    await _feed(sml, _build_single_frame(100))
    assert await sml.get_powermeter_watts() == [100.0, 0.0, 0.0]
    now += 60
    with pytest.raises(ValueError, match="stale"):
        await sml.get_powermeter_watts()


async def _serve(
    connections: list[bytes | None],
) -> tuple[asyncio.AbstractServer, str]:
    """TCP server standing in for ser2net: the n-th connection gets the n-th
    payload and is then closed (``None``: accept and stay silent)."""
    queue = list(connections)

    async def handle(reader: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        payload = queue.pop(0) if queue else None
        if payload is None:
            await asyncio.sleep(3600)
            return
        w.write(payload)
        await w.drain()
        w.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"socket://127.0.0.1:{port}"


async def _wait_for_watts(sml: Sml, watts: float) -> None:
    for _ in range(200):
        try:
            if (await sml.get_powermeter_watts())[0] == watts:
                return
        except ValueError:
            pass
        await asyncio.sleep(0.02)
    raise AssertionError(f"never read {watts} W")


async def test_reconnects_after_connection_closes() -> None:
    """A dropped connection (e.g. ser2net closing it) is reopened by itself."""
    server, url = await _serve([_build_single_frame(100), _build_single_frame(200)])
    sml = Sml(url)
    with patch("astrameter.powermeter.sml.RECONNECT_DELAY_SECONDS", 0.05):
        try:
            await sml.start()
            await _wait_for_watts(sml, 100)
            await _wait_for_watts(sml, 200)
        finally:
            await sml.stop()
            server.close()


async def test_silent_connection_is_reopened() -> None:
    """An open but silent connection (half-open TCP) is treated as dead."""
    server, url = await _serve([None, _build_single_frame(150)])
    sml = Sml(url)
    with (
        patch("astrameter.powermeter.sml.RECONNECT_DELAY_SECONDS", 0.05),
        patch("astrameter.powermeter.sml._READ_TIMEOUT", 0.2),
    ):
        try:
            await sml.start()
            await _wait_for_watts(sml, 150)
        finally:
            await sml.stop()
            server.close()
