import asyncio
import struct
import unittest

import pytest

from b2500_meter.powermeter.sma_energy_meter import (
    CHANNEL_END,
    CHANNEL_L1_POWER_MINUS,
    CHANNEL_L1_POWER_PLUS,
    CHANNEL_L2_POWER_MINUS,
    CHANNEL_L2_POWER_PLUS,
    CHANNEL_L3_POWER_MINUS,
    CHANNEL_L3_POWER_PLUS,
    CHANNEL_SOFTWARE_VERSION,
    CHANNEL_TOTAL_POWER_MINUS,
    CHANNEL_TOTAL_POWER_PLUS,
    SMA_SUSY_IDS,
    SmaEnergyMeter,
    _get_channel_data_length,
)

# Energy counter channels (8 bytes)
CHANNEL_TOTAL_ENERGY_PLUS = 0x00010800
CHANNEL_L1_ENERGY_PLUS = 0x00150800


def _build_header(susy_id=349, serial=3000012345, protocol_id=0x6069):
    """Build a valid SMA Speedwire header (28 bytes)."""
    header = bytearray(28)
    # Magic "SMA\0"
    header[0:4] = b"SMA\x00"
    # Tag42: length word with bytes[5]=0x04, bytes[6]=0x02
    header[4] = 0x00
    header[5] = 0x04
    header[6] = 0x02
    header[7] = 0x00
    # Default channel (bytes 8-11)
    struct.pack_into(">I", header, 8, 1)
    # Padding (bytes 12-15)
    # Protocol ID (bytes 16-17)
    struct.pack_into(">H", header, 16, protocol_id)
    # SUSY ID (bytes 18-19)
    struct.pack_into(">H", header, 18, susy_id)
    # Serial number (bytes 20-23)
    struct.pack_into(">I", header, 20, serial)
    # Measuring time (bytes 24-27)
    struct.pack_into(">I", header, 24, 0)
    return bytes(header)


def _build_channel(identifier, value, length=4):
    """Build an OBIS channel entry (4-byte identifier + value)."""
    data = struct.pack(">I", identifier)
    if length == 4:
        data += struct.pack(">I", value)
    elif length == 8:
        data += struct.pack(">Q", value)
    return data


def _build_end_marker():
    return struct.pack(">I", CHANNEL_END)


def _build_packet(channels, susy_id=349, serial=3000012345, protocol_id=0x6069):
    """Build a complete SMA Speedwire packet."""
    header = _build_header(susy_id=susy_id, serial=serial, protocol_id=protocol_id)
    body = b""
    for ch in channels:
        body += ch
    body += _build_end_marker()
    return header + body


def _create_meter(**kwargs):
    """Create an SmaEnergyMeter without starting the listener."""
    return SmaEnergyMeter(**kwargs)


class TestGetChannelDataLength(unittest.TestCase):
    def test_type_04_returns_4(self):
        self.assertEqual(_get_channel_data_length(0x00010400), 4)

    def test_type_08_returns_8(self):
        self.assertEqual(_get_channel_data_length(0x00010800), 8)

    def test_end_marker_returns_0(self):
        self.assertEqual(_get_channel_data_length(CHANNEL_END), 0)

    def test_software_version_returns_4(self):
        self.assertEqual(_get_channel_data_length(CHANNEL_SOFTWARE_VERSION), 4)


