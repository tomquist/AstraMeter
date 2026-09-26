import logging
import time
from collections.abc import Callable
from typing import Any

from aiohttp import BasicAuth, ClientResponseError

from .http_client import HttpPowermeter
from .sml import (
    _OBIS_POWER_CURRENT,
    _OBIS_POWER_L1,
    _OBIS_POWER_L2,
    _OBIS_POWER_L3,
    parse_sml_powers,
)

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")

# The Pulse Bridge mirrors a push source (the meter emits ~1/s, with jitter):
# polling it occasionally returns an incomplete or CRC-bad telegram that can't
# be decoded. Such misses are transient and self-healing, so reuse the last
# good reading for up to this long rather than erroring on every miss (#518).
# Beyond the window a genuinely broken bridge/meter still surfaces as an error.
_STALE_AFTER_S = 15.0

# The bridge's webserver is slow — responses regularly take >1 s (#551) — so
# the default must leave comfortable headroom. Overridable via TIMEOUT.
DEFAULT_TIMEOUT_S = 5.0

# Bridge firmware ~1794 (September 2026) renamed the telegram endpoint from
# ``/data.json`` to ``/node_data.json`` and 404s the old path (#685); older
# firmware only serves ``/data.json``. No path works on both, so try the new
# one first (bridges update over the air) and fall back to the other on 404.
_DATA_ENDPOINTS = ("node_data.json", "data.json")


class TibberPulse(HttpPowermeter):
    """Reads a Tibber Pulse via the local Pulse Bridge HTTP API.

    Fetches the raw SML telegram from the bridge's ``/node_data.json`` endpoint
    (``/data.json`` on firmware before ~1794) over HTTP Basic auth and decodes
    the instantaneous active power locally — no Tibber cloud involved. The
    bridge's local webserver must be enabled
    (``webserver-force-enable``) and the password is the nine-character code
    printed on the bridge (e.g. ``AD56-54BA``); the user is ``admin``.

    Returns signed power (positive = grid import, negative = feed-in) as either
    three per-phase values or a single aggregate, matching the OBIS registers
    the meter exposes. Flip the sign with ``POWER_MULTIPLIER = -1`` if reversed.
    """

    def __init__(
        self,
        ip: str,
        password: str,
        node_id: str = "1",
        user: str = "admin",
        *,
        obis_power_current: str = _OBIS_POWER_CURRENT,
        obis_power_l1: str = _OBIS_POWER_L1,
        obis_power_l2: str = _OBIS_POWER_L2,
        obis_power_l3: str = _OBIS_POWER_L3,
        timeout: float = DEFAULT_TIMEOUT_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.ip = ip
        self.password = password
        self.node_id = node_id
        self.user = user
        self._obis_current = obis_power_current
        self._obis_l1 = obis_power_l1
        self._obis_l2 = obis_power_l2
        self._obis_l3 = obis_power_l3
        self._clock = clock or time.monotonic
        # Last successfully decoded reading and when it was decoded, so a
        # transient undecodable telegram can reuse it instead of erroring.
        self._last_powers: list[float] | None = None
        self._last_good: float | None = None
        # The endpoint that last answered, so the fallback costs one extra
        # request per switch rather than one per poll.
        self._endpoint = _DATA_ENDPOINTS[0]

    def _session_options(self) -> dict[str, Any]:
        return {
            **super()._session_options(),
            "auth": BasicAuth(self.user, self.password),
        }

    async def _fetch_telegram(self) -> bytes:
        # Pick the fallback from the path this poll tried, not from
        # ``self._endpoint``, which an overlapping poll may already have moved.
        tried = self._endpoint
        try:
            return await self.get_bytes(self._url(tried))
        except ClientResponseError as e:
            if e.status != 404:
                raise
        # The bridge doesn't serve this path, so it runs the other firmware
        # generation: at startup, or after an OTA update while running.
        other = next(ep for ep in _DATA_ENDPOINTS if ep != tried)
        data = await self.get_bytes(self._url(other))
        logger.info("Tibber Pulse: bridge serves /%s, using it from now on", other)
        self._endpoint = other
        return data

    def _url(self, endpoint: str) -> str:
        return f"http://{self.ip}/{endpoint}?node_id={self.node_id}"

    async def get_powermeter_watts(self) -> list[float]:
        data = await self._fetch_telegram()
        powers = parse_sml_powers(
            data,
            self._obis_current,
            self._obis_l1,
            self._obis_l2,
            self._obis_l3,
        )
        if powers:
            result = [float(x) for x in powers]
            self._last_powers = result
            self._last_good = self._clock()
            return result
        # Transient decode miss: reuse the last good reading for a bounded
        # window so an occasional bad telegram doesn't spam warnings or starve
        # the control loop (#518). Past the window, surface the failure.
        if self._last_powers is not None and self._last_good is not None:
            age = self._clock() - self._last_good
            if age <= _STALE_AFTER_S:
                logger.debug(
                    "Tibber Pulse: undecodable telegram, reusing last good "
                    "values %s (age %.1fs)",
                    self._last_powers,
                    age,
                )
                return list(self._last_powers)
        raise ValueError("Could not decode SML telegram from Tibber Pulse")
