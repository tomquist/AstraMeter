import asyncio

import aiohttp

from .base import Powermeter


class VZLogger(Powermeter):
    def __init__(self, ip: str, port: str, uuid: str | list[str]):
        self.ip = ip
        self.port = port
        self.uuids = [uuid] if isinstance(uuid, str) else list(uuid)
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session:
            return
        # Fail fast: the battery polls ~1/s, so a slow source should error
        # quickly and let the next poll retry rather than pin a handler.
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=2, connect=1)
        )

    async def stop(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def get_json(self, uuid: str):
        if not self.session:
            raise RuntimeError("Session not started; call start() first")
        url = f"http://{self.ip}:{self.port}/{uuid}"
        async with self.session.get(url) as resp:
            return await resp.json(content_type=None)

    async def get_powermeter_watts(self) -> list[float]:
        results = await asyncio.gather(*(self.get_json(u) for u in self.uuids))
        return [int(r["data"][0]["tuples"][0][1]) for r in results]
