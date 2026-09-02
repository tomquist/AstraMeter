from .http_client import HttpPowermeter


class AmisReader(HttpPowermeter):
    def __init__(self, ip: str):
        self.ip = ip

    async def get_powermeter_watts(self) -> list[float]:
        response = await self.get_json(f"http://{self.ip}/rest")
        return [int(response["saldo"])]
