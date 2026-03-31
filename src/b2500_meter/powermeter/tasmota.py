from urllib.parse import urlencode

import aiohttp
from aiohttp import ClientTimeout

from .base import Powermeter


class Tasmota(Powermeter):
    def __init__(
        self,
        ip: str,
        user: str,
        password: str,
        json_status: str,
        json_payload_mqtt_prefix: str,
        json_power_mqtt_label: str,
        json_power_input_mqtt_label: str,
        json_power_output_mqtt_label: str,
        json_power_calculate: bool,
    ):
        self.ip = ip
        self.user = user
        self.password = password
        self.json_status = json_status
        self.json_payload_mqtt_prefix = json_payload_mqtt_prefix
        self.json_power_mqtt_label = json_power_mqtt_label
        self.json_power_input_mqtt_label = json_power_input_mqtt_label
        self.json_power_output_mqtt_label = json_power_output_mqtt_label
        self.json_power_calculate = json_power_calculate
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self.session:
            return
        self.session = aiohttp.ClientSession(timeout=ClientTimeout(total=10))

    async def stop(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def get_json(self, path):
        if not self.session:
            raise RuntimeError("Session not started; call start() first")
        url = f"http://{self.ip}{path}"
        async with self.session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def get_powermeter_watts_async(self) -> list[float]:
        if not self.user:
            response = await self.get_json("/cm?cmnd=status%2010")
        else:
            qs = urlencode(
                {"user": self.user, "password": self.password, "cmnd": "status 10"}
            )
            response = await self.get_json(f"/cm?{qs}")
        value = response[self.json_status][self.json_payload_mqtt_prefix]
        if not self.json_power_calculate:
            return [int(value[self.json_power_mqtt_label])]
        else:
            power_in = value[self.json_power_input_mqtt_label]
            power_out = value[self.json_power_output_mqtt_label]
            return [int(power_in) - int(power_out)]
