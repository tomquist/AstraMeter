import aiohttp

from .base import Powermeter


class ESPHome(Powermeter):
    def __init__(self, ip: str, port: str, domain: str, id: str):
        self.ip = ip
        self.port = port
        self.domain = domain
        self.id = id
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
        parsed_data = await self.get_json(f"/{self.domain}/{self.id}")
        return [int(parsed_data["value"])]
