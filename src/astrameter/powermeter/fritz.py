import asyncio
import hashlib
import re
import xml.etree.ElementTree as ET

import aiohttp
from aiohttp import ClientTimeout

from .base import Powermeter

# AVM AHA-HTTP-Interface endpoints.
# Docs: https://fritz.com/fileadmin/user_upload/Global/Service/Schnittstellen/AHA-HTTP-Interface.pdf
LOGIN_PATH = "/login_sid.lua"
HOMEAUTO_PATH = "/webservices/homeautoswitch.lua"
INVALID_SID = "0000000000000000"

# Identifiers that already end in a unit suffix like "-1"/"-2".
_AIN_SUFFIX_RE = re.compile(r"-\d+$")


def _normalize_ain(ain: str) -> str:
    """Strip whitespace from an AIN so identifiers compare regardless of spaces.

    AVM shows AINs with a blank (e.g. ``12345 0123456``) in the FRITZ!Box UI and
    in the ``getdevicelistinfos`` XML, but users frequently configure them
    without it. Removing all whitespace makes both forms equivalent.
    """
    return "".join(ain.split())


def compute_login_response(challenge: str, password: str) -> str:
    """Compute the AVM ``login_sid.lua`` challenge response.

    Supports both the PBKDF2 challenge (firmware ≥ 7.24, ``2$iter1$salt1$iter2$salt2``)
    and the legacy MD5 challenge.
    """
    if challenge.startswith("2$"):
        _, iter1, salt1, iter2, salt2 = challenge.split("$")
        hash1 = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt1), int(iter1)
        )
        hash2 = hashlib.pbkdf2_hmac("sha256", hash1, bytes.fromhex(salt2), int(iter2))
        return f"{salt2}${hash2.hex()}"
    # Legacy MD5 challenge-response (UTF-16LE encoded "<challenge>-<password>").
    digest = hashlib.md5(f"{challenge}-{password}".encode("utf-16-le")).hexdigest()
    return f"{challenge}-{digest}"


class _SessionExpired(RuntimeError):
    """Internal marker: the SID is no longer valid, triggers a transparent re-login."""


class FritzSmartEnergy(Powermeter):
    """Powermeter for the AVM FRITZ!Smart Energy 250 smart-meter read head.

    The read head pairs with a FRITZ!Box over DECT; AstraMeter reads its current
    power through the FRITZ!Box's AHA-HTTP-Interface (``getdevicelistinfos``).

    The device exposes two sub-units under the base AIN: ``-1`` (*Strombezug* /
    grid import) and ``-2`` (*Einspeisung* / feed-in). Both report the **signed**
    instantaneous power in milliwatts (positive = import, negative = feed-in), so
    reading the ``-1`` branch alone yields net grid power. If the configured AIN
    has no ``-N`` suffix, ``-1`` is appended automatically.
    """

    def __init__(
        self,
        host: str,
        user: str,
        password: str,
        ain: str,
        *,
        use_tls: bool = False,
        verify_ssl: bool = True,
        timeout: float = 10.0,
    ) -> None:
        host = host.strip().rstrip("/")
        if host.startswith(("http://", "https://")):
            self._base_url = host
        else:
            scheme = "https" if use_tls else "http"
            self._base_url = f"{scheme}://{host or 'fritz.box'}"

        self._user = user
        self._password = password

        ain = _normalize_ain(ain)
        if not ain:
            raise ValueError("FRITZ!Smart Energy requires an AIN")
        if not _AIN_SUFFIX_RE.search(ain):
            # No unit suffix configured: default to the grid-import (-1) branch,
            # which carries signed net power.
            ain += "-1"
        self._ain = ain

        self._timeout = timeout
        # Only force-disable verification when actually using TLS without it;
        # otherwise let aiohttp use its defaults (and ignore for plain http).
        # Derive TLS from the resolved scheme so an explicit ``https://`` HOST
        # also honors VERIFY_SSL.
        effective_tls = self._base_url.startswith("https://")
        self._ssl: bool | None = False if (effective_tls and not verify_ssl) else None
        self._session: aiohttp.ClientSession | None = None
        self._sid: str | None = None
        self._auth_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._session:
            return
        self._sid = None
        self._session = aiohttp.ClientSession(
            timeout=ClientTimeout(total=self._timeout)
        )

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        self._sid = None

    async def get_powermeter_watts(self) -> list[float]:
        if self._session is None:
            raise RuntimeError("Session not started; call start() first")

        async with self._auth_lock:
            if self._sid is None:
                await self._login()
            try:
                xml = await self._fetch_device_list()
            except _SessionExpired:
                await self._login()
                xml = await self._fetch_device_list()

        return [self._extract_power_mw(xml) / 1000.0]

    async def _login(self) -> None:
        """Authenticate against ``login_sid.lua`` and store the resulting SID."""
        info = await self._get_session_info({"version": "2"})
        sid = info.findtext("SID") or INVALID_SID
        if sid != INVALID_SID:
            self._sid = sid
            return

        challenge = info.findtext("Challenge") or ""
        response = compute_login_response(challenge, self._password)
        info = await self._get_session_info(
            {"version": "2", "username": self._user, "response": response}
        )
        sid = info.findtext("SID") or INVALID_SID
        if sid == INVALID_SID:
            block_time = info.findtext("BlockTime") or "0"
            raise RuntimeError(
                "FRITZ!Box login failed (check USER/PASSWORD); "
                f"blocked for {block_time}s"
            )
        self._sid = sid

    async def _get_session_info(self, params: dict[str, str]) -> ET.Element:
        assert self._session is not None
        async with self._session.get(
            self._base_url + LOGIN_PATH, params=params, ssl=self._ssl
        ) as resp:
            resp.raise_for_status()
            text = await resp.text()
        return ET.fromstring(text)

    async def _fetch_device_list(self) -> str:
        assert self._session is not None
        params = {"switchcmd": "getdevicelistinfos", "sid": self._sid or INVALID_SID}
        async with self._session.get(
            self._base_url + HOMEAUTO_PATH, params=params, ssl=self._ssl
        ) as resp:
            if resp.status == 403:
                # FRITZ!Box rejects an expired/invalid SID with 403.
                raise _SessionExpired
            resp.raise_for_status()
            return await resp.text()

    def _extract_power_mw(self, xml: str) -> float:
        root = ET.fromstring(xml)
        for device in root.iter("device"):
            if _normalize_ain(device.get("identifier", "")) == self._ain:
                power = device.findtext("powermeter/power")
                if power is None:
                    raise ValueError(
                        f"FRITZ device '{self._ain}' has no powermeter/power value"
                    )
                return float(power)
        raise ValueError(
            f"FRITZ device with AIN '{self._ain}' not found in the device list"
        )
