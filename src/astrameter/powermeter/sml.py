import asyncio
import configparser
import contextlib
import datetime
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import serial_asyncio_fast
import smllib.errors
from smllib import SmlFrame, SmlStreamReader
from smllib.const import UNITS

from .base import PushPowermeter, stream_fresh

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")

# Default OBIS hex (smllib const / German eHZ-style meters)
# Aggregate instantaneous active power (1-0:16.7.0)
_OBIS_POWER_CURRENT = "0100100700ff"
# Per-phase sum active power L1/L2/L3 (Summenwirkleistung)
_OBIS_POWER_L1 = "0100240700ff"
_OBIS_POWER_L2 = "0100380700ff"
_OBIS_POWER_L3 = "01004c0700ff"

_OBIS_HEX_RE = re.compile(r"^[0-9a-f]{12}$")

# A reading older than this is stale.  Meters send every 1-4 s, so this is
# several missed telegrams in a row.
_MAX_READING_AGE = 10.0
# Silence on an open port for this long means the connection is dead (e.g. a
# half-open TCP link to ser2net), so the reader reopens it.
_READ_TIMEOUT = 30.0
# How long to wait before reopening the port after it fails.
RECONNECT_DELAY_SECONDS = 5.0


def _normalize_obis_hex(raw: str, label: str) -> str:
    v = raw.strip().lower()
    if not _OBIS_HEX_RE.match(v):
        raise ValueError(
            f"{label} must be exactly 12 hexadecimal digits (SML OBIS form), got {raw!r}"
        )
    return v


@dataclass
class EnergyStats:
    """Instantaneous power: one value (aggregate W) or three (per-phase W)."""

    powers: list[float] = field(default_factory=lambda: [0])
    when: datetime.datetime = field(default_factory=datetime.datetime.now)

    @classmethod
    def from_sml_frame(
        cls,
        sml_frame: SmlFrame,
        obis_current: str,
        obis_l1: str,
        obis_l2: str,
        obis_l3: str,
    ) -> "EnergyStats":
        by_obis = {ov.obis: ov for ov in sml_frame.get_obis()}
        p1 = _optional_w(by_obis, obis_l1, "phase L1 power")
        p2 = _optional_w(by_obis, obis_l2, "phase L2 power")
        p3 = _optional_w(by_obis, obis_l3, "phase L3 power")
        if p1 is not None and p2 is not None and p3 is not None:
            return cls(powers=[p1, p2, p3])
        agg = _optional_w(by_obis, obis_current, "aggregate power")
        if agg is not None:
            return cls(powers=[agg])
        return cls()


def _optional_w(by_obis: dict, obis_key: str, label: str) -> float | None:
    ov = by_obis.get(obis_key)
    if ov is None:
        return None
    _expect_unit(ov, "W", label)
    return _apply_scaler(ov)


def _apply_scaler(ov: Any) -> float:
    """Scale an SML value by its ``10**scaler`` exponent.

    smllib exposes the meter's raw integer in ``ov.value`` and the decimal
    exponent in ``ov.scaler``; the true reading is ``value * 10**scaler``.
    Classic eHZ meters report power already scaled (``scaler`` 0 or absent),
    but some meters (e.g. Apator Picus eHZ) send watts with ``scaler = -3``,
    so the raw integer is 1000x too large. Applying the scaler avoids the
    inflated readings reported in #519 (no manual ``POWER_MULTIPLIER`` needed).
    """
    value = ov.value
    scaler = getattr(ov, "scaler", None)
    if not scaler:
        return value
    return round(value * 10**scaler, abs(scaler) + 3)


def _expect_unit(ov: Any, expected: str, label: str) -> None:
    actual = UNITS.get(ov.unit)
    if actual != expected:
        raise ValueError(
            f"Unexpected unit for {label}: expected {expected!r}, "
            f"got {actual!r} (unit code={ov.unit!r}, value={ov.value!r})"
        )


