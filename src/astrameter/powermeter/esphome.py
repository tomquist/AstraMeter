from .http_client import HttpPowermeter


class ESPHome(HttpPowermeter):
    def __init__(self, ip: str, port: str, domain: str, id: str):
        self.ip = ip
        self.port = port
        self.domain = domain
        self.id = id

    async def get_powermeter_watts(self) -> list[float]:
        parsed_data = await self.get_json(
            f"http://{self.ip}:{self.port}/{self.domain}/{self.id}"
        )
        return [int(parsed_data["value"])]