class TestHandlePacket(unittest.TestCase):
    def test_three_phase_consumption(self):
        meter = _create_meter()
        packet = _build_packet(
            [
                _build_channel(CHANNEL_L1_POWER_PLUS, 1000),  # 100.0 W
                _build_channel(CHANNEL_L1_POWER_MINUS, 0),
                _build_channel(CHANNEL_L2_POWER_PLUS, 2000),  # 200.0 W
                _build_channel(CHANNEL_L2_POWER_MINUS, 0),
                _build_channel(CHANNEL_L3_POWER_PLUS, 3000),  # 300.0 W
                _build_channel(CHANNEL_L3_POWER_MINUS, 0),
            ]
        )
        meter._handle_packet(packet)
        self.assertEqual(meter.values, [100.0, 200.0, 300.0])

    def test_three_phase_net_power(self):
        meter = _create_meter()
        packet = _build_packet(
            [
                _build_channel(CHANNEL_L1_POWER_PLUS, 1500),  # 150.0 W
                _build_channel(CHANNEL_L1_POWER_MINUS, 500),  #  50.0 W
                _build_channel(CHANNEL_L2_POWER_PLUS, 0),
                _build_channel(CHANNEL_L2_POWER_MINUS, 2000),  # -200.0 W
                _build_channel(CHANNEL_L3_POWER_PLUS, 1000),
                _build_channel(CHANNEL_L3_POWER_MINUS, 1000),  # 0.0 W
            ]
        )
        meter._handle_packet(packet)
        assert meter.values is not None
        self.assertAlmostEqual(meter.values[0], 100.0)
        self.assertAlmostEqual(meter.values[1], -200.0)
        self.assertAlmostEqual(meter.values[2], 0.0)

    def test_total_only_fallback(self):
        meter = _create_meter()
        packet = _build_packet(
            [
                _build_channel(CHANNEL_TOTAL_POWER_PLUS, 5000),  # 500.0 W
                _build_channel(CHANNEL_TOTAL_POWER_MINUS, 0),
            ]
        )
        meter._handle_packet(packet)
        self.assertEqual(meter.values, [500.0])

    def test_total_net_production(self):
        meter = _create_meter()
        packet = _build_packet(
            [
                _build_channel(CHANNEL_TOTAL_POWER_PLUS, 100),
                _build_channel(CHANNEL_TOTAL_POWER_MINUS, 3000),
            ]
        )
        meter._handle_packet(packet)
        assert meter.values is not None
        self.assertAlmostEqual(meter.values[0], -290.0)

    def test_invalid_magic_ignored(self):
        meter = _create_meter()
        packet = b"XYZ\x00" + b"\x00" * 24
        meter._handle_packet(packet)
        self.assertIsNone(meter.values)

    def test_invalid_tag42_ignored(self):
        meter = _create_meter()
        packet = bytearray(_build_header())
        packet[5] = 0x00
        packet[6] = 0x00
        packet += _build_end_marker()
        meter._handle_packet(bytes(packet))
        self.assertIsNone(meter.values)

    def test_wrong_protocol_id_ignored(self):
        meter = _create_meter()
        packet = _build_packet(
            [_build_channel(CHANNEL_TOTAL_POWER_PLUS, 1000)],
            protocol_id=0x6065,  # inverter, not energy meter
        )
        meter._handle_packet(packet)
        self.assertIsNone(meter.values)

    def test_too_short_packet_ignored(self):
        meter = _create_meter()
        meter._handle_packet(b"SMA\x00" + b"\x00" * 10)
        self.assertIsNone(meter.values)

    def test_serial_filter_match(self):
        meter = _create_meter(serial_number=12345)
        packet = _build_packet(
            [_build_channel(CHANNEL_TOTAL_POWER_PLUS, 1000)],
            serial=12345,
        )
        meter._handle_packet(packet)
        self.assertEqual(meter.values, [100.0])

    def test_serial_filter_mismatch(self):
        meter = _create_meter(serial_number=12345)
        packet = _build_packet(
            [_build_channel(CHANNEL_TOTAL_POWER_PLUS, 1000)],
            serial=99999,
        )
        meter._handle_packet(packet)
        self.assertIsNone(meter.values)

    def test_auto_detect_locks_serial(self):
        meter = _create_meter()
        # First packet from meter with serial 11111
        packet1 = _build_packet(
            [_build_channel(CHANNEL_TOTAL_POWER_PLUS, 1000)],
            serial=11111,
            susy_id=349,
        )
        meter._handle_packet(packet1)
        self.assertEqual(meter._detected_serial, 11111)
        self.assertEqual(meter.values, [100.0])

        # Second packet from different serial should be ignored
        packet2 = _build_packet(
            [_build_channel(CHANNEL_TOTAL_POWER_PLUS, 9999)],
            serial=22222,
            susy_id=349,
        )
        meter._handle_packet(packet2)
        # Should still have old value
        self.assertEqual(meter.values, [100.0])

    def test_auto_detect_rejects_unknown_susy_id(self):
        meter = _create_meter()
        packet = _build_packet(
            [_build_channel(CHANNEL_TOTAL_POWER_PLUS, 1000)],
            serial=11111,
            susy_id=999,
        )
        meter._handle_packet(packet)
        self.assertIsNone(meter._detected_serial)
        self.assertIsNone(meter.values)

    def test_energy_channels_skipped_correctly(self):
        """8-byte energy channels should be skipped without breaking power parsing."""
        meter = _create_meter()
        packet = _build_packet(
            [
                _build_channel(CHANNEL_L1_POWER_PLUS, 1000),
                _build_channel(CHANNEL_L1_POWER_MINUS, 0),
                # 8-byte energy counter interspersed
                _build_channel(CHANNEL_TOTAL_ENERGY_PLUS, 123456789, length=8),
                _build_channel(CHANNEL_L2_POWER_PLUS, 2000),
                _build_channel(CHANNEL_L2_POWER_MINUS, 0),
                _build_channel(CHANNEL_L1_ENERGY_PLUS, 987654321, length=8),
                _build_channel(CHANNEL_L3_POWER_PLUS, 3000),
                _build_channel(CHANNEL_L3_POWER_MINUS, 0),
            ]
        )
        meter._handle_packet(packet)
        self.assertEqual(meter.values, [100.0, 200.0, 300.0])

    def test_phase_data_preferred_over_total(self):
        """When both phase and total data present, phase data is used."""
        meter = _create_meter()
        packet = _build_packet(
            [
                _build_channel(CHANNEL_TOTAL_POWER_PLUS, 6000),
                _build_channel(CHANNEL_TOTAL_POWER_MINUS, 0),
                _build_channel(CHANNEL_L1_POWER_PLUS, 1000),
                _build_channel(CHANNEL_L1_POWER_MINUS, 0),
                _build_channel(CHANNEL_L2_POWER_PLUS, 2000),
                _build_channel(CHANNEL_L2_POWER_MINUS, 0),
                _build_channel(CHANNEL_L3_POWER_PLUS, 3000),
                _build_channel(CHANNEL_L3_POWER_MINUS, 0),
            ]
        )
        meter._handle_packet(packet)
        assert meter.values is not None
        self.assertEqual(len(meter.values), 3)
        self.assertEqual(meter.values, [100.0, 200.0, 300.0])

    def test_software_version_channel_skipped(self):
        meter = _create_meter()
        packet = _build_packet(
            [
                _build_channel(CHANNEL_SOFTWARE_VERSION, 0x01020304),
                _build_channel(CHANNEL_TOTAL_POWER_PLUS, 5000),
                _build_channel(CHANNEL_TOTAL_POWER_MINUS, 0),
            ]
        )
        meter._handle_packet(packet)
        self.assertEqual(meter.values, [500.0])


