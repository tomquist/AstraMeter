import struct
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import ModbusTcpServer

from b2500_meter.powermeter import ModbusPowermeter

# ---------------------------------------------------------------------------
# Tier 1 — Migration-critical (mock-based)
# ---------------------------------------------------------------------------


async def test_get_powermeter_watts():
    with patch("b2500_meter.powermeter.modbus.AsyncModbusTcpClient") as MockClient:
        mock_client = MockClient.return_value
        mock_client.connect = AsyncMock(return_value=True)
        mock_client.close = MagicMock()

        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_result.registers = [500]
        mock_client.read_holding_registers = AsyncMock(return_value=mock_result)

        pm = ModbusPowermeter("192.168.1.14", 502, 1, 0, 1)
        await pm.start()
        assert await pm.get_powermeter_watts_async() == [500.0]
        MockClient.assert_called_with("192.168.1.14", port=502)
        mock_client.read_holding_registers.assert_called_once_with(0, 1, slave=1)
        await pm.stop()


async def test_float32():
    with patch("b2500_meter.powermeter.modbus.AsyncModbusTcpClient") as MockClient:
        mock_client = MockClient.return_value
        mock_client.connect = AsyncMock(return_value=True)
        mock_client.close = MagicMock()

        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_result.registers = [0x4120, 0x0000]
        mock_client.read_holding_registers = AsyncMock(return_value=mock_result)

        pm = ModbusPowermeter(
            "192.168.1.14",
            502,
            1,
            0,
            2,
            data_type="FLOAT32",
            byte_order="BIG",
            word_order="BIG",
        )
        await pm.start()
        assert await pm.get_powermeter_watts_async() == [10.0]
        mock_client.read_holding_registers.assert_called_once_with(0, 2, slave=1)
        await pm.stop()


async def test_input_registers():
    with patch("b2500_meter.powermeter.modbus.AsyncModbusTcpClient") as MockClient:
        mock_client = MockClient.return_value
        mock_client.connect = AsyncMock(return_value=True)
        mock_client.close = MagicMock()

        mock_result = MagicMock()
        mock_result.isError.return_value = False
        mock_result.registers = [500]
        mock_client.read_input_registers = AsyncMock(return_value=mock_result)

        pm = ModbusPowermeter("192.168.1.14", 502, 1, 0, 1, register_type="INPUT")
        await pm.start()
        assert await pm.get_powermeter_watts_async() == [500.0]
        mock_client.read_input_registers.assert_called_once_with(0, 1, slave=1)
        await pm.stop()


async def test_start_creates_client_and_connects():
    pm = ModbusPowermeter("192.168.1.14", 502, 1, 0, 1)
    assert pm.client is None
    with patch("b2500_meter.powermeter.modbus.AsyncModbusTcpClient") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.connect = AsyncMock(return_value=True)
        await pm.start()
        MockClient.assert_called_once_with("192.168.1.14", port=502)
        mock_instance.connect.assert_awaited_once()
        assert pm.client is mock_instance


async def test_read_before_start_raises():
    pm = ModbusPowermeter("192.168.1.14", 502, 1, 0, 1)
    with pytest.raises(RuntimeError, match="Client not started"):
        await pm.get_powermeter_watts_async()


# ---------------------------------------------------------------------------
# Tier 2 — Lifecycle correctness (mock-based)
# ---------------------------------------------------------------------------


async def test_stop_closes_and_resets():
    with patch("b2500_meter.powermeter.modbus.AsyncModbusTcpClient") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.connect = AsyncMock(return_value=True)
        mock_instance.close = MagicMock()

        pm = ModbusPowermeter("192.168.1.14", 502, 1, 0, 1)
        await pm.start()
        await pm.stop()
        mock_instance.close.assert_called_once()
        assert pm.client is None


async def test_start_is_idempotent():
    with patch("b2500_meter.powermeter.modbus.AsyncModbusTcpClient") as MockClient:
        mock_instance = MockClient.return_value
        mock_instance.connect = AsyncMock(return_value=True)

        pm = ModbusPowermeter("192.168.1.14", 502, 1, 0, 1)
        await pm.start()
        await pm.start()
        MockClient.assert_called_once()
        mock_instance.connect.assert_awaited_once()


async def test_read_error_raises():
    with patch("b2500_meter.powermeter.modbus.AsyncModbusTcpClient") as MockClient:
        mock_client = MockClient.return_value
        mock_client.connect = AsyncMock(return_value=True)
        mock_client.close = MagicMock()

        mock_result = MagicMock()
        mock_result.isError.return_value = True
        mock_client.read_holding_registers = AsyncMock(return_value=mock_result)

        pm = ModbusPowermeter("192.168.1.14", 502, 1, 0, 1)
        await pm.start()
        with pytest.raises(Exception, match="Error reading Modbus data"):
            await pm.get_powermeter_watts_async()
        await pm.stop()


# ---------------------------------------------------------------------------
# Tier 3 — E2E with local Modbus TCP server
# ---------------------------------------------------------------------------


def _float32_to_registers(value: float) -> list[int]:
    """Encode a float as two big-endian 16-bit registers."""
    packed = struct.pack(">f", value)
    return [int.from_bytes(packed[0:2], "big"), int.from_bytes(packed[2:4], "big")]


@pytest.fixture
async def modbus_server():
    """Start a local Modbus TCP server on an ephemeral port."""
    # Holding registers: address 0+ with some known values
    hr_values = [0] * 100
    hr_values[0] = 500  # UINT16 at address 0
    # FLOAT32 for 10.0 at address 10 (two registers)
    float_regs = _float32_to_registers(10.0)
    hr_values[10] = float_regs[0]
    hr_values[11] = float_regs[1]

    # Input registers: address 0+ with known values
    ir_values = [0] * 100
    ir_values[0] = 750

    store = ModbusSlaveContext(
        hr=ModbusSequentialDataBlock(0, hr_values),
        ir=ModbusSequentialDataBlock(0, ir_values),
        zero_mode=True,
    )
    context = ModbusServerContext(slaves=store, single=True)
    server = ModbusTcpServer(context, address=("127.0.0.1", 0))
    await server.listen()
    port = server.transport.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    yield port
    await server.shutdown()


async def test_e2e_uint16_holding(modbus_server: int):
    pm = ModbusPowermeter("127.0.0.1", modbus_server, 1, 0, 1)
    await pm.start()
    try:
        result = await pm.get_powermeter_watts_async()
        assert result == [500.0]
    finally:
        await pm.stop()


async def test_e2e_float32(modbus_server: int):
    pm = ModbusPowermeter(
        "127.0.0.1",
        modbus_server,
        1,
        10,
        2,
        data_type="FLOAT32",
        byte_order="BIG",
        word_order="BIG",
    )
    await pm.start()
    try:
        result = await pm.get_powermeter_watts_async()
        assert result == [10.0]
    finally:
        await pm.stop()


async def test_e2e_input_registers(modbus_server: int):
    pm = ModbusPowermeter(
        "127.0.0.1",
        modbus_server,
        1,
        0,
        1,
        register_type="INPUT",
    )
    await pm.start()
    try:
        result = await pm.get_powermeter_watts_async()
        assert result == [750.0]
    finally:
        await pm.stop()
