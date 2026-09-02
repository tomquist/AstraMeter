from .http_client import HttpPowermeter


class IoBroker(HttpPowermeter):
    def __init__(
        self,
        ip: str,
        port: str,
        current_power_alias: str,
        power_calculate: bool,
        power_input_alias: str,
        power_output_alias: str,
    ):
        self.ip = ip
        self.port = port
        self.current_power_alias = current_power_alias
        self.power_calculate = power_calculate
        self.power_input_alias = power_input_alias
        self.power_output_alias = power_output_alias

    async def _get_bulk(self, aliases: str):
        return await self.get_json(f"http://{self.ip}:{self.port}/getBulk/{aliases}")

    async def get_powermeter_watts(self) -> list[float]:
        if not self.power_calculate:
            response = await self._get_bulk(self.current_power_alias)
            for item in response:
                if item["id"] == self.current_power_alias:
                    return [int(item["val"])]
            raise ValueError(
                f"Alias {self.current_power_alias!r} not found in response"
            )
        else:
            response = await self._get_bulk(
                f"{self.power_input_alias},{self.power_output_alias}"
            )
            power_in = 0
            power_out = 0
            for item in response:
                if item["id"] == self.power_input_alias:
                    power_in = int(item["val"])
                if item["id"] == self.power_output_alias:
                    power_out = int(item["val"])
            return [power_in - power_out]
