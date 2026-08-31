from .base import HttpPollingPowermeter


class Shrdzm(HttpPollingPowermeter):
    def __init__(self, ip: str, user: str, password: str):
        super().__init__(f"http://{ip}")
        self.ip = ip
        self.user = user
        self.password = password

    async def get_powermeter_watts(self) -> list[float]:
        response = await self._get_json(
            f"/getLastData?user={self.user}&password={self.password}"
        )
        return [int(response["1.7.0"]) - int(response["2.7.0"])]
