import aiohttp
from aiohttp import BasicAuth, ClientTimeout

from .base import Powermeter
from .sml import (
    _OBIS_POWER_CURRENT,
    _OBIS_POWER_L1,
    _OBIS_POWER_L2,
    _OBIS_POWER_L3,
    parse_sml_powers,
)


class TibberPulse(Powermeter):
    """Reads a Tibber Pulse via the local Pulse Bridge HTTP API.

    Fetches the raw SML telegram from the bridge's ``/data.json`` endpoint
    (HTTP Basic auth) and decodes the instantaneous active power locally — no
    Tibber cloud involved. The bridge's local webserver must be enabled
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
    ):
        self.ip = ip
        self.password = password
        self.node_id = node_id
        self.user = user
        self._obis_current = obis_power_current
        self._obis_l1 = obis_power_l1
        self._obis_l2 = obis_power_l2
        self._obis_l3 = obis_power_l3
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session:
            return
        # Fail fast: the battery polls ~1/s, so a slow source should error
        # quickly and let the next poll retry rather than pin a handler.
        self.session = aiohttp.ClientSession(
            auth=BasicAuth(self.user, self.password),
            timeout=ClientTimeout(total=2, connect=1),
        )

    async def stop(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def get_powermeter_watts(self) -> list[float]:
        if not self.session:
            raise RuntimeError("Session not started; call start() first")
        url = f"http://{self.ip}/data.json?node_id={self.node_id}"
        async with self.session.get(url) as resp:
            resp.raise_for_status()
            data = await resp.read()
        powers = parse_sml_powers(
            data,
            self._obis_current,
            self._obis_l1,
            self._obis_l2,
            self._obis_l3,
        )
        if not powers:
            raise ValueError("Could not decode SML telegram from Tibber Pulse")
        return [float(x) for x in powers]
