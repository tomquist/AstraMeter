from pymodbus.client import AsyncModbusTcpClient
from pymodbus.constants import Endian
from pymodbus.payload import BinaryPayloadDecoder

from .base import Powermeter

DATA_TYPE_DECODERS = {
    "FLOAT32": "decode_32bit_float",
    "INT16": "decode_16bit_int",
    "UINT16": "decode_16bit_uint",
    "INT32": "decode_32bit_int",
    "UINT32": "decode_32bit_uint",
}

BYTE_ORDERS = {
    "BIG": Endian.BIG,
    "LITTLE": Endian.LITTLE,
}


REGISTER_TYPES = {
    "HOLDING": "read_holding_registers",
    "INPUT": "read_input_registers",
}


class ModbusPowermeter(Powermeter):
    def __init__(
        self,
        host,
        port,
        unit_id,
        address,
        count,
        data_type="UINT16",
        byte_order="BIG",
        word_order="BIG",
        register_type="HOLDING",
    ):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self.address = address
        self.count = count
        self.data_type = data_type.upper()
        self.byte_order = byte_order.upper()
        self.word_order = word_order.upper()

        self._byte_order = BYTE_ORDERS.get(self.byte_order, Endian.BIG)
        self._word_order = BYTE_ORDERS.get(self.word_order, Endian.BIG)
        self._decode_method = DATA_TYPE_DECODERS.get(self.data_type)
        if not self._decode_method:
            raise ValueError(f"Unsupported data type: {data_type}")

        self.register_type = register_type.upper()
        self._read_method = REGISTER_TYPES.get(self.register_type)
        if not self._read_method:
            raise ValueError(f"Unsupported register type: {register_type}")

        self.client: AsyncModbusTcpClient | None = None

    async def start(self) -> None:
        if self.client:
            return
        self.client = AsyncModbusTcpClient(self.host, port=self.port)
        if not await self.client.connect():
            self.client = None
            raise ConnectionError(f"Failed to connect to {self.host}:{self.port}")

    async def stop(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    async def get_powermeter_watts(self) -> list[float]:
        if not self.client:
            raise RuntimeError("Client not started; call start() first")
        read = getattr(self.client, self._read_method)
        result = await read(self.address, self.count, slave=self.unit_id)
        if result.isError():
            raise Exception("Error reading Modbus data")
        decoder = BinaryPayloadDecoder.fromRegisters(
            result.registers,
            byteorder=self._byte_order,
            wordorder=self._word_order,
        )
        value = getattr(decoder, self._decode_method)()
        return [float(value)]
