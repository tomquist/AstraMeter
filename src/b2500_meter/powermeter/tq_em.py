import asyncio
import time

import aiohttp
from aiohttp import ClientTimeout

from .base import Powermeter


class TQEnergyManager(Powermeter):
    """Powermeter using the TQ Energy Manager JSON API."""

    # OBIS codes
    _TOTAL_TO_GRID = 0
    _TOTAL_FROM_GRID = 1
    _TOTAL_KEYS = (
        "1-0:1.4.0*255",  # Σ active power (from grid)
        "1-0:2.4.0*255",  # Σ active power (to grid)
    )

    _TOTAL_TO_GRID_L1 = 0
    _TOTAL_FROM_GRID_L1 = 1
    _TOTAL_TO_GRID_L2 = 2
    _TOTAL_FROM_GRID_L2 = 3
    _TOTAL_TO_GRID_L3 = 4
    _TOTAL_FROM_GRID_L3 = 5
    _PHASE_KEYS = (
        "1-0:21.4.0*255",  # L1 active power (from grid)
        "1-0:22.4.0*255",  # L1 active power (to grid)
        "1-0:41.4.0*255",  # L2 active power (from grid)
        "1-0:42.4.0*255",  # L2 active power (to grid)
        "1-0:61.4.0*255",  # L3 active power (from grid)
        "1-0:62.4.0*255",  # L3 active power (to grid)
    )

    _MAX_IDLE = 60 * 30  # 30 min

    def __init__(self, host: str, password: str = "", *, timeout: float = 5.0) -> None:
        self._host, self._pw, self._timeout = host.rstrip("/"), password, timeout
        self._sess: aiohttp.ClientSession | None = None
        self._serial: str | None = None
        self._last_use = 0.0
        self._auth_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._sess:
            return
        self._sess = aiohttp.ClientSession(
            timeout=ClientTimeout(total=self._timeout),
        )

    async def stop(self) -> None:
        if self._sess:
            await self._sess.close()
            self._sess = None

    # ------------------------------------------------------------------ #
    # PUBLIC                                                             #
    # ------------------------------------------------------------------ #
    async def get_powermeter_watts_async(self) -> list[float]:
        if not self._sess:
            raise RuntimeError("Session not started; call start() first")
        async with self._auth_lock:
            await self._ensure_session()

            try:
                data = await self._read_live_json()
            except _SessionExpired:
                await self._login()
                data = await self._read_live_json()

        if any(k in data for k in self._PHASE_KEYS):
            return [
                float(data.get(self._PHASE_KEYS[self._TOTAL_TO_GRID_L1], 0))
                - float(data.get(self._PHASE_KEYS[self._TOTAL_FROM_GRID_L1], 0)),
                float(data.get(self._PHASE_KEYS[self._TOTAL_TO_GRID_L2], 0))
                - float(data.get(self._PHASE_KEYS[self._TOTAL_FROM_GRID_L2], 0)),
                float(data.get(self._PHASE_KEYS[self._TOTAL_TO_GRID_L3], 0))
                - float(data.get(self._PHASE_KEYS[self._TOTAL_FROM_GRID_L3], 0)),
            ]

        if any(k in data for k in self._TOTAL_KEYS):
            return [
                float(data.get(self._TOTAL_KEYS[self._TOTAL_TO_GRID], 0))
                - float(data.get(self._TOTAL_KEYS[self._TOTAL_FROM_GRID], 0))
            ]

        raise RuntimeError("Required OBIS values missing in payload")

    # ------------------------------------------------------------------ #
    # INTERNALS                                                          #
    # ------------------------------------------------------------------ #
    async def _ensure_session(self) -> None:
        if self._sess is None:
            raise RuntimeError("Session not started; call start() first")
        now = time.time()
        if self._serial is None or (now - self._last_use) > self._MAX_IDLE:
            await self._login()
        self._last_use = now

    async def _login(self) -> None:
        """Authenticate lazily with the device."""
        if self._sess is None:
            raise RuntimeError("Session not started; call start() first")
        async with self._sess.get(f"http://{self._host}/start.php") as r1:
            r1.raise_for_status()
            j1 = await r1.json(content_type=None)

        self._serial = j1.get("serial") or j1.get("ieq_serial")
        if not self._serial:
            raise RuntimeError("Serial number missing in /start.php response")

        if j1.get("authentication") is True:
            return

        payload = {"login": self._serial, "save_login": 1}
        if self._pw:
            payload["password"] = self._pw

        async with self._sess.post(
            f"http://{self._host}/start.php", data=payload
        ) as r2:
            r2.raise_for_status()
            j2 = await r2.json(content_type=None)
            if j2.get("authentication") is not True:
                raise RuntimeError("Authentication failed")

    async def _read_live_json(self) -> dict:
        if self._sess is None:
            raise RuntimeError("Session not started; call start() first")
        async with self._sess.get(f"http://{self._host}/mum-webservice/data.php") as r:
            if r.status in (401, 403):
                raise _SessionExpired

            r.raise_for_status()
            data = await r.json(content_type=None)
            if data.get("status", 0) >= 900:
                raise _SessionExpired
            return data


class _SessionExpired(RuntimeError):
    """Internal marker - triggers transparent re-login."""

    pass
