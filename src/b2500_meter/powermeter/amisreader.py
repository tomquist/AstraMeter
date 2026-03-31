import aiohttp

from .base import Powermeter


class AmisReader(Powermeter):
    def __init__(self, ip: str):
        self.ip = ip
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
        response = await self.get_json("/rest")
        return [int(response["saldo"])]
