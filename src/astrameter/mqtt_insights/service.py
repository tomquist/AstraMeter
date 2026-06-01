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
from .marstek_mqtt import (
    MarstekMqttBinding,
    MarstekPollContext,
    app_topics_for,
    build_cd4_response,
    build_response,
    device_topics_for,
    parse_app_topic,
    parse_marstek_poll_payload,
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
    addon_slug: str | None = None
    # Respond to Marstek app MQTT polls for CT002/CT003 on the same
    # broker connection. Combined with hame-relay this surfaces the
    # emulator in the Marstek app. Default on; requires [MARSTEK]
    # credentials so that the managed MAC matches the cloud device.
    marstek_mqtt_enabled: bool = True
    # Periodic broadcast interval (seconds). When > 0 and marstek_mqtt_enabled,
    # publish power values for every registered binding at this cadence so the
    # Marstek app stays up-to-date without relying solely on its own polls.
    marstek_mqtt_interval: float = 300.0


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
        self._distribution_weight_handlers: dict[str, Callable[[str, float], None]] = {}
        self._rotation_handlers: dict[str, Callable[[], None]] = {}
        self._connected = asyncio.Event()
        # Marstek MQTT responder state — populated via register_marstek().
        self._marstek_bindings: dict[str, MarstekMqttBinding] = {}
        self._marstek_lock = asyncio.Lock()
        self._client: aiomqtt.Client | None = None
        # Rate-limit per-device get_values failure logging so a broken
        # powermeter doesn't flood the log at hm2mqtt's poll cadence.
        self._marstek_get_values_failed: set[str] = set()
        # In-flight poll handlers — tracked so one slow powermeter doesn't
        # block the listener loop, and so we can cancel pending tasks on
        # reconnect / shutdown. Keyed by binding device_id so we serialize
        # work per binding (skip spawning while a prior task is in flight).
        self._marstek_tasks_by_binding: dict[str, asyncio.Task[None]] = {}

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

    def register_distribution_weight_handler(
        self, device_id: str, handler: Callable[[str, float], None]
    ) -> None:
        self._distribution_weight_handlers[device_id] = handler

    def register_rotation_handler(
        self, device_id: str, handler: Callable[[], None]
    ) -> None:
        self._rotation_handlers[device_id] = handler

    def unregister_handlers(self, device_id: str) -> None:
        """Remove all command handlers for a device (e.g. on device stop)."""
        self._active_handlers.pop(device_id, None)
        self._manual_target_handlers.pop(device_id, None)
        self._auto_target_handlers.pop(device_id, None)
        self._distribution_weight_handlers.pop(device_id, None)
        self._rotation_handlers.pop(device_id, None)

    # ── Marstek MQTT responder ────────────────────────────────────────

    @property
    def marstek_mqtt_enabled(self) -> bool:
        return self._config.marstek_mqtt_enabled

    async def register_marstek(self, binding: MarstekMqttBinding) -> None:
        """Register a CT002/CT003 Marstek MQTT responder for *binding*.

        If already connected, live-subscribes to the App topics; otherwise
        the ``_run`` loop picks up the new entry on the next (re)connect.
        """
        if not self._config.marstek_mqtt_enabled:
            return
        async with self._marstek_lock:
            existing = self._marstek_bindings.get(binding.device_id)
            if existing is not None and existing.mac != binding.mac:
                logger.warning(
                    "Marstek MQTT: re-registering %s with a different MAC (%s → %s)",
                    binding.device_id,
                    existing.mac,
                    binding.mac,
                )
            self._marstek_bindings[binding.device_id] = binding
            client = self._client
            if client is not None:
                for topic in app_topics_for(binding):
                    with contextlib.suppress(aiomqtt.MqttError):
                        await client.subscribe(topic)

    async def unregister_marstek(self, device_id: str) -> None:
        async with self._marstek_lock:
            binding = self._marstek_bindings.pop(device_id, None)
            self._marstek_get_values_failed.discard(device_id)
            # Cancel any in-flight poll handler so it can't publish a stale
            # reply after the binding is gone. The done_callback removes the
            # entry from the map.
            pending_task = self._marstek_tasks_by_binding.get(device_id)
            client = self._client
            if binding is not None and client is not None:
                for topic in app_topics_for(binding):
                    with contextlib.suppress(aiomqtt.MqttError):
                        await client.unsubscribe(topic)
        if pending_task is not None and not pending_task.done():
            pending_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pending_task

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

                    # Subscribe to command topics.  Each per-consumer setting
                    # has its own retained command sub-topic
                    # ({base}/ct002/<dev>/consumer/<cid>/<field>/set); the
                    # device-level button keeps the plain {base}/ct002/<dev>/set.
                    await client.subscribe(f"{cfg.base_topic}/ct002/+/consumer/+/+/set")
                    await client.subscribe(f"{cfg.base_topic}/ct002/+/set")

                    # Subscribe to Marstek App topics for every registered
                    # binding. Store the client so register_marstek() called
                    # while already connected can live-subscribe too.
                    if cfg.marstek_mqtt_enabled:
                        async with self._marstek_lock:
                            self._client = client
                            for binding in self._marstek_bindings.values():
                                for topic in app_topics_for(binding):
                                    await client.subscribe(topic)

                    self._connected.set()

                    try:
                        coros: list[Any] = [
                            self._publish_loop(client),
                            self._listen_commands(client),
                        ]
                        if cfg.marstek_mqtt_enabled and cfg.marstek_mqtt_interval > 0:
                            coros.append(self._marstek_broadcast_loop(client))
                        await asyncio.gather(*coros)
                    finally:
                        async with self._marstek_lock:
                            self._client = None
                        await self._cancel_marstek_tasks()

            except asyncio.CancelledError:
                self._connected.clear()
                # Graceful shutdown: publish offline in a shielded scope
                # so the pending cancellation doesn't abort the publish.
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._publish_offline(cfg, tls_context))
                raise
            except (aiomqtt.MqttError, OSError) as exc:
                self._connected.clear()
                # Reconnect loop — traceback would be noisy, keep it terse.
                logger.warning(
                    "MQTT Insights connection error: %s. Reconnecting in %ss...",
                    exc,
                    RECONNECT_DELAY,
                    exc_info=False,
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
            "distribution_weight": data.get("distribution_weight", 1.0),
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
                    base, did, cfg.ha_discovery_prefix, addon_slug=cfg.addon_slug
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
                    base, did, cfg.ha_discovery_prefix, addon_slug=cfg.addon_slug
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
            if topic_str.startswith("hame_energy/") or topic_str.startswith(
                "marstek_energy/"
            ):
                await self._handle_marstek_message(client, message)
                continue
            if not topic_str.startswith(prefix) or not topic_str.endswith(suffix):
                continue

            middle = topic_str[len(prefix) : -len(suffix)]

            raw = message.payload
            try:
                payload_str = raw.decode() if isinstance(raw, bytes) else str(raw)
            except UnicodeDecodeError:
                logger.warning("Invalid command payload on %s", topic_str)
                continue

            # Distinguish device-level vs consumer-level topics.
            #   consumer: {base}/ct002/<dev>/consumer/<cid>/<field>/set (scalar)
            #   device:   {base}/ct002/<dev>/set                        (JSON)
            parts = middle.split("/consumer/", 1)
            if len(parts) == 2:
                device_id, rest = parts
                consumer_id, sep, field = rest.rpartition("/")
                if not sep:
                    logger.warning("Malformed consumer command topic %s", topic_str)
                    continue
                self._handle_consumer_field_command(
                    device_id, consumer_id, field, payload_str
                )
            else:
                # Device-level: {base}/ct002/{device_id}/set — JSON body.
                try:
                    cmd = json.loads(payload_str)
                except json.JSONDecodeError:
                    logger.warning("Invalid command payload on %s", topic_str)
                    continue
                if not isinstance(cmd, dict):
                    logger.warning(
                        "Command payload is not a JSON object on %s", topic_str
                    )
                    continue
                self._handle_device_command(middle, cmd)

    @staticmethod
    def _parse_bool(payload: str) -> bool | None:
        token = payload.strip().lower()
        if token in ("true", "on", "1"):
            return True
        if token in ("false", "off", "0"):
            return False
        return None

    def _dispatch(
        self,
        handlers: dict,
        label: str,
        device_id: str,
        consumer_id: str,
        *args: Any,
    ) -> None:
        handler = handlers.get(device_id)
        if not handler:
            logger.debug(
                "No %s handler for device %s (consumer %s)",
                label,
                device_id,
                consumer_id,
            )
            return
        try:
            handler(consumer_id, *args)
        except Exception:
            logger.exception(
                "%s handler error for %s/%s", label, device_id, consumer_id
            )

    def _handle_consumer_field_command(
        self, device_id: str, consumer_id: str, field: str, payload: str
    ) -> None:
        # An empty payload is how a retained command gets cleared — ignore it
        # rather than logging a spurious "invalid value" warning.
        if not payload.strip():
            return

        if field == "active":
            value = self._parse_bool(payload)
            if value is None:
                logger.warning(
                    "Invalid active value for %s/%s: %r",
                    device_id,
                    consumer_id,
                    payload,
                )
                return
            self._dispatch(
                self._active_handlers, "active", device_id, consumer_id, value
            )
        elif field == "auto_target":
            value = self._parse_bool(payload)
            if value is None:
                logger.warning(
                    "Invalid auto_target value for %s/%s: %r",
                    device_id,
                    consumer_id,
                    payload,
                )
                return
            self._dispatch(
                self._auto_target_handlers,
                "auto_target",
                device_id,
                consumer_id,
                value,
            )
        elif field == "manual_target":
            try:
                target = float(payload)
            except ValueError:
                logger.warning(
                    "Invalid manual_target value for %s/%s: %r",
                    device_id,
                    consumer_id,
                    payload,
                )
                return
            if not math.isfinite(target) or not -10000 <= target <= 10000:
                logger.warning(
                    "Out-of-range manual_target for %s/%s: %s",
                    device_id,
                    consumer_id,
                    target,
                )
                return
            self._dispatch(
                self._manual_target_handlers,
                "manual_target",
                device_id,
                consumer_id,
                target,
            )
        elif field == "distribution_weight":
            try:
                weight = float(payload)
            except ValueError:
                logger.warning(
                    "Invalid distribution_weight value for %s/%s: %r",
                    device_id,
                    consumer_id,
                    payload,
                )
                return
            if not math.isfinite(weight) or not 0.0 <= weight <= 10.0:
                logger.warning(
                    "Out-of-range distribution_weight for %s/%s: %s",
                    device_id,
                    consumer_id,
                    weight,
                )
                return
            self._dispatch(
                self._distribution_weight_handlers,
                "distribution_weight",
                device_id,
                consumer_id,
                weight,
            )
        else:
            logger.debug(
                "Unknown consumer command field %r for %s/%s",
                field,
                device_id,
                consumer_id,
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

    async def _marstek_broadcast_loop(self, client: aiomqtt.Client) -> None:
        """Periodically publish power values for all registered bindings."""
        interval = self._config.marstek_mqtt_interval
        while True:
            async with self._marstek_lock:
                bindings = tuple(self._marstek_bindings.values())
            for binding in bindings:
                self._spawn_marstek_poll_task(
                    client,
                    binding,
                    MarstekPollContext(echo_cd=1, slave_id=None),
                )
            await asyncio.sleep(interval)

    async def _handle_marstek_message(
        self, client: aiomqtt.Client, message: aiomqtt.Message
    ) -> None:
        """Dispatch a poll quickly; offload the response to a task so a
        slow powermeter can't stall the listener loop."""
        topic = str(message.topic)
        parsed = parse_app_topic(topic)
        if parsed is None:
            return
        ct_type, mac = parsed
        binding = await self._find_marstek_binding(ct_type, mac)
        if binding is None:
            logger.debug("Marstek MQTT: no binding for %s/%s", ct_type, mac)
            return

        body = message.payload if isinstance(message.payload, bytes) else b""
        poll = parse_marstek_poll_payload(body)
        if poll is None:
            logger.debug("Marstek MQTT: non-poll payload on %s", topic)
            return

        self._spawn_marstek_poll_task(client, binding, poll)

    def _spawn_marstek_poll_task(
        self,
        client: aiomqtt.Client,
        binding: MarstekMqttBinding,
        poll: MarstekPollContext,
    ) -> None:
        """Spawn a poll handler task, but only if one isn't already in flight
        for *binding*. Concurrent overlapping reads for the same binding are
        suppressed so a slow powermeter can't queue up duplicate work."""
        existing = self._marstek_tasks_by_binding.get(binding.device_id)
        if existing is not None and not existing.done():
            logger.debug(
                "Marstek MQTT: skipping poll for %s — prior handler still running",
                binding.device_id,
            )
            return
        task = asyncio.create_task(self._serve_marstek_poll(client, binding, poll))
        self._marstek_tasks_by_binding[binding.device_id] = task

        def _done(t: asyncio.Task[None], _device_id: str = binding.device_id) -> None:
            # Only clear the slot if it still points at *this* task — a later
            # unregister/register could have replaced it.
            if self._marstek_tasks_by_binding.get(_device_id) is t:
                self._marstek_tasks_by_binding.pop(_device_id, None)

        task.add_done_callback(_done)

    async def _serve_marstek_poll(
        self,
        client: aiomqtt.Client,
        binding: MarstekMqttBinding,
        poll: MarstekPollContext,
    ) -> None:
        if poll.echo_cd == 4:
            try:
                if binding.get_cd4_slave_csv is None:
                    slv = ""
                else:
                    slv = binding.get_cd4_slave_csv()
                payload = build_cd4_response(slv)
            except Exception:
                if binding.device_id not in self._marstek_get_values_failed:
                    logger.exception(
                        "Marstek MQTT: cd=4 slave list failed for %s; suppressing "
                        "further failures until recovery",
                        binding.device_id,
                    )
                    self._marstek_get_values_failed.add(binding.device_id)
                return
            if binding.device_id in self._marstek_get_values_failed:
                logger.info(
                    "Marstek MQTT: poll value fetch recovered for %s",
                    binding.device_id,
                )
                self._marstek_get_values_failed.discard(binding.device_id)
        else:
            try:
                watts = await binding.get_values()
            except Exception:
                if binding.device_id not in self._marstek_get_values_failed:
                    logger.exception(
                        "Marstek MQTT: poll value fetch failed for %s; suppressing "
                        "further failures until values recover",
                        binding.device_id,
                    )
                    self._marstek_get_values_failed.add(binding.device_id)
                return
            if binding.device_id in self._marstek_get_values_failed:
                logger.info(
                    "Marstek MQTT: poll value fetch recovered for %s",
                    binding.device_id,
                )
                self._marstek_get_values_failed.discard(binding.device_id)

            n_slaves = 0
            if binding.get_connected_slave_count is not None:
                n_slaves = binding.get_connected_slave_count()
            payload = build_response(
                binding, list(watts), poll=poll, connected_slave_count=n_slaves
            )

        # Re-check the active binding before publishing: unregister_marstek
        # may have run while we awaited get_values, in which case publishing
        # a reply for a defunct binding would leak stale data.
        async with self._marstek_lock:
            current = self._marstek_bindings.get(binding.device_id)
        if current is not binding:
            return

        for reply_topic in device_topics_for(binding):
            with contextlib.suppress(aiomqtt.MqttError):
                await client.publish(reply_topic, payload=payload, qos=0, retain=False)

    async def _cancel_marstek_tasks(self) -> None:
        pending = tuple(self._marstek_tasks_by_binding.values())
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self._marstek_tasks_by_binding.clear()

    async def _find_marstek_binding(
        self, ct_type: str, mac: str
    ) -> MarstekMqttBinding | None:
        # Snapshot under the lock so a concurrent (un)register can't mutate
        # the dict mid-scan.
        async with self._marstek_lock:
            candidates = tuple(self._marstek_bindings.values())
        mac_lower = mac.lower()
        for binding in candidates:
            if binding.ct_type == ct_type and binding.mac == mac_lower:
                return binding
        return None