class Sml(PushPowermeter):
    """Push source fed by an SML meter's IR head.

    The meter sends a telegram every 1-4 s whether or not anyone asks, so a
    background task reads the port continuously and publishes each frame as it
    completes.  A reading is therefore never older than the last telegram, and
    no backlog of old telegrams builds up in the serial or TCP buffers between
    battery polls.
    """

    def __init__(
        self,
        serial_device: str,
        *,
        obis_power_current: str = _OBIS_POWER_CURRENT,
        obis_power_l1: str = _OBIS_POWER_L1,
        obis_power_l2: str = _OBIS_POWER_L2,
        obis_power_l3: str = _OBIS_POWER_L3,
    ) -> None:
        super().__init__()
        if not serial_device.strip():
            raise ValueError("serial_device must be non-empty (config: SERIAL)")
        self._serial_device = serial_device.strip()
        self._obis_current = obis_power_current
        self._obis_l1 = obis_power_l1
        self._obis_l2 = obis_power_l2
        self._obis_l3 = obis_power_l3
        self._clock: Callable[[], float] = time.monotonic
        self._current: EnergyStats | None = None
        self._last_frame_time: float | None = None
        self._stream = SmlStreamReader()
        self._connected = False
        self._reader_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._reader_task is not None:
            return
        self._reader_task = asyncio.create_task(self._read_loop())

    async def stop(self) -> None:
        # Cleared before cancelling: cancellation leaves the loop before its
        # own reset runs, so stream_online() would otherwise stay True.
        self._connected = False
        task, self._reader_task = self._reader_task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def stream_online(self) -> bool | None:
        return self._connected and stream_fresh(
            self._last_frame_time, _MAX_READING_AGE, self._clock
        )

    async def get_powermeter_watts(self) -> list[float]:
        if self._current is None or self._last_frame_time is None:
            raise ValueError("No value received from SML meter")
        if not stream_fresh(self._last_frame_time, _MAX_READING_AGE, self._clock):
            age = self._clock() - self._last_frame_time
            raise ValueError(f"SML reading is stale ({age:.0f} s old)")
        return [float(x) for x in self._current.powers]

    async def _read_loop(self) -> None:
        """Read the port for the life of the source, reopening it after errors."""
        while True:
            writer: asyncio.StreamWriter | None = None
            try:
                reader, writer = await serial_asyncio_fast.open_serial_connection(
                    url=self._serial_device, baudrate=9600
                )
                self._connected = True
                logger.info("SML connected to %s", self._serial_device)
                await self._read_stream(reader)
            except Exception as exc:
                logger.error("SML read from %s failed: %s", self._serial_device, exc)
            finally:
                self._connected = False
                # A partial frame from the old connection can't be completed
                # by the new one.
                self._stream.clear()
                if writer is not None:
                    writer.close()
                    with contextlib.suppress(Exception):
                        await writer.wait_closed()
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _read_stream(self, reader: asyncio.StreamReader) -> None:
        """Publish every frame from *reader* until it closes or goes silent."""
        while True:
            try:
                data = await asyncio.wait_for(reader.read(512), timeout=_READ_TIMEOUT)
            except asyncio.TimeoutError:
                logger.error(
                    "SML: no data from %s for %.0f s, reconnecting",
                    self._serial_device,
                    _READ_TIMEOUT,
                )
                return
            if not data:
                logger.error("SML: serial connection to %s closed", self._serial_device)
                return
            # At 9600 baud a read returns only the few bytes that have
            # arrived, so a frame is assembled across many reads.
            self._stream.add(data)
            while (frame := self._next_buffered_frame()) is not None:
                self._publish(frame)

    def _publish(self, frame: SmlFrame) -> None:
        try:
            stats = EnergyStats.from_sml_frame(
                frame, self._obis_current, self._obis_l1, self._obis_l2, self._obis_l3
            )
        except (ValueError, smllib.errors.SmlLibException) as e:
            logger.error("error decoding SML frame: %s", e)
            return
        self._current = stats
        self._last_frame_time = self._clock()
        logger.debug("got sml frame: %s", stats)
        self._message_event.set()

    def _next_buffered_frame(self) -> SmlFrame | None:
        """Return the next complete frame already buffered, skipping bad ones."""
        while True:
            try:
                return self._stream.get_frame()
            except smllib.errors.CrcError as e:
                # get_frame drops the corrupt frame, so the next may be intact.
                logger.debug("CRC error, keep reading: %s", e)
            except smllib.errors.SmlLibException as e:
                logger.error("error reading frame: %s", e)
                self._stream.clear()
                return None


def parse_sml_powers(
    data: bytes,
    obis_current: str = _OBIS_POWER_CURRENT,
    obis_l1: str = _OBIS_POWER_L1,
    obis_l2: str = _OBIS_POWER_L2,
    obis_l3: str = _OBIS_POWER_L3,
) -> list[float] | None:
    """Decode a complete SML telegram (bytes) into instantaneous watts.

    Returns the per-phase (``[L1, L2, L3]``) or aggregate (``[W]``) power list,
    or ``None`` if no complete, valid SML frame could be parsed from *data*.
    Used by sources that fetch a whole telegram at once (e.g. the Tibber Pulse
    Bridge HTTP API) rather than streaming from a serial port.
    """
    stream = SmlStreamReader()
    stream.add(data)
    try:
        sml_frame = stream.get_frame()
    except smllib.errors.SmlLibException as e:
        logger.debug("failed to parse SML telegram: %s", e)
        return None
    if sml_frame is None:
        return None
    return EnergyStats.from_sml_frame(
        sml_frame, obis_current, obis_l1, obis_l2, obis_l3
    ).powers


def parse_sml_obis_config(
    section: str,
    config: configparser.ConfigParser,
) -> tuple[str, str, str, str]:
    """Resolve OBIS hex overrides for [SML]; defaults match smllib eHZ registers."""

    def one(key: str, default: str) -> str:
        raw = config.get(section, key, fallback="").strip()
        if not raw:
            return default
        return _normalize_obis_hex(raw, f"[{section}] {key}")

    return (
        one("OBIS_POWER_CURRENT", _OBIS_POWER_CURRENT),
        one("OBIS_POWER_L1", _OBIS_POWER_L1),
        one("OBIS_POWER_L2", _OBIS_POWER_L2),
        one("OBIS_POWER_L3", _OBIS_POWER_L3),
    )
