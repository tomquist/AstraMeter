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
        topic: str | list[str],
        json_path: str | list[str] | None = None,
        username: str | None = None,
        password: str | None = None,
    ):
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password

        # Normalize topic(s) and json_path(s) into subscription list
        topics = [topic] if isinstance(topic, str) else list(topic)
        if json_path is None:
            paths: list[str | None] = [None] * len(topics)
        elif isinstance(json_path, str):
            paths = [json_path] * len(topics)
        else:
            paths = list(json_path)

        # Handle single topic + multiple paths: replicate topic
        if len(topics) == 1 and len(paths) > 1:
            topics = topics * len(paths)
        # Handle multiple topics + single-element path list (e.g. json_path=["$.a"])
        elif len(topics) > 1 and len(paths) == 1:
            paths = paths * len(topics)

        if not topics:
            raise ValueError("At least one MQTT topic is required.")

        if len(topics) != len(paths):
            raise ValueError(
                f"Topic count ({len(topics)}) and JSON path count ({len(paths)}) "
                f"must match, or one of them must be a single value."
            )

        self._subscriptions: list[tuple[str, str | None]] = list(
            zip(topics, paths, strict=True)
        )

        # Build O(1) topic -> subscription index mapping
        self._topic_indices: dict[str, list[int]] = {}
        for i, (t, _) in enumerate(self._subscriptions):
            self._topic_indices.setdefault(t, []).append(i)

        self.values: list[float | None] = [None] * len(self._subscriptions)
        self._run_task: asyncio.Task[None] | None = None
        self._message_event = asyncio.Event()
        self._connected_event = asyncio.Event()

    @property
    def value(self) -> float | None:
        return self.values[0] if self.values else None

    @value.setter
    def value(self, v: float | None) -> None:
        if self.values:
            self.values[0] = v

    async def start(self) -> None:
        self.values = [None] * len(self._subscriptions)
        self._message_event.clear()
        self._connected_event.clear()
        self._run_task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        unique_topics = list(self._topic_indices.keys())
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
                    for t in unique_topics:
                        await client.subscribe(t)
                    self._connected_event.set()
                    async for message in client.messages:
                        raw = message.payload
                        payload = raw.decode() if isinstance(raw, bytes) else str(raw)
                        topic_str = str(message.topic)
                        indices = self._topic_indices.get(topic_str, [])
                        if not indices:
                            continue
                        # Parse JSON once if any subscription for this topic needs it
                        parsed_json = None
                        for i in indices:
                            _, jp = self._subscriptions[i]
                            try:
                                if jp:
                                    if parsed_json is None:
                                        parsed_json = json.loads(payload)
                                    self.values[i] = extract_json_value(parsed_json, jp)
                                else:
                                    self.values[i] = float(payload)
                                self._message_event.set()
                            except (json.JSONDecodeError, ValueError) as e:
                                logger.error(
                                    f"Failed to parse MQTT payload for index {i}: {e}"
                                )
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
        if all(v is not None for v in self.values):
            return list(self.values)  # type: ignore[arg-type]
        raise ValueError("No value received from MQTT")

    async def wait_for_message_async(self, timeout=5):
        if all(v is not None for v in self.values):
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError("Timeout waiting for MQTT message")
            self._message_event.clear()
            try:
                await asyncio.wait_for(self._message_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                raise TimeoutError("Timeout waiting for MQTT message") from None
            if all(v is not None for v in self.values):
                return
