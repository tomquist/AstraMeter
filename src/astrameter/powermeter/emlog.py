from .http_client import HttpPowermeter


class Emlog(HttpPowermeter):
    def __init__(self, ip: str, meterindex: str, json_power_calculate: bool) -> None:
        self.ip = ip
        self.meterindex = meterindex
        self.json_power_calculate = json_power_calculate

    async def get_powermeter_watts(self) -> list[float]:
        response = await self.get_json(
            f"http://{self.ip}/pages/getinformation.php?heute&meterindex={self.meterindex}"
        )
        if not self.json_power_calculate:
            return [int(response["Leistung170"])]
        else:
            power_in = response["Leistung170"]
            power_out = response["Leistung270"]
            return [int(power_in) - int(power_out)]
