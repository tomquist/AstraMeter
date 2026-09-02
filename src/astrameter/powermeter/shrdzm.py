from .http_client import HttpPowermeter


class Shrdzm(HttpPowermeter):
    def __init__(self, ip: str, user: str, password: str) -> None:
        self.ip = ip
        self.user = user
        self.password = password

    async def get_powermeter_watts(self) -> list[float]:
        response = await self.get_json(
            f"http://{self.ip}/getLastData?user={self.user}&password={self.password}"
        )
        return [int(response["1.7.0"]) - int(response["2.7.0"])]
