from .base import HttpPollingPowermeter


class AmisReader(HttpPollingPowermeter):
    def __init__(self, ip: str):
        super().__init__(f"http://{ip}")
        self.ip = ip

    async def get_powermeter_watts(self) -> list[float]:
        response = await self._get_json("/rest")
        return [int(response["saldo"])]
