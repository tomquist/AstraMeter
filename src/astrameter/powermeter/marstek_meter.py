"""A real Marstek CT (CT002, CT003) read over its own UDP protocol.

AstraMeter polls the CT the way a battery does and takes the measured phase
powers from the reply. What the CT accepts and how it replies is documented in
docs/ct002-ct003-protocol.md; the short version this module relies on:

* The CT answers any battery MAC, without pairing, as long as the request
  names the CT's own type and either its MAC or the wildcard MAC. It replies to
  the request's source address and port, and names its own type and MAC.
* A request with phase ``0``, power ``0`` and ``participate = 0`` is counted in
  no bucket, so the batteries the CT still serves see no extra reporter.

The reader must never read an AstraMeter emulator, least of all its own: the
reading would be derived from the reading.  Hence the guards below: unicast
only, never to an address of this machine, replies only from the CT, and never
a reply naming an emulated CT's MAC.  The emulator in turn drops every request
whose battery MAC starts with ``READER_MAC_PREFIX``.
"""

import asyncio
import contextlib
import ipaddress
import logging
import time
import uuid
from collections.abc import Callable, Collection

from astrameter.ct002.ct002 import WILDCARD_CT_MAC, normalize_ct_mac
from astrameter.ct002.protocol import (
    READER_MAC_PREFIX,
    build_payload,
    parse_int,
    parse_request,
)
from astrameter.marstek_api import MANAGED_MAC_PREFIX

from .base import PushPowermeter, stream_fresh

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")

CT_PORT = 12345

#: CT types tried in turn until the CT answers one: a CT silently ignores a
#: request naming another type.  The original CT002, the newer CT002
#: (TPM2-100CT) and the CT003.
CT_TYPES = ("HME-4", "TPM2-0", "HME-3")

#: The battery type the reader announces.  The CT only echoes it back; this one
#: is known to be answered by every CT type, including TPM2-0.
READER_DEVICE_TYPE = "VNSE3-0"

DEFAULT_POLL_INTERVAL = 1.0

# The least time between a request and one sent early for a waiting reader: the
# pause a current CT takes after each reply, so readers can't ask faster than
# it answers.
KICK_MIN_INTERVAL_SECONDS = 0.5

# Seconds without a reply before the last reading counts as stale.  A current
# CT answers about twice a second in total, shared by everything polling it.
DEFAULT_MAX_AGE_SECONDS = 10.0

# A reply carries 24 fields (28 with the energy registers); the phase powers
# are fields 5-7 (0-based 4-6).
_MIN_RESPONSE_FIELDS = 24
_PHASE_FIELDS = slice(4, 7)
_COUNT_FIELDS = slice(8, 12)

# How often to repeat the "no reply" warning while the CT stays silent.
_SILENCE_WARNING_SECONDS = 60.0


def default_reader_mac() -> str:
    """This host's reader MAC: the reserved prefix plus a stable suffix.

    Stable across restarts so the CT reuses its slot, and per host so two
    installations polling one CT don't share a slot (the CT answers a slot at
    its latest address only).
    """
    return f"{READER_MAC_PREFIX}{uuid.getnode() & 0xFFFFFF:06x}"


def _is_own_address(peer: str, local: str) -> bool:
    addr = ipaddress.ip_address(peer)
    return (
        addr.is_loopback or addr.is_unspecified or addr.is_multicast or (peer == local)
    )


class _MarstekMeterProtocol(asyncio.DatagramProtocol):
    def __init__(self, meter: "MarstekMeter") -> None:
        self.meter = meter

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            self.meter._handle_reply(data)
        except Exception as e:
            logger.debug("Marstek meter: dropping invalid reply: %s", e)

    def error_received(self, exc: Exception) -> None:
        # A connected UDP socket reports the CT's ICMP "port unreachable" here.
        logger.debug("Marstek meter: %s", exc)


