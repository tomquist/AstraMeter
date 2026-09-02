from typing import Any

import aiohttp
from aiohttp import BasicAuth, DigestAuthMiddleware

from .http_client import POLL_TIMEOUT, HttpPowermeter


class Shelly(HttpPowermeter):
    def __init__(self, ip: str, user: str, password: str, emeterindex: str):
        self.ip = ip
        self.user = user
        self.password = password
        self.emeterindex = emeterindex
        # Gen2+ RPC endpoints use digest auth where the classic ones use basic.
        self._rpc_session: aiohttp.ClientSession | None = None

    def _session_options(self) -> dict[str, Any]:
        options = super()._session_options()
        if self.user:
            options["auth"] = BasicAuth(self.user, self.password)
        return options

    async def start(self) -> None:
        await super().start()
        if self._rpc_session is None:
            self._rpc_session = aiohttp.ClientSession(
                timeout=POLL_TIMEOUT,
                middlewares=[DigestAuthMiddleware(self.user, self.password)],
            )

    async def stop(self) -> None:
        await super().stop()
        if self._rpc_session:
            await self._rpc_session.close()
            self._rpc_session = None

    async def _get_rpc_json(self, method: str) -> Any:
        if not self._rpc_session:
            raise RuntimeError("Session not started; call start() first")
        async with self._rpc_session.get(f"http://{self.ip}/rpc/{method}") as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)


class Shelly1PM(Shelly):
    async def get_powermeter_watts(self) -> list[float]:
        if self.emeterindex:
            meter = await self.get_json(f"http://{self.ip}/meter/{self.emeterindex}")
            return [int(meter["power"])]
        else:
            status = await self.get_json(f"http://{self.ip}/status")
            return [int(meter["power"]) for meter in status["meters"]]


class ShellyPlus1PM(Shelly):
    async def get_powermeter_watts(self) -> list[float]:
        response = await self._get_rpc_json("Switch.GetStatus?id=0")
        return [int(response["apower"])]


class ShellyEM(Shelly):
    async def get_powermeter_watts(self) -> list[float]:
        if self.emeterindex:
            emeter = await self.get_json(f"http://{self.ip}/emeter/{self.emeterindex}")
            return [int(emeter["power"])]
        else:
            status = await self.get_json(f"http://{self.ip}/status")
            return [int(emeter["power"]) for emeter in status["emeters"]]


class Shelly3EMPro(Shelly):
    async def get_powermeter_watts(self) -> list[float]:
        response = await self._get_rpc_json("EM.GetStatus?id=0")
        return [int(response["total_act_power"])]
