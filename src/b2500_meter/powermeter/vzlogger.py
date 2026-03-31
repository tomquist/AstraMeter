import aiohttp

from .base import Powermeter


class VZLogger(Powermeter):
    def __init__(self, ip: str, port: str, uuid: str):
        self.ip = ip
        self.port = port
        self.uuid = uuid
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session:
            return
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def stop(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def get_json(self):
        if not self.session:
            raise RuntimeError("Session not started; call start() first")
        url = f"http://{self.ip}:{self.port}/{self.uuid}"
        async with self.session.get(url) as resp:
            return await resp.json(content_type=None)

    async def get_powermeter_watts_async(self) -> list[float]:
        return [int((await self.get_json())["data"][0]["tuples"][0][1])]
