"""MQTT Insights service — publishes internal state to MQTT with HA Discovery."""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiomqtt

from b2500_meter.config.logger import logger

from .discovery import (
    _sanitize_id,
    build_ct002_consumer_discovery,
    build_ct002_device_discovery,
    build_shelly_battery_discovery,
    build_shelly_device_discovery,
)

RECONNECT_DELAY = 5
QUEUE_MAX_SIZE = 100


@dataclass
class MqttInsightsConfig:
    broker: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    base_topic: str = "b2500_meter"
    ha_discovery: bool = True
    ha_discovery_prefix: str = "homeassistant"


@dataclass
class _Event:
    kind: str  # "ct002", "ct002_status", "ct002_remove", "shelly", "shelly_status"
    device_id: str
    entity_id: str  # consumer_id / battery ip_slug
    data: dict[str, Any] = field(default_factory=dict)


class MqttInsightsService:
    def __init__(self, config: MqttInsightsConfig) -> None:
        self._config = config
        self._queue: asyncio.Queue[_Event] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._task: asyncio.Task[None] | None = None
        self._discovered_ct002_consumers: set[str] = set()
        self._discovered_ct002_devices: set[str] = set()
        self._discovered_shelly_batteries: set[str] = set()
        self._discovered_shelly_devices: set[str] = set()
        self._active_handlers: dict[str, Callable[[str, bool], None]] = {}

    # ── Public API (called from device event listeners) ───────────────

    def on_ct002_response(
        self, device_id: str, consumer_id: str, data: dict[str, Any]
    ) -> None:
        """Queue CT002 consumer event (fire-and-forget)."""
        evt = _Event(
            kind="ct002", device_id=device_id, entity_id=consumer_id, data=data
        )
        self._put_nowait(evt)

    def on_ct002_consumer_removed(self, device_id: str, consumer_id: str) -> None:
        """Queue CT002 consumer removal event."""
        evt = _Event(kind="ct002_remove", device_id=device_id, entity_id=consumer_id)
        self._put_nowait(evt)

    def on_shelly_response(
        self, device_id: str, battery_ip: str, data: dict[str, Any]
    ) -> None:
        """Queue Shelly battery event (fire-and-forget)."""
        ip_slug = _sanitize_id(battery_ip)
        evt = _Event(kind="shelly", device_id=device_id, entity_id=ip_slug, data=data)
        self._put_nowait(evt)

    def register_active_handler(
        self, device_id: str, handler: Callable[[str, bool], None]
    ) -> None:
        self._active_handlers[device_id] = handler

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # ── Internal ──────────────────────────────────────────────────────

    def _put_nowait(self, evt: _Event) -> None:
        try:
            self._queue.put_nowait(evt)
        except asyncio.QueueFull:
            # Drop oldest to make room
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(evt)

    async def _run(self) -> None:
        cfg = self._config
        tls_context = ssl.create_default_context() if cfg.tls else None

        while True:
            try:
                async with aiomqtt.Client(
                    hostname=cfg.broker,
                    port=cfg.port,
                    username=cfg.username,
                    password=cfg.password,
                    tls_context=tls_context,
                    keepalive=60,
                    will=aiomqtt.Will(
                        topic=f"{cfg.base_topic}/status",
                        payload=b"offline",
                        qos=1,
                        retain=True,
                    ),
                ) as client:
                    logger.info(
                        "MQTT Insights connected to %s:%s", cfg.broker, cfg.port
                    )
                    # Clear discovery sets on (re)connect so we re-publish
                    self._discovered_ct002_consumers.clear()
                    self._discovered_ct002_devices.clear()
                    self._discovered_shelly_batteries.clear()
                    self._discovered_shelly_devices.clear()

                    # Publish online status
                    await client.publish(
                        f"{cfg.base_topic}/status",
                        payload=b"online",
                        qos=1,
                        retain=True,
                    )

                    # Subscribe to command topics
                    await client.subscribe(f"{cfg.base_topic}/ct002/+/consumer/+/set")

                    # Run publish loop and message listener concurrently
                    await asyncio.gather(
                        self._publish_loop(client),
                        self._listen_commands(client),
                    )

            except asyncio.CancelledError:
                # Graceful shutdown: publish offline in a shielded scope
                # so the pending cancellation doesn't abort the publish.
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._publish_offline(cfg, tls_context))
                raise
            except (aiomqtt.MqttError, OSError) as exc:
                logger.warning(
                    "MQTT Insights connection error: %s. Reconnecting in %ss...",
                    exc,
                    RECONNECT_DELAY,
                )
                await asyncio.sleep(RECONNECT_DELAY)
            except Exception:
                logger.exception(
                    "MQTT Insights unexpected error, reconnecting in %ss...",
                    RECONNECT_DELAY,
                )
                await asyncio.sleep(RECONNECT_DELAY)

    async def _publish_offline(
        self, cfg: MqttInsightsConfig, tls_context: ssl.SSLContext | None
    ) -> None:
        async with aiomqtt.Client(
            hostname=cfg.broker,
            port=cfg.port,
            username=cfg.username,
            password=cfg.password,
            tls_context=tls_context,
        ) as client:
            await client.publish(
                f"{cfg.base_topic}/status",
                payload=b"offline",
                qos=1,
                retain=True,
            )

    async def _publish_loop(self, client: aiomqtt.Client) -> None:
        cfg = self._config
        base = cfg.base_topic

        while True:
            evt = await self._queue.get()

            try:
                if evt.kind == "ct002":
                    await self._handle_ct002_event(client, base, cfg, evt)
                elif evt.kind == "ct002_remove":
                    await self._handle_ct002_remove(client, base, cfg, evt)
                elif evt.kind == "shelly":
                    await self._handle_shelly_event(client, base, cfg, evt)
            except aiomqtt.MqttError:
                raise
            except Exception:
                logger.exception("Error publishing MQTT Insights event")

    async def _handle_ct002_event(
        self,
        client: aiomqtt.Client,
        base: str,
        cfg: MqttInsightsConfig,
        evt: _Event,
    ) -> None:
        did = evt.device_id
        cid = evt.entity_id
        data = evt.data

        # Per-consumer state
        consumer_key = f"{did}/{cid}"
        state_topic = f"{base}/ct002/{did}/consumer/{cid}"
        avail_topic = f"{state_topic}/availability"

        # Extract consumer-level state
        consumer_state = {
            "grid_power": data.get("grid_power", {}),
            "target": data.get("target", {}),
            "phase": data.get("phase", ""),
            "reported_power": data.get("reported_power", 0),
            "device_type": data.get("device_type", ""),
            "battery_ip": data.get("battery_ip", ""),
            "ct_type": data.get("ct_type", ""),
            "ct_mac": data.get("ct_mac", ""),
            "saturation": data.get("saturation", 0.0),
            "last_target": data.get("last_target"),
            "active": data.get("active", True),
            "last_seen": data.get("last_seen", ""),
        }

        await client.publish(
            state_topic,
            payload=json.dumps(consumer_state).encode(),
            retain=True,
        )
        await client.publish(avail_topic, payload=b"online", retain=True)

        # Device-level status
        device_status = {
            "smooth_target": data.get("smooth_target", 0),
            "active_control": data.get("active_control", False),
            "consumer_count": data.get("consumer_count", 0),
        }
        await client.publish(
            f"{base}/ct002/{did}/status",
            payload=json.dumps(device_status).encode(),
            retain=True,
        )

        # Discovery on first sight
        if cfg.ha_discovery:
            if did not in self._discovered_ct002_devices:
                self._discovered_ct002_devices.add(did)
                topic, payload = build_ct002_device_discovery(
                    base, did, cfg.ha_discovery_prefix
                )
                await client.publish(
                    topic, payload=json.dumps(payload).encode(), retain=True
                )

            if consumer_key not in self._discovered_ct002_consumers:
                self._discovered_ct002_consumers.add(consumer_key)
                topic, payload = build_ct002_consumer_discovery(
                    base, did, cid, cfg.ha_discovery_prefix
                )
                await client.publish(
                    topic, payload=json.dumps(payload).encode(), retain=True
                )

    async def _handle_ct002_remove(
        self,
        client: aiomqtt.Client,
        base: str,
        cfg: MqttInsightsConfig,
        evt: _Event,
    ) -> None:
        did = evt.device_id
        cid = evt.entity_id
        consumer_key = f"{did}/{cid}"
        avail_topic = f"{base}/ct002/{did}/consumer/{cid}/availability"
        await client.publish(avail_topic, payload=b"offline", retain=True)
        self._discovered_ct002_consumers.discard(consumer_key)

    async def _handle_shelly_event(
        self,
        client: aiomqtt.Client,
        base: str,
        cfg: MqttInsightsConfig,
        evt: _Event,
    ) -> None:
        did = evt.device_id
        ip_slug = evt.entity_id
        data = evt.data

        battery_key = f"{did}/{ip_slug}"
        state_topic = f"{base}/shelly/{did}/battery/{ip_slug}"
        avail_topic = f"{state_topic}/availability"

        battery_state = {
            "grid_power": data.get("grid_power", {}),
            "active": data.get("active", True),
            "last_seen": data.get("last_seen", ""),
        }

        await client.publish(
            state_topic,
            payload=json.dumps(battery_state).encode(),
            retain=True,
        )
        await client.publish(avail_topic, payload=b"online", retain=True)

        # Device-level status
        device_status = {
            "battery_count": data.get("battery_count", 0),
        }
        await client.publish(
            f"{base}/shelly/{did}/status",
            payload=json.dumps(device_status).encode(),
            retain=True,
        )

        # Discovery
        if cfg.ha_discovery:
            if did not in self._discovered_shelly_devices:
                self._discovered_shelly_devices.add(did)
                topic, payload = build_shelly_device_discovery(
                    base, did, cfg.ha_discovery_prefix
                )
                await client.publish(
                    topic, payload=json.dumps(payload).encode(), retain=True
                )

            if battery_key not in self._discovered_shelly_batteries:
                self._discovered_shelly_batteries.add(battery_key)
                topic, payload = build_shelly_battery_discovery(
                    base, did, ip_slug, cfg.ha_discovery_prefix
                )
                await client.publish(
                    topic, payload=json.dumps(payload).encode(), retain=True
                )

    async def _listen_commands(self, client: aiomqtt.Client) -> None:
        base = self._config.base_topic
        prefix = f"{base}/ct002/"
        suffix = "/set"

        async for message in client.messages:
            topic_str = str(message.topic)
            if not topic_str.startswith(prefix) or not topic_str.endswith(suffix):
                continue

            # Parse: {base}/ct002/{device_id}/consumer/{consumer_id}/set
            parts = topic_str[len(prefix) : -len(suffix)].split("/consumer/", 1)
            if len(parts) != 2:
                continue
            device_id, consumer_id = parts

            raw = message.payload
            try:
                payload_str = raw.decode() if isinstance(raw, bytes) else str(raw)
                cmd = json.loads(payload_str)
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("Invalid command payload on %s", topic_str)
                continue

            if "active" in cmd:
                active = bool(cmd["active"])
                handler = self._active_handlers.get(device_id)
                if handler:
                    try:
                        handler(consumer_id, active)
                    except Exception:
                        logger.exception(
                            "Active handler error for %s/%s", device_id, consumer_id
                        )
                else:
                    logger.debug(
                        "No active handler for device %s (consumer %s)",
                        device_id,
                        consumer_id,
                    )