class MarstekMeter(PushPowermeter):
    _TIMEOUT_MESSAGE = "Timeout waiting for a reply from the Marstek meter"

    def __init__(
        self,
        ip: str,
        mac: str = "",
        ct_type: str = "",
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        *,
        own_ct_macs: Collection[str] = (),
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        reader_mac: str | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        if not ip:
            raise ValueError("MARSTEK_METER needs the IP of the CT")
        self.ip = ip
        self._ct_mac = normalize_ct_mac(mac)
        self._ct_type = ct_type.strip()
        self._poll_interval = max(0.1, poll_interval)
        self._own_ct_macs = {normalize_ct_mac(m) for m in own_ct_macs if m.strip()}
        if self._ct_mac and self._ct_mac in self._own_ct_macs:
            raise ValueError(
                f"MARSTEK_METER's MAC {self._ct_mac} is also the CT_MAC of the "
                "emulated CT; give the emulated CT a MAC of its own"
            )
        self._max_age_seconds = max(0.0, max_age_seconds)
        self.reader_mac = (reader_mac or default_reader_mac()).lower()
        self._clock = clock or time.monotonic
        self.values: list[float] | None = None
        self._last_reply_monotonic: float | None = None
        self._last_request_monotonic: float | None = None
        self._attempt = 0
        self._paired_elsewhere = 0
        self._refused_self = False
        self._silent_since: float | None = None
        self._transport: asyncio.DatagramTransport | None = None
        self._poll_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._message_event.clear()
        loop = asyncio.get_running_loop()
        # A connected socket: the kernel delivers datagrams from the CT's
        # address only, and refuses a broadcast destination outright.
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _MarstekMeterProtocol(self), remote_addr=(self.ip, CT_PORT)
        )
        peer = transport.get_extra_info("peername")[0]
        local = transport.get_extra_info("sockname")[0]
        if _is_own_address(peer, local):
            transport.close()
            raise ValueError(
                f"MARSTEK_METER IP {self.ip} is this machine; it must be the "
                "address of a real Marstek CT"
            )
        self._transport = transport
        self._silent_since = self._clock()
        self._poll_task = asyncio.create_task(self._poll_loop())
        logger.info("Marstek meter: polling %s:%d", self.ip, CT_PORT)

    async def stop(self) -> None:
        if self._poll_task is not None:
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
            self._poll_task = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    async def _poll_loop(self) -> None:
        while True:
            self._send_request()
            self._warn_if_silent()
            await asyncio.sleep(self._poll_interval)

    def _request_type(self) -> str:
        if self._ct_type:
            return self._ct_type
        ct_type = CT_TYPES[self._attempt % len(CT_TYPES)]
        self._attempt += 1
        return ct_type

    def build_request(self) -> bytes:
        """The next poll: an unassigned, idle battery that does not participate."""
        return bytes(
            build_payload(
                [
                    READER_DEVICE_TYPE,
                    self.reader_mac,
                    self._request_type(),
                    self._ct_mac or WILDCARD_CT_MAC,
                    "0",
                    "0",
                    "0",
                ]
            )
        )

    def _send_request(self) -> None:
        if self._transport is not None:
            self._transport.sendto(self.build_request())
            self._last_request_monotonic = self._clock()

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        # Ask now rather than at the next tick, so a reader waiting for a fresh
        # reading gets it as soon as the CT replies.  A request while ours is
        # still unanswered costs the CT nothing (a slot gets one reply however
        # often it asks); the interval keeps the rest at the CT's own pace.
        last = self._last_request_monotonic
        if last is None or self._clock() - last >= KICK_MIN_INTERVAL_SECONDS:
            self._send_request()
        await super().wait_for_next_message(timeout)

    def _warn_if_silent(self) -> None:
        if self._silent_since is None:
            return
        if self._clock() - self._silent_since < _SILENCE_WARNING_SECONDS:
            return
        logger.warning(
            "Marstek meter: no reply from %s for %.0f s. Check that IP is the "
            "CT's address%s.",
            self.ip,
            self._clock() - self._silent_since,
            " and MAC/TYPE match it" if self._ct_mac or self._ct_type else "",
        )
        self._silent_since = self._clock()

    def _handle_reply(self, data: bytes) -> None:
        fields, error = parse_request(data)
        if fields is None:
            logger.debug("Marstek meter: invalid reply: %s", error)
            return
        if len(fields) < _MIN_RESPONSE_FIELDS:
            logger.debug("Marstek meter: reply has %d fields", len(fields))
            return
        ct_type, ct_mac, _, battery_mac = fields[:4]
        ct_mac = normalize_ct_mac(ct_mac)
        if battery_mac.lower() != self.reader_mac:
            return
        if self._is_emulated(ct_mac):
            if not self._refused_self:
                logger.error(
                    "Marstek meter: %s answered as CT %s, which is an AstraMeter "
                    "emulator, not a real CT. Refusing its readings.",
                    self.ip,
                    ct_mac,
                )
                self._refused_self = True
            return
        if self._ct_mac and ct_mac != self._ct_mac:
            logger.debug("Marstek meter: ignoring reply from CT %s", ct_mac)
            return
        # Strictly: parse_int would read a garbled field as a fresh 0 W.
        try:
            phases = [float(int(v)) for v in fields[_PHASE_FIELDS]]
        except ValueError:
            logger.debug(
                "Marstek meter: ignoring reply with phase powers %s",
                fields[_PHASE_FIELDS],
            )
            return

        if not self._ct_mac or not self._ct_type:
            logger.info("Marstek meter: found %s CT %s at %s", ct_type, ct_mac, self.ip)
        self._ct_mac = ct_mac
        self._ct_type = ct_type
        self._track_other_batteries(fields)
        self.values = phases
        self._last_reply_monotonic = self._clock()
        self._silent_since = None
        self._message_event.set()

    def _is_emulated(self, ct_mac: str) -> bool:
        # A real CT always names its own MAC, never the wildcard; an emulator
        # without CT_MAC echoes whatever MAC the request named.
        return (
            ct_mac == WILDCARD_CT_MAC
            or ct_mac.startswith(MANAGED_MAC_PREFIX)
            or ct_mac in self._own_ct_macs
        )

    def _track_other_batteries(self, fields: list[str]) -> None:
        # The reader is counted in no bucket, so any count is another battery
        # still steering against the real CT, alongside AstraMeter.
        count = sum(parse_int(v) for v in fields[_COUNT_FIELDS])
        if count and not self._paired_elsewhere:
            logger.warning(
                "Marstek meter: %d battery/batteries still use the real CT at %s "
                "directly. Pair them with AstraMeter's CT instead, or both will "
                "steer them.",
                count,
                self.ip,
            )
        self._paired_elsewhere = count

    def stream_online(self) -> bool | None:
        return stream_fresh(
            self._last_reply_monotonic, self._max_age_seconds, self._clock
        )

    async def get_powermeter_watts(self) -> list[float]:
        if self.values is None:
            raise ValueError("No reply received from the Marstek meter")
        if not self.stream_online():
            raise ValueError(
                f"No reply from the Marstek meter for over {self._max_age_seconds:.0f} s"
            )
        return list(self.values)
