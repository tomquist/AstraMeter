from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

import aiohttp
from aiohttp import ClientResponseError, ClientTimeout, TCPConnector

from .base import Powermeter

logger = logging.getLogger("astrameter")

ENLIGHTEN_LOGIN_URL = "https://enlighten.enphaseenergy.com/login/login.json"
ENTREZ_TOKEN_URL = "https://entrez.enphaseenergy.com/tokens"
DEFAULT_TIMEOUT_SECONDS = 10.0


def _build_ssl_context(verify_ssl: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify_ssl:
        # Order matters: verify_mode=CERT_NONE requires check_hostname=False first.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _obtain_token(
    cloud_session: aiohttp.ClientSession,
    username: str,
    password: str,
    serial: str,
) -> str:
    async with cloud_session.post(
        ENLIGHTEN_LOGIN_URL,
        data={"user[email]": username, "user[password]": password},
    ) as resp:
        resp.raise_for_status()
        login_payload = await resp.json(content_type=None)
    session_id = (
        login_payload.get("session_id") if isinstance(login_payload, dict) else None
    )
    if not session_id:
        message = (
            login_payload.get("message", "unknown")
            if isinstance(login_payload, dict)
            else "unknown"
        )
        raise ValueError(
            f"Envoy: Enlighten login response missing session_id (message: {message})"
        )

    async with cloud_session.post(
        ENTREZ_TOKEN_URL,
        json={
            "session_id": session_id,
            "serial_num": serial,
            "username": username,
        },
    ) as resp:
        resp.raise_for_status()
        token = (await resp.text()).strip()

    if not token.startswith("eyJ") or token.count(".") != 2:
        raise ValueError(
            f"Envoy: entrez token endpoint did not return a JWT (body: {token[:200]!r})"
        )

    logger.info("Envoy: obtained new JWT token from Enlighten cloud")
    return token


class Envoy(Powermeter):
    def __init__(
        self,
        host: str,
        token: str = "",
        username: str = "",
        password: str = "",
        serial: str = "",
        verify_ssl: bool = False,
    ) -> None:
        if not host:
            raise ValueError("Envoy: HOST is required")
        has_credentials = bool(username and password and serial)
        if not token and not has_credentials:
            raise ValueError("Envoy: provide either TOKEN or USERNAME/PASSWORD/SERIAL")

        self.host = host
        self._username = username
        self._password = password
        self._serial = serial
        self._has_credentials = has_credentials
        self._verify_ssl = verify_ssl
        self._ssl_context = _build_ssl_context(verify_ssl)
        self._token = token
        self._token_lock = asyncio.Lock()
        self._session: aiohttp.ClientSession | None = None
        self._cloud_session: aiohttp.ClientSession | None = None

        if not verify_ssl:
            logger.warning(
                "Envoy: TLS certificate verification is disabled for the local "
                "Envoy (VERIFY_SSL=False); use only on a trusted LAN. Enphase "
                "Enlighten cloud requests are unaffected and always use system TLS."
            )

    async def start(self) -> None:
        if self._session is not None:
            return
        timeout = ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS)
        self._session = aiohttp.ClientSession(
            connector=TCPConnector(ssl=self._ssl_context),
            timeout=timeout,
        )
        # Separate session for the Enphase cloud: always uses default system TLS,
        # never weakened by VERIFY_SSL=False on the local Envoy.
        self._cloud_session = aiohttp.ClientSession(timeout=timeout)

    async def stop(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._cloud_session is not None:
            await self._cloud_session.close()
            self._cloud_session = None

    async def _ensure_token(self) -> None:
        if self._token:
            return
        async with self._token_lock:
            if self._token:
                return
            if self._cloud_session is None:
                raise RuntimeError("Cloud session not started; call start() first")
            self._token = await _obtain_token(
                self._cloud_session, self._username, self._password, self._serial
            )

    async def _refresh_token(self) -> None:
        async with self._token_lock:
            if self._cloud_session is None:
                raise RuntimeError("Cloud session not started; call start() first")
            self._token = await _obtain_token(
                self._cloud_session, self._username, self._password, self._serial
            )

    async def _get_production(self) -> dict[str, Any]:
        if self._session is None:
            raise RuntimeError("Session not started; call start() first")
        url = f"https://{self.host}/production.json?details=1"
        headers = {"Authorization": f"Bearer {self._token}"}
        async with self._session.get(url, headers=headers) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
        return data if isinstance(data, dict) else {}

    async def _fetch_production(self) -> dict[str, Any]:
        await self._ensure_token()
        old_token = self._token
        try:
            return await self._get_production()
        except ClientResponseError as e:
            if e.status != 401 or not self._has_credentials:
                raise
            # If another coroutine already refreshed while we were awaiting,
            # skip our own refresh and retry with the fresh token.
            if self._token == old_token:
                logger.info("Envoy: token rejected (401), refreshing")
                await self._refresh_token()
            return await self._get_production()

    async def get_powermeter_watts(self) -> list[float]:
        data = await self._fetch_production()
        consumption = data.get("consumption")
        if not isinstance(consumption, list):
            raise ValueError(
                "Envoy: production.json missing 'consumption' array; "
                "consumption CTs are required"
            )

        entry = next(
            (
                c
                for c in consumption
                if isinstance(c, dict) and c.get("measurementType") == "net-consumption"
            ),
            None,
        )
        if entry is None:
            raise ValueError(
                "Envoy: response does not expose 'net-consumption'; "
                "consumption CTs are required"
            )

        lines = entry.get("lines")
        if isinstance(lines, list) and lines:
            values: list[float] = []
            for i, line in enumerate(lines[:3]):
                if not isinstance(line, dict) or "wNow" not in line:
                    raise ValueError(
                        f"Envoy: malformed net-consumption line entry at index {i}"
                    )
                try:
                    values.append(float(line["wNow"]))
                except (TypeError, ValueError) as err:
                    raise ValueError(
                        f"Envoy: non-numeric 'wNow' in net-consumption line at index {i}"
                    ) from err
            return values

        if "wNow" not in entry:
            raise ValueError("Envoy: net-consumption entry missing 'wNow'")
        try:
            return [float(entry["wNow"])]
        except (TypeError, ValueError) as err:
            raise ValueError("Envoy: non-numeric 'wNow' in net-consumption") from err
