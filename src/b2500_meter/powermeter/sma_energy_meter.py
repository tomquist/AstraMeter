import asyncio
import contextlib
import socket
import struct

from b2500_meter.config.logger import logger

from .base import Powermeter

# SMA Speedwire multicast defaults
DEFAULT_MULTICAST_GROUP = "239.12.255.254"
DEFAULT_PORT = 9522

# SMA device SUSY IDs
SMA_SUSY_IDS = {
    270: "SMA Energy Meter 1.0",
    349: "SMA Energy Meter 2.0",
    372: "Sunny Home Manager 2.0",
    501: "Sunny Home Manager 2.0",
}

# OBIS channel identifiers for active power (4 bytes each, raw value / 10 = watts)
CHANNEL_TOTAL_POWER_PLUS = 0x00010400
CHANNEL_TOTAL_POWER_MINUS = 0x00020400
CHANNEL_L1_POWER_PLUS = 0x00150400
CHANNEL_L1_POWER_MINUS = 0x00160400
CHANNEL_L2_POWER_PLUS = 0x00290400
CHANNEL_L2_POWER_MINUS = 0x002A0400
CHANNEL_L3_POWER_PLUS = 0x003D0400
CHANNEL_L3_POWER_MINUS = 0x003E0400

POWER_DIVISOR = 10.0

# End-of-data marker
CHANNEL_END = 0x00000000

# Software version channel
CHANNEL_SOFTWARE_VERSION = 0x90000000


def _get_channel_data_length(identifier):
    """Determine data length for an OBIS channel identifier.

    The second byte of the identifier encodes the measurement type:
    - 0x04: instantaneous value (4 bytes)
    - 0x08: counter/meter value (8 bytes)
    """
    if identifier == CHANNEL_END:
        return 0
    type_byte = (identifier >> 8) & 0xFF
    if type_byte == 0x04:
        return 4
    elif type_byte == 0x08:
        return 8
    elif identifier == CHANNEL_SOFTWARE_VERSION:
        return 4
    return 4


class _SmaProtocol(asyncio.DatagramProtocol):
    def __init__(self, meter: "SmaEnergyMeter"):
        self.meter = meter

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            self.meter._handle_packet(data)
        except Exception as e:
            logger.debug(f"SMA Energy Meter: dropping invalid packet: {e}")

    def error_received(self, exc: Exception) -> None:
        logger.debug(f"SMA Energy Meter: OS error: {exc}")

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            logger.warning(f"SMA Energy Meter: connection lost: {exc}")


class SmaEnergyMeter(Powermeter):
    def __init__(
        self,
        multicast_group=DEFAULT_MULTICAST_GROUP,
        port=DEFAULT_PORT,
        serial_number=0,
        interface="",
    ):
        self.multicast_group = multicast_group
        self.port = port
        self.serial_number = serial_number
        self.interface = interface
        self.values: list[float] | None = None
        self._async_message_event: asyncio.Event | None = None
        self._detected_serial: int | None = None
        self._transport: asyncio.DatagramTransport | None = None

    async def start(self):
        self._async_message_event = asyncio.Event()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                with contextlib.suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            sock.bind(("", self.port))

            interface_ip = self.interface if self.interface else "0.0.0.0"
            mreq = struct.pack(
                "4s4s",
                socket.inet_aton(self.multicast_group),
                socket.inet_aton(interface_ip),
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)

            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _SmaProtocol(self),
                sock=sock,
            )
        except BaseException:
            sock.close()
            raise
        self._transport = transport
        logger.info(
            f"SMA Energy Meter: listening on {self.multicast_group}:{self.port}"
        )

    async def stop(self):
        if self._transport:
            self._transport.close()
            self._transport = None

    def _handle_packet(self, data):
        if len(data) < 28:
            return

        # Validate magic constant "SMA\0"
        if data[0:4] != b"SMA\x00":
            return

        # Validate tag42
        if data[5] != 0x04 or data[6] != 0x02:
            return

        # Validate protocol ID (0x6069 = energy meter)
        protocol_id = struct.unpack_from(">H", data, 16)[0]
        if protocol_id != 0x6069:
            return

        # Extract device identifiers
        susy_id = struct.unpack_from(">H", data, 18)[0]
        serial = struct.unpack_from(">I", data, 20)[0]

        # Filter by serial number
        if self.serial_number != 0:
            if serial != self.serial_number:
                return
        else:
            if self._detected_serial is None:
                device_name = SMA_SUSY_IDS.get(susy_id)
                if device_name is None:
                    return
                self._detected_serial = serial
                logger.info(
                    f"SMA Energy Meter: auto-detected {device_name} "
                    f"with serial {serial}"
                )
            elif serial != self._detected_serial:
                return

        self._parse_channels(data)

    def _parse_channels(self, data):
        raw = {}
        pos = 28
        data_len = len(data)
        has_phase_data = False

        while pos + 4 <= data_len:
            identifier = struct.unpack_from(">I", data, pos)[0]

            if identifier == CHANNEL_END:
                break

            channel_len = _get_channel_data_length(identifier)

            if pos + 4 + channel_len > data_len:
                break

            if identifier in (
                CHANNEL_TOTAL_POWER_PLUS,
                CHANNEL_TOTAL_POWER_MINUS,
                CHANNEL_L1_POWER_PLUS,
                CHANNEL_L1_POWER_MINUS,
                CHANNEL_L2_POWER_PLUS,
                CHANNEL_L2_POWER_MINUS,
                CHANNEL_L3_POWER_PLUS,
                CHANNEL_L3_POWER_MINUS,
            ):
                value = struct.unpack_from(">I", data, pos + 4)[0]
                raw[identifier] = value / POWER_DIVISOR
                if identifier in (
                    CHANNEL_L1_POWER_PLUS,
                    CHANNEL_L1_POWER_MINUS,
                    CHANNEL_L2_POWER_PLUS,
                    CHANNEL_L2_POWER_MINUS,
                    CHANNEL_L3_POWER_PLUS,
                    CHANNEL_L3_POWER_MINUS,
                ):
                    has_phase_data = True

            pos += 4 + channel_len

        if has_phase_data:
            l1 = raw.get(CHANNEL_L1_POWER_PLUS, 0) - raw.get(CHANNEL_L1_POWER_MINUS, 0)
            l2 = raw.get(CHANNEL_L2_POWER_PLUS, 0) - raw.get(CHANNEL_L2_POWER_MINUS, 0)
            l3 = raw.get(CHANNEL_L3_POWER_PLUS, 0) - raw.get(CHANNEL_L3_POWER_MINUS, 0)
            values = [l1, l2, l3]
        elif CHANNEL_TOTAL_POWER_PLUS in raw or CHANNEL_TOTAL_POWER_MINUS in raw:
            total = raw.get(CHANNEL_TOTAL_POWER_PLUS, 0) - raw.get(
                CHANNEL_TOTAL_POWER_MINUS, 0
            )
            values = [total]
        else:
            return

        self.values = values
        if self._async_message_event is not None:
            self._async_message_event.set()

    async def get_powermeter_watts(self) -> list[float]:
        if self.values is not None:
            return list(self.values)
        raise ValueError("No value received from SMA Energy Meter")

    async def wait_for_message(self, timeout=5):
        if self._async_message_event is None:
            raise RuntimeError("start() must be called before wait_for_message()")
        try:
            await asyncio.wait_for(self._async_message_event.wait(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for SMA Energy Meter data") from None
