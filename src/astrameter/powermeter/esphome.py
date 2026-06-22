from .base import HttpPollingPowermeter


class ESPHome(HttpPollingPowermeter):
    def __init__(self, ip: str, port: str, domain: str, id: str):
        super().__init__(f"http://{ip}:{port}")
        self.ip = ip
        self.port = port
        self.domain = domain
        self.id = id

    async def get_powermeter_watts(self) -> list[float]:
        parsed_data = await self._get_json(f"/{self.domain}/{self.id}")
        return [int(parsed_data["value"])]
