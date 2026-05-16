import aiohttp
from aiohttp import BasicAuth, ClientTimeout, DigestAuthMiddleware

from .base import Powermeter


class Shelly(Powermeter):
    def __init__(self, ip: str, user: str, password: str, emeterindex: str):
        self.ip = ip
        self.user = user
        self.password = password
        self.emeterindex = emeterindex
        self._session: aiohttp.ClientSession | None = None
        self._rpc_session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        timeout = ClientTimeout(total=10)
        auth = BasicAuth(self.user, self.password) if self.user else None
        self._session = aiohttp.ClientSession(auth=auth, timeout=timeout)
        self._rpc_session = aiohttp.ClientSession(
            timeout=timeout,
            middlewares=[DigestAuthMiddleware(self.user, self.password)],
        )

    async def stop(self) -> None:
        if self._session:
            await self._session.close()
            self._session = None
        if self._rpc_session:
            await self._rpc_session.close()
            self._rpc_session = None

    async def _get_json(self, path: str) -> dict:
        assert self._session is not None
        url = f"http://{self.ip}{path}"
        async with self._session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def _get_rpc_json(self, path: str) -> dict:
        assert self._rpc_session is not None
        url = f"http://{self.ip}/rpc{path}"
        async with self._rpc_session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def get_powermeter_watts(self) -> list[float]:
        raise NotImplementedError()


class Shelly1PM(Shelly):
    async def get_powermeter_watts(self) -> list[float]:
        if self.emeterindex:
            meter = await self._get_json(f"/meter/{self.emeterindex}")
            return [int(meter["power"])]
        else:
            status = await self._get_json("/status")
            return [int(meter["power"]) for meter in status["meters"]]


class ShellyPlus1PM(Shelly):
    async def get_powermeter_watts(self) -> list[float]:
        response = await self._get_rpc_json("/Switch.GetStatus?id=0")
        return [int(response["apower"])]


class ShellyEM(Shelly):
    async def get_powermeter_watts(self) -> list[float]:
        if self.emeterindex:
            emeter = await self._get_json(f"/emeter/{self.emeterindex}")
            return [int(emeter["power"])]
        else:
            status = await self._get_json("/status")
            return [int(emeter["power"]) for emeter in status["emeters"]]


class Shelly3EM(Shelly):
    async def get_powermeter_watts(self) -> list[float]:
        status = await self._get_json("/status")
        return [int(emeter["power"]) for emeter in status["emeters"]]


class Shelly3EMPro(Shelly):
    async def get_powermeter_watts(self) -> list[float]:
        response = await self._get_rpc_json("/EM.GetStatus?id=0")
        return [int(response["total_act_power"])]
