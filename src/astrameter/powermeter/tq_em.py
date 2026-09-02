import asyncio
import time

from .http_client import HttpPowermeter, SessionExpired, retry_after_relogin

# The Energy Manager answers a lapsed login with either status.
_LOGIN_EXPIRED = frozenset({401, 403})


class TQEnergyManager(HttpPowermeter):
    """Powermeter using the TQ Energy Manager JSON API."""

    # OBIS (from grid, to grid) pairs; watts = from grid - to grid.
    _TOTAL_KEYS = ("1-0:1.4.0*255", "1-0:2.4.0*255")
    _PHASE_KEYS = (
        ("1-0:21.4.0*255", "1-0:22.4.0*255"),  # L1
        ("1-0:41.4.0*255", "1-0:42.4.0*255"),  # L2
        ("1-0:61.4.0*255", "1-0:62.4.0*255"),  # L3
    )

    _MAX_IDLE = 60 * 30  # log in again after 30 min without a read

    def __init__(self, host: str, password: str = "", *, timeout: float = 5.0) -> None:
        super().__init__(timeout=timeout)
        self._host = host.rstrip("/")
        self._password = password
        self._serial: str | None = None
        self._last_use = 0.0
        self._auth_lock = asyncio.Lock()

    async def get_powermeter_watts(self) -> list[float]:
        async with self._auth_lock:
            await self._ensure_logged_in()
            data = await retry_after_relogin(self._read_live_json, self._login)

        if any(key in data for pair in self._PHASE_KEYS for key in pair):
            return [
                float(data.get(from_grid, 0)) - float(data.get(to_grid, 0))
                for from_grid, to_grid in self._PHASE_KEYS
            ]

        if any(key in data for key in self._TOTAL_KEYS):
            from_grid, to_grid = self._TOTAL_KEYS
            return [float(data.get(from_grid, 0)) - float(data.get(to_grid, 0))]

        raise RuntimeError("Required OBIS values missing in payload")

    async def _ensure_logged_in(self) -> None:
        self._require_session()
        now = time.time()
        if self._serial is None or (now - self._last_use) > self._MAX_IDLE:
            await self._login()
        self._last_use = now

    async def _login(self) -> None:
        start_page = await self.get_json(f"http://{self._host}/start.php")

        self._serial = start_page.get("serial") or start_page.get("ieq_serial")
        if not self._serial:
            raise RuntimeError("Serial number missing in /start.php response")

        if start_page.get("authentication") is True:
            return

        payload = {"login": self._serial, "save_login": 1}
        if self._password:
            payload["password"] = self._password

        async with self._require_session().post(
            f"http://{self._host}/start.php", data=payload
        ) as resp:
            resp.raise_for_status()
            login_result = await resp.json(content_type=None)
            if login_result.get("authentication") is not True:
                raise RuntimeError("Authentication failed")

    async def _read_live_json(self) -> dict:
        data = await self.get_json(
            f"http://{self._host}/mum-webservice/data.php",
            expired_statuses=_LOGIN_EXPIRED,
        )
        if data.get("status", 0) >= 900:
            raise SessionExpired
        return data
