import asyncio
import configparser
import datetime
import re
from dataclasses import dataclass, field

import serial_asyncio_fast
import smllib.errors
from smllib import SmlFrame, SmlStreamReader
from smllib.const import UNITS

from b2500_meter.config.logger import logger

from .base import Powermeter

# Default OBIS hex (smllib const / German eHZ-style meters)
# Aggregate instantaneous active power (1-0:16.7.0)
_OBIS_POWER_CURRENT = "0100100700ff"
# Per-phase sum active power L1/L2/L3 (Summenwirkleistung)
_OBIS_POWER_L1 = "0100240700ff"
_OBIS_POWER_L2 = "0100380700ff"
_OBIS_POWER_L3 = "01004c0700ff"

_OBIS_HEX_RE = re.compile(r"^[0-9a-f]{12}$")


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

    powers: list[int] = field(default_factory=lambda: [0])
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


def _optional_w(by_obis: dict, obis_key: str, label: str) -> int | None:
    ov = by_obis.get(obis_key)
    if ov is None:
        return None
    _expect_unit(ov, "W", label)
    return ov.value


def _expect_unit(ov, expected: str, label: str) -> None:
    actual = UNITS.get(ov.unit)
    if actual != expected:
        raise ValueError(
            f"Unexpected unit for {label}: expected {expected!r}, "
            f"got {actual!r} (unit code={ov.unit!r}, value={ov.value!r})"
        )


class Sml(Powermeter):
    def __init__(
        self,
        serial_device: str,
        *,
        obis_power_current: str = _OBIS_POWER_CURRENT,
        obis_power_l1: str = _OBIS_POWER_L1,
        obis_power_l2: str = _OBIS_POWER_L2,
        obis_power_l3: str = _OBIS_POWER_L3,
    ):
        if not serial_device.strip():
            raise ValueError("serial_device must be non-empty (config: SERIAL)")
        self._serial_device = serial_device.strip()
        self._obis_current = obis_power_current
        self._obis_l1 = obis_power_l1
        self._obis_l2 = obis_power_l2
        self._obis_l3 = obis_power_l3
        self._current = EnergyStats()
        self._lock = asyncio.Lock()
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    @property
    def current(self) -> EnergyStats:
        return self._current

    async def start(self) -> None:
        if self._reader is not None:
            return
        self._reader, self._writer = await serial_asyncio_fast.open_serial_connection(
            url=self._serial_device, baudrate=9600
        )

    async def stop(self) -> None:
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
            self._reader = None
            self._writer = None

    async def get_powermeter_watts(self) -> list[float]:
        if self._lock.locked():
            return [float(x) for x in self._current.powers]
        async with self._lock:
            await self._read_serial()
            return [float(x) for x in self._current.powers]

    async def _read_serial(self) -> None:
        if self._reader is None:
            raise RuntimeError("Sml not started; call start() first")
        stream = SmlStreamReader()
        try:
            data = await asyncio.wait_for(self._reader.read(512), timeout=10)
        except asyncio.TimeoutError:
            logger.error("serial read timed out")
            return
        stream.add(data)
        for i in range(10):
            sml_frame = await self._try_read_frame(stream)
            if sml_frame is not None:
                self._current = EnergyStats.from_sml_frame(
                    sml_frame,
                    self._obis_current,
                    self._obis_l1,
                    self._obis_l2,
                    self._obis_l3,
                )
                logger.debug("got sml frame: %s after %s attempts", self._current, i)
                return
        logger.error("failed to read SML frame after 10 attempts")

    async def _try_read_frame(self, stream: SmlStreamReader) -> SmlFrame | None:
        try:
            sml_frame = stream.get_frame()
        except smllib.errors.CrcError as e:
            logger.debug("CRC error, keep reading: %s", e)
            sml_frame = None
        except smllib.errors.SmlLibException as e:
            logger.error("error reading frame: %s", e)
            sml_frame = None
        if sml_frame is None:
            assert self._reader is not None
            try:
                data = await asyncio.wait_for(self._reader.read(512), timeout=10)
            except asyncio.TimeoutError:
                logger.error("serial read timed out")
                return None
            if not data:
                logger.error("serial connection closed")
                return None
            # May buffer partial SML; frame may parse on a later loop iteration.
            stream.add(data)
        return sml_frame


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
