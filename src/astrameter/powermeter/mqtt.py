import asyncio
import contextlib
import json
import logging
import ssl

import aiomqtt

from .base import PushPowermeter
from .json_http import extract_json_value

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")

RECONNECT_DELAY = 5


class MqttPowermeter(PushPowermeter):
    _TIMEOUT_MESSAGE = "Timeout waiting for MQTT message"

    def __init__(
        self,
        broker: str,
        port: int,
        topic: str | list[str],
        json_path: str | list[str] | None = None,
        username: str | None = None,
        password: str | None = None,
        tls: bool = False,
    ):
        super().__init__()
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.tls = tls

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

        self._topic_indices: dict[str, list[int]] = {}
        for index, (topic_name, _) in enumerate(self._subscriptions):
            self._topic_indices.setdefault(topic_name, []).append(index)

        self.values: list[float | None] = [None] * len(self._subscriptions)
        self._run_task: asyncio.Task[None] | None = None
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
        tls_context = ssl.create_default_context() if self.tls else None
        while True:
            try:
                async with aiomqtt.Client(
                    hostname=self.broker,
                    port=self.port,
                    username=self.username,
                    password=self.password,
                    tls_context=tls_context,
                    keepalive=60,
                ) as client:
                    logger.info(f"Connected to MQTT broker {self.broker}:{self.port}")
                    for topic_name in unique_topics:
                        await client.subscribe(topic_name)
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
                        for index in indices:
                            _, json_path = self._subscriptions[index]
                            try:
                                if json_path:
                                    if parsed_json is None:
                                        parsed_json = json.loads(payload)
                                    self.values[index] = extract_json_value(
                                        parsed_json, json_path
                                    )
                                else:
                                    self.values[index] = float(payload)
                                self._message_event.set()
                            except (json.JSONDecodeError, ValueError) as e:
                                logger.error(
                                    f"Failed to parse MQTT payload for index {index}: {e}"
                                )
            except aiomqtt.MqttError as e:
                self._connected_event.clear()
                # Reconnect loop — traceback would be noisy, keep it terse.
                logger.warning(
                    "MQTT connection error: %s. Reconnecting in %ss...",
                    e,
                    RECONNECT_DELAY,
                    exc_info=False,
                )
                await asyncio.sleep(RECONNECT_DELAY)

    async def stop(self) -> None:
        if self._run_task:
            self._run_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._run_task
            self._run_task = None

    def stream_online(self) -> bool | None:
        # Connection + readiness, no timestamp: values are only reset on
        # start(), so a phase whose publisher stops re-publishing keeps its
        # cached value and stays online (mirrors Home Assistant). Only a
        # disconnect or a never-received subscription flips this offline.
        return self._connected_event.is_set() and all(
            v is not None for v in self.values
        )

    async def get_powermeter_watts(self) -> list[float]:
        if all(v is not None for v in self.values):
            return list(self.values)  # type: ignore[arg-type]
        raise ValueError("No value received from MQTT")

    async def wait_for_message(self, timeout: float = 5) -> None:
        # Every subscription must have delivered, so keep waiting for messages
        # until the deadline rather than returning on the first one.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not all(v is not None for v in self.values):
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(self._TIMEOUT_MESSAGE)
            await self.wait_for_next_message(remaining)
