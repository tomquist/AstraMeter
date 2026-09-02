import asyncio

from .http_client import HttpPowermeter


class VZLogger(HttpPowermeter):
    def __init__(self, ip: str, port: str, uuid: str | list[str]):
        self.ip = ip
        self.port = port
        self.uuids = [uuid] if isinstance(uuid, str) else list(uuid)

    async def get_powermeter_watts(self) -> list[float]:
        results = await asyncio.gather(
            *(
                self.get_json(f"http://{self.ip}:{self.port}/{uuid}")
                for uuid in self.uuids
            )
        )
        return [int(r["data"][0]["tuples"][0][1]) for r in results]
