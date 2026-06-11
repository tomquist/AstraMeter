import asyncio

from .base import HttpPollingPowermeter


class VZLogger(HttpPollingPowermeter):
    def __init__(self, ip: str, port: str, uuid: str | list[str]):
        super().__init__(f"http://{ip}:{port}")
        self.ip = ip
        self.port = port
        self.uuids = [uuid] if isinstance(uuid, str) else list(uuid)

    async def get_powermeter_watts(self) -> list[float]:
        results = await asyncio.gather(*(self._get_json(f"/{u}") for u in self.uuids))
        return [int(r["data"][0]["tuples"][0][1]) for r in results]
