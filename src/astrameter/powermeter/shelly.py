from typing import Any

from aiohttp import BasicAuth, DigestAuthMiddleware

from .http_client import HttpPowermeter


class Shelly(HttpPowermeter):
    # Gen2+ RPC endpoints use digest auth where the classic ones use basic, and
    # each subclass only ever talks to one of the two.
    _rpc = False

    def __init__(self, ip: str, user: str, password: str, emeterindex: str):
        self.ip = ip
        self.user = user
        self.password = password
        self.emeterindex = emeterindex

    def _session_options(self) -> dict[str, Any]:
        options = super()._session_options()
        if self._rpc:
            options["middlewares"] = [DigestAuthMiddleware(self.user, self.password)]
        elif self.user:
            options["auth"] = BasicAuth(self.user, self.password)
        return options


class Shelly1PM(Shelly):
    async def get_powermeter_watts(self) -> list[float]:
        if self.emeterindex:
            meter = await self.get_json(f"http://{self.ip}/meter/{self.emeterindex}")
            return [int(meter["power"])]
        else:
            status = await self.get_json(f"http://{self.ip}/status")
            return [int(meter["power"]) for meter in status["meters"]]


class ShellyPlus1PM(Shelly):
    _rpc = True

    async def get_powermeter_watts(self) -> list[float]:
        response = await self.get_json(f"http://{self.ip}/rpc/Switch.GetStatus?id=0")
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
    _rpc = True

    async def get_powermeter_watts(self) -> list[float]:
        response = await self.get_json(f"http://{self.ip}/rpc/EM.GetStatus?id=0")
        return [int(response["total_act_power"])]
