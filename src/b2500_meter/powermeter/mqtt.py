import asyncio
import contextlib
import json

import aiomqtt
from jsonpath_ng import parse

from b2500_meter.config.logger import logger

from .base import Powermeter

RECONNECT_DELAY = 5


def extract_json_value(data, path):
    jsonpath_expr = parse(path)
    match = jsonpath_expr.find(data)
    if match:
        return float(match[0].value)
    else:
        raise ValueError("No match found for the JSON path")


class MqttPowermeter(Powermeter):
    def __init__(
        self,
        broker: str,
        port: int,
        topic: str,
        json_path: str | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        self.broker = broker
        self.port = port
        self.topic = topic
        self.json_path = json_path
        self.username = username
        self.password = password
        self.value: float | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._message_event = asyncio.Event()
        self._connected_event = asyncio.Event()

    async def start(self) -> None:
        self.value = None
        self._message_event.clear()
        self._connected_event.clear()
        self._run_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.broker,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    keepalive=60,
                ) as client:
                    logger.info(f"Connected to MQTT broker {self.broker}:{self.port}")
                    await client.subscribe(self.topic)
                    self._connected_event.set()
                    async for message in client.messages:
                        raw = message.payload
                        payload = raw.decode() if isinstance(raw, bytes) else str(raw)
                        try:
                            if self.json_path:
                                data = json.loads(payload)
                                self.value = extract_json_value(data, self.json_path)
                            else:
                                self.value = float(payload)
                            self._message_event.set()
                        except (json.JSONDecodeError, ValueError) as e:
                            logger.error(f"Failed to parse MQTT payload: {e}")
            except aiomqtt.MqttError as e:
                self._connected_event.clear()
                logger.warning(
                    f"MQTT connection error: {e}. Reconnecting in {RECONNECT_DELAY}s..."
                )
                await asyncio.sleep(RECONNECT_DELAY)

    async def stop(self) -> None:
        if self._run_task:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
            self._run_task = None

    async def get_powermeter_watts_async(self) -> list[float]:
        if self.value is not None:
            return [self.value]
        raise ValueError("No value received from MQTT")

    async def wait_for_message_async(self, timeout=5):
        if self.value is not None:
            return
        try:
            await asyncio.wait_for(self._message_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for MQTT message") from None
