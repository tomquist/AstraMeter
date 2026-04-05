import aiohttp

from .base import Powermeter


class Shrdzm(Powermeter):
    def __init__(self, ip: str, user: str, password: str):
        self.ip = ip
        self.user = user
        self.password = password
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

    async def get_powermeter_watts(self) -> list[float]:
        response = await self.get_json(
            f"/getLastData?user={self.user}&password={self.password}"
        )
        return [int(response["1.7.0"]) - int(response["2.7.0"])]
