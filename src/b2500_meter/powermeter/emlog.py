import aiohttp

from .base import Powermeter


class Emlog(Powermeter):
    def __init__(self, ip: str, meterindex: str, json_power_calculate: bool):
        self.ip = ip
        self.meterindex = meterindex
        self.json_power_calculate = json_power_calculate
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session:
            return
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def stop(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def get_json(self, path):
        if not self.session:
            raise RuntimeError("Session not started; call start() first")
        url = f"http://{self.ip}{path}"
        async with self.session.get(url) as resp:
            return await resp.json(content_type=None)

    async def get_powermeter_watts_async(self) -> list[float]:
        response = await self.get_json(
            f"/pages/getinformation.php?heute&meterindex={self.meterindex}"
        )
        if not self.json_power_calculate:
            return [int(response["Leistung170"])]
        else:
            power_in = response["Leistung170"]
            power_out = response["Leistung270"]
            return [int(power_in) - int(power_out)]
