import aiohttp

from .base import Powermeter


class IoBroker(Powermeter):
    def __init__(
        self,
        ip: str,
        port: str,
        current_power_alias: str,
        power_calculate: bool,
        power_input_alias: str,
        power_output_alias: str,
    ):
        self.ip = ip
        self.port = port
        self.current_power_alias = current_power_alias
        self.power_calculate = power_calculate
        self.power_input_alias = power_input_alias
        self.power_output_alias = power_output_alias
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
        url = f"http://{self.ip}:{self.port}{path}"
        async with self.session.get(url) as resp:
            return await resp.json(content_type=None)

    async def get_powermeter_watts(self) -> list[float]:
        if not self.power_calculate:
            response = await self.get_json(f"/getBulk/{self.current_power_alias}")
            for item in response:
                if item["id"] == self.current_power_alias:
                    return [int(item["val"])]
            raise ValueError(
                f"Alias {self.current_power_alias!r} not found in response"
            )
        else:
            response = await self.get_json(
                f"/getBulk/{self.power_input_alias},{self.power_output_alias}"
            )
            power_in = 0
            power_out = 0
            for item in response:
                if item["id"] == self.power_input_alias:
                    power_in = int(item["val"])
                if item["id"] == self.power_output_alias:
                    power_out = int(item["val"])
            return [power_in - power_out]
