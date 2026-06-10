from typing import Any
from urllib.parse import urlencode

from .base import HttpPollingPowermeter


class Tasmota(HttpPollingPowermeter):
    def __init__(
        self,
        ip: str,
        user: str,
        password: str,
        json_status: str,
        json_payload_mqtt_prefix: str,
        json_power_mqtt_label: str | list[str],
        json_power_input_mqtt_label: str | list[str],
        json_power_output_mqtt_label: str | list[str],
        json_power_calculate: bool,
    ):
        super().__init__(f"http://{ip}")
        self.ip = ip
        self.user = user
        self.password = password
        self.json_status = json_status
        self.json_payload_mqtt_prefix = json_payload_mqtt_prefix
        self.json_power_mqtt_labels = (
            [json_power_mqtt_label]
            if isinstance(json_power_mqtt_label, str)
            else list(json_power_mqtt_label)
        )
        self.json_power_input_mqtt_labels = (
            [json_power_input_mqtt_label]
            if isinstance(json_power_input_mqtt_label, str)
            else list(json_power_input_mqtt_label)
        )
        self.json_power_output_mqtt_labels = (
            [json_power_output_mqtt_label]
            if isinstance(json_power_output_mqtt_label, str)
            else list(json_power_output_mqtt_label)
        )
        self.json_power_calculate = json_power_calculate
        if json_power_calculate:
            if len(self.json_power_input_mqtt_labels) != len(
                self.json_power_output_mqtt_labels
            ):
                raise ValueError(
                    "JSON_POWER_INPUT_MQTT_LABEL and JSON_POWER_OUTPUT_MQTT_LABEL "
                    "must have the same number of entries"
                )
            if any(
                not label.strip()
                for label in self.json_power_input_mqtt_labels
                + self.json_power_output_mqtt_labels
            ):
                raise ValueError(
                    "JSON_POWER_INPUT_MQTT_LABEL and JSON_POWER_OUTPUT_MQTT_LABEL "
                    "entries cannot be empty when JSON_POWER_CALCULATE is enabled"
                )

    async def _get_json(self, path: str) -> Any:
        if not self.session:
            raise RuntimeError("Session not started; call start() first")
        url = f"{self._base_url}{path}"
        async with self.session.get(url) as resp:
            resp.raise_for_status()
            return await resp.json(content_type=None)

    async def get_powermeter_watts(self) -> list[float]:
        if not self.user:
            response = await self._get_json("/cm?cmnd=status%2010")
        else:
            qs = urlencode(
                {"user": self.user, "password": self.password, "cmnd": "status 10"}
            )
            response = await self._get_json(f"/cm?{qs}")
        value = response[self.json_status][self.json_payload_mqtt_prefix]
        if not self.json_power_calculate:
            return [int(value[label]) for label in self.json_power_mqtt_labels]
        else:
            return [
                int(value[in_l]) - int(value[out_l])
                for in_l, out_l in zip(
                    self.json_power_input_mqtt_labels,
                    self.json_power_output_mqtt_labels,
                    strict=True,
                )
            ]
