"""MQTT Insights service — publishes internal state to MQTT with HA Discovery."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import ssl
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import aiomqtt

from astrameter.config.logger import logger

from .discovery import (
    _sanitize_id,
    build_ct002_consumer_discovery,
    build_ct002_device_discovery,
    build_shelly_battery_discovery,
    build_shelly_device_discovery,
)

RECONNECT_DELAY = 5
QUEUE_MAX_SIZE = 100


async def _arp_lookup(ip: str) -> str:
    """Best-effort ARP lookup via /proc/net/arp. Returns 'AA:BB:CC:DD:EE:FF' or ''."""

    def _sync_lookup() -> str:
        try:
            with open("/proc/net/arp") as f:
                for line in f:
                    parts = line.split()
                    if (
                        len(parts) >= 4
                        and parts[0] == ip
                        and parts[3] != "00:00:00:00:00:00"
                    ):
                        return parts[3].upper()
        except OSError:
            pass
        return ""

    return await asyncio.to_thread(_sync_lookup)


@dataclass
class MqttInsightsConfig:
    broker: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    base_topic: str = "astrameter"
    ha_discovery: bool = True
    ha_discovery_prefix: str = "homeassistant"


@dataclass
class _Event:
    kind: str  # "ct002", "ct002_remove", "shelly", "shelly_remove"
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
        self._pending_arp: set[str] = set()
        self._active_handlers: dict[str, Callable[[str, bool], None]] = {}
        self._manual_target_handlers: dict[str, Callable[[str, float], None]] = {}
        self._auto_target_handlers: dict[str, Callable[[str, bool], None]] = {}
        self._rotation_handlers: dict[str, Callable[[], None]] = {}
        self._connected = asyncio.Event()

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

    def on_shelly_battery_removed(self, device_id: str, battery_ip: str) -> None:
        """Queue Shelly battery removal event."""
        ip_slug = _sanitize_id(battery_ip)
        evt = _Event(kind="shelly_remove", device_id=device_id, entity_id=ip_slug)
        self._put_nowait(evt)

    def register_active_handler(
        self, device_id: str, handler: Callable[[str, bool], None]
    ) -> None:
        self._active_handlers[device_id] = handler

    def register_manual_target_handler(
        self, device_id: str, handler: Callable[[str, float], None]
    ) -> None:
        self._manual_target_handlers[device_id] = handler

    def register_auto_target_handler(
        self, device_id: str, handler: Callable[[str, bool], None]
    ) -> None:
        self._auto_target_handlers[device_id] = handler

    def register_rotation_handler(
        self, device_id: str, handler: Callable[[], None]
    ) -> None:
        self._rotation_handlers[device_id] = handler

    def unregister_handlers(self, device_id: str) -> None:
        """Remove all command handlers for a device (e.g. on device stop)."""
        self._active_handlers.pop(device_id, None)
        self._manual_target_handlers.pop(device_id, None)
        self._auto_target_handlers.pop(device_id, None)
        self._rotation_handlers.pop(device_id, None)

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        self._connected.clear()
        self._task = asyncio.create_task(self._run())

    async def wait_connected(self, timeout: float = 10) -> None:
        """Wait until the service has connected and subscribed."""
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

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
                    await client.subscribe(f"{cfg.base_topic}/ct002/+/set")

                    self._connected.set()

                    # Run publish loop and message listener concurrently
                    await asyncio.gather(
                        self._publish_loop(client),
                        self._listen_commands(client),
                    )

            except asyncio.CancelledError:
                self._connected.clear()
                # Graceful shutdown: publish offline in a shielded scope
                # so the pending cancellation doesn't abort the publish.
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._publish_offline(cfg, tls_context))
                raise
            except (aiomqtt.MqttError, OSError) as exc:
                self._connected.clear()
                logger.warning(
                    "MQTT Insights connection error: %s. Reconnecting in %ss...",
                    exc,
                    RECONNECT_DELAY,
                )
                await asyncio.sleep(RECONNECT_DELAY)
            except Exception:
                self._connected.clear()
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
                elif evt.kind == "shelly_remove":
                    await self._handle_shelly_remove(client, base, evt)
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
            "poll_interval": data.get("poll_interval"),
            "last_seen": data.get("last_seen", ""),
            "manual_target": data.get("manual_target"),
            "auto_target": data.get("auto_target", True),
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

            need_discovery = consumer_key not in self._discovered_ct002_consumers
            need_arp_retry = consumer_key in self._pending_arp

            if need_discovery or need_arp_retry:
                network_mac = ""
                battery_ip = data.get("battery_ip", "")
                if battery_ip:
                    network_mac = await _arp_lookup(battery_ip)

                if need_discovery:
                    self._discovered_ct002_consumers.add(consumer_key)
                    if battery_ip and not network_mac:
                        self._pending_arp.add(consumer_key)

                if network_mac:
                    self._pending_arp.discard(consumer_key)

                if need_discovery or network_mac:
                    topic, payload = build_ct002_consumer_discovery(
                        base,
                        did,
                        cid,
                        cfg.ha_discovery_prefix,
                        device_type=data.get("device_type", ""),
                        network_mac=network_mac,
                        battery_ip=battery_ip,
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
        self._pending_arp.discard(consumer_key)

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
            "poll_interval": data.get("poll_interval"),
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

    async def _handle_shelly_remove(
        self,
        client: aiomqtt.Client,
        base: str,
        evt: _Event,
    ) -> None:
        did = evt.device_id
        ip_slug = evt.entity_id
        battery_key = f"{did}/{ip_slug}"
        avail_topic = f"{base}/shelly/{did}/battery/{ip_slug}/availability"
        await client.publish(avail_topic, payload=b"offline", retain=True)
        self._discovered_shelly_batteries.discard(battery_key)

    async def _listen_commands(self, client: aiomqtt.Client) -> None:
        base = self._config.base_topic
        prefix = f"{base}/ct002/"
        suffix = "/set"

        async for message in client.messages:
            topic_str = str(message.topic)
            if not topic_str.startswith(prefix) or not topic_str.endswith(suffix):
                continue

            middle = topic_str[len(prefix) : -len(suffix)]

            raw = message.payload
            try:
                payload_str = raw.decode() if isinstance(raw, bytes) else str(raw)
                cmd = json.loads(payload_str)
            except (json.JSONDecodeError, UnicodeDecodeError):
                logger.warning("Invalid command payload on %s", topic_str)
                continue

            if not isinstance(cmd, dict):
                logger.warning("Command payload is not a JSON object on %s", topic_str)
                continue

            # Distinguish device-level vs consumer-level topics.
            parts = middle.split("/consumer/", 1)
            if len(parts) == 2:
                device_id, consumer_id = parts
                self._handle_consumer_command(device_id, consumer_id, cmd)
            else:
                # Device-level: {base}/ct002/{device_id}/set
                device_id = middle
                self._handle_device_command(device_id, cmd)

    def _handle_consumer_command(
        self, device_id: str, consumer_id: str, cmd: dict
    ) -> None:
        if "active" in cmd:
            raw = cmd["active"]
            if raw is True or raw is False:
                handler = self._active_handlers.get(device_id)
                if handler:
                    try:
                        handler(consumer_id, raw)
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
            else:
                logger.warning("Invalid active value for %s/%s", device_id, consumer_id)

        if "manual_target" in cmd:
            raw_target = cmd["manual_target"]
            if isinstance(raw_target, bool):
                logger.warning(
                    "Invalid manual_target value for %s/%s", device_id, consumer_id
                )
            else:
                try:
                    target = float(raw_target)
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid manual_target value for %s/%s",
                        device_id,
                        consumer_id,
                    )
                else:
                    if not math.isfinite(target):
                        logger.warning(
                            "Non-finite manual_target for %s/%s",
                            device_id,
                            consumer_id,
                        )
                    elif not -10000 <= target <= 10000:
                        logger.warning(
                            "Out-of-range manual_target for %s/%s: %s",
                            device_id,
                            consumer_id,
                            target,
                        )
                    else:
                        handler = self._manual_target_handlers.get(device_id)
                        if handler:
                            try:
                                handler(consumer_id, target)
                            except Exception:
                                logger.exception(
                                    "Manual target handler error for %s/%s",
                                    device_id,
                                    consumer_id,
                                )
                        else:
                            logger.debug(
                                "No manual_target handler for device %s (consumer %s)",
                                device_id,
                                consumer_id,
                            )

        if "auto_target" in cmd:
            raw = cmd["auto_target"]
            if raw is True or raw is False:
                handler = self._auto_target_handlers.get(device_id)
                if handler:
                    try:
                        handler(consumer_id, raw)
                    except Exception:
                        logger.exception(
                            "Auto target handler error for %s/%s",
                            device_id,
                            consumer_id,
                        )
                else:
                    logger.debug(
                        "No auto_target handler for device %s (consumer %s)",
                        device_id,
                        consumer_id,
                    )
            else:
                logger.warning(
                    "Invalid auto_target value for %s/%s", device_id, consumer_id
                )

    def _handle_device_command(self, device_id: str, cmd: dict) -> None:
        if cmd.get("force_rotation") is True:
            handler = self._rotation_handlers.get(device_id)
            if handler:
                try:
                    handler()
                except Exception:
                    logger.exception("Rotation handler error for device %s", device_id)
            else:
                logger.debug("No rotation handler for device %s", device_id)