class TestGetPowermeterWattsAsync:
    async def test_no_data_raises(self):
        meter = _create_meter()
        with pytest.raises(ValueError):
            await meter.get_powermeter_watts_async()

    async def test_returns_copy(self):
        meter = _create_meter()
        meter.values = [100.0, 200.0, 300.0]
        result = await meter.get_powermeter_watts_async()
        result[0] = 999
        assert meter.values[0] == 100.0


class TestWaitForMessageAsync:
    async def test_timeout_raises(self):
        meter = _create_meter()
        meter._async_message_event = asyncio.Event()
        with pytest.raises(TimeoutError):
            await meter.wait_for_message_async(timeout=0)

    async def test_returns_when_data_available(self):
        meter = _create_meter()
        meter._async_message_event = asyncio.Event()
        packet = _build_packet(
            [
                _build_channel(CHANNEL_TOTAL_POWER_PLUS, 1000),
                _build_channel(CHANNEL_TOTAL_POWER_MINUS, 0),
            ]
        )
        meter._handle_packet(packet)
        await meter.wait_for_message_async(timeout=1)
        result = await meter.get_powermeter_watts_async()
        assert result == [100.0]

    async def test_not_started_raises(self):
        meter = _create_meter()
        with pytest.raises(RuntimeError):
            await meter.wait_for_message_async(timeout=0)


class TestLifecycle:
    async def test_stop_closes_transport(self):
        meter = _create_meter()
        # Simulate a started meter with a mock transport
        meter._async_message_event = asyncio.Event()

        class FakeTransport:
            def __init__(self):
                self.closed = False

            def close(self):
                self.closed = True

        transport = FakeTransport()
        meter._transport = transport  # type: ignore[assignment]
        await meter.stop()
        assert transport.closed
        assert meter._transport is None

    async def test_stop_when_not_started(self):
        meter = _create_meter()
        await meter.stop()  # should not raise


class TestDeviceNames(unittest.TestCase):
    def test_known_susy_ids(self):
        self.assertEqual(SMA_SUSY_IDS[270], "SMA Energy Meter 1.0")
        self.assertEqual(SMA_SUSY_IDS[349], "SMA Energy Meter 2.0")
        self.assertEqual(SMA_SUSY_IDS[372], "Sunny Home Manager 2.0")
        self.assertEqual(SMA_SUSY_IDS[501], "Sunny Home Manager 2.0")

    def test_unknown_susy_id(self):
        self.assertNotIn(999, SMA_SUSY_IDS)


if __name__ == "__main__":
    unittest.main()
