"""MQTT Insights service — publishes internal state to MQTT with HA Discovery."""

from __future__ import annotations

import asyncio
import contextlib
import json
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import aiomqtt

from astrameter.config.logger import logger
from astrameter.ct002.controls import (
    CONSUMER_CONTROLS_BY_FIELD,
    ControllableDevice,
    apply_device_control,
)
from astrameter.powermeter.wrappers.health import HealthTrackingPowermeter
from astrameter.version_info import get_version

from .discovery import (
    _sanitize_id,
    build_addon_device_discovery,
    build_ct002_consumer_discovery,
    build_ct002_device_discovery,
    build_powermeter_device_discovery,
    build_retirement_payload,
    build_shelly_battery_discovery,
    build_shelly_device_discovery,
)

if TYPE_CHECKING:
    from astrameter.powermeter.base import Powermeter
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
from .topics import (
    ConsumerCommandTopic,
    MalformedCommandTopic,
    availability_topic,
    bridge_topic,
    consumer_command_filter,
    consumer_command_topic,
    ct002_consumer_topic,
    ct002_status_topic,
    device_command_filter,
    device_command_topic,
    parse_command_topic,
    powermeter_topic,
    shelly_battery_topic,
    shelly_status_topic,
    system_status_topic,
)

RECONNECT_DELAY = 5
QUEUE_MAX_SIZE = 100

# Discovery kinds — the families ``_discovered`` tracks and the status
# snapshot counts separately.
CT002_DEVICE = "ct002_device"
CT002_CONSUMER = "ct002_consumer"
SHELLY_DEVICE = "shelly_device"
SHELLY_BATTERY = "shelly_battery"
POWERMETER = "powermeter"

# Health loop: reuse the control loop's most recent read for a pull powermeter
# if it happened within this many seconds; otherwise issue one bounded probe.
POWERMETER_IDLE_THRESHOLD = 2.0
POWERMETER_PROBE_TIMEOUT = 5.0


async def _publish_json(client: aiomqtt.Client, topic: str, payload: Any) -> None:
    """Publish *payload* as retained JSON at qos 0.

    State and discovery are retained snapshots: a dropped message is superseded
    by the next one, so qos 0 is enough.  Commands are the exception and go out
    through ``MqttInsightsService._publish_command`` at qos 1 — a dashboard
    write the broker drops is a setting that silently reverts.
    """
    await client.publish(topic, payload=json.dumps(payload).encode(), retain=True)


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
    # Per-powermeter "Online" diagnostic sensor publish cadence (seconds).
    # 0 disables the health loop entirely.
    powermeter_health_interval: float = 30.0


@dataclass(frozen=True, slots=True)
class MarstekBindingSnapshot:
    """Immutable view of one registered Marstek MQTT responder binding."""

    device_id: str
    ct_type: str
    mac: str
    ver_v: int
    wifi_rssi: int
    poll_in_flight: bool
    value_fetch_failing: bool


@dataclass(frozen=True, slots=True)
class MqttInsightsSnapshot:
    """Immutable MQTT Insights view for the status API.

    Carries the broker locator only — the username and password stay out of
    the snapshot entirely so they cannot leak into the dashboard document.
    """

    connected: bool
    broker: str
    port: int
    tls: bool
    base_topic: str
    ha_discovery: bool
    ha_discovery_prefix: str
    hub_identifier: str
    queue_depth: int
    queue_dropped_total: int
    discovered_ct002_devices: int
    discovered_ct002_consumers: int
    discovered_shelly_devices: int
    discovered_shelly_batteries: int
    discovered_powermeters: int
    powermeter_health_interval: float
    marstek_enabled: bool
    marstek_interval: float
    marstek_bindings: tuple[MarstekBindingSnapshot, ...]


@dataclass
class _Event:
    kind: str  # "ct002", "ct002_remove", "shelly", "shelly_remove"
    device_id: str
    entity_id: str  # consumer_id / battery ip_slug
    data: dict[str, Any] = field(default_factory=dict)


class MqttInsightsService:
    def __init__(
        self,
        config: MqttInsightsConfig,
        powermeters: list[Powermeter] | None = None,
    ) -> None:
        self._config = config
        self._powermeters: list[Powermeter] = list(powermeters or [])
        self._queue: asyncio.Queue[_Event] = asyncio.Queue(maxsize=QUEUE_MAX_SIZE)
        self._queue_dropped = 0
        self._task: asyncio.Task[None] | None = None
        # Discovery already published this session, as (kind, key) — one set
        # so a new device family adds a kind instead of a sixth parallel set,
        # a sixth ``clear()`` and a sixth counter.
        self._discovered: set[tuple[str, str]] = set()
        # Devices whose controls MQTT commands may drive, by device id.
        self._devices: dict[str, ControllableDevice] = {}
        # Latest retained consumer command per (consumer_id, field), by device.
        # The broker redelivers retained commands right after we subscribe,
        # usually before the owning device has started and registered, so the
        # payload is kept here and replayed when the device registers.
        self._pending_consumer_commands: dict[str, dict[tuple[str, str], str]] = {}
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

    def register_device(self, device_id: str, device: ControllableDevice) -> None:
        """Let MQTT commands drive *device*, applying any retained command
        that arrived before it registered."""
        self._devices[device_id] = device
        self._replay_consumer_commands(device_id)

    def unregister_device(self, device_id: str) -> None:
        self._devices.pop(device_id, None)

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

    @property
    def connected(self) -> bool:
        """True once connected *and* subscribed (cleared on every drop)."""
        return self._connected.is_set()

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

    def status_snapshot(self) -> MqttInsightsSnapshot:
        """Broker and integration state for the status API.

        MUST stay a plain ``def`` doing attribute reads only: the caller
        walks the whole live tree between UDP handlers, so an ``await``
        here would let the rest of the snapshot tear.  In particular the
        Marstek bindings are read with a single ``tuple(...)`` instead of
        under ``_marstek_lock`` — the copy is atomic without the loop
        yielding, and taking the lock would need an await.
        """
        cfg = self._config
        bindings = tuple(self._marstek_bindings.values())
        # A finished task lingers in the map until its done-callback runs.
        polling = {
            device_id
            for device_id, task in self._marstek_tasks_by_binding.items()
            if not task.done()
        }
        failing = self._marstek_get_values_failed
        return MqttInsightsSnapshot(
            connected=self.connected,
            broker=cfg.broker,
            port=cfg.port,
            tls=cfg.tls,
            base_topic=cfg.base_topic,
            ha_discovery=cfg.ha_discovery,
            ha_discovery_prefix=cfg.ha_discovery_prefix,
            hub_identifier=self._hub_identifier(),
            queue_depth=self._queue.qsize(),
            queue_dropped_total=self._queue_dropped,
            discovered_ct002_devices=self._discovered_count(CT002_DEVICE),
            discovered_ct002_consumers=self._discovered_count(CT002_CONSUMER),
            discovered_shelly_devices=self._discovered_count(SHELLY_DEVICE),
            discovered_shelly_batteries=self._discovered_count(SHELLY_BATTERY),
            discovered_powermeters=self._discovered_count(POWERMETER),
            powermeter_health_interval=cfg.powermeter_health_interval,
            marstek_enabled=cfg.marstek_mqtt_enabled,
            marstek_interval=cfg.marstek_mqtt_interval,
            marstek_bindings=tuple(
                MarstekBindingSnapshot(
                    device_id=binding.device_id,
                    ct_type=binding.ct_type,
                    mac=binding.mac,
                    ver_v=binding.ver_v,
                    wifi_rssi=binding.wifi_rssi,
                    poll_in_flight=binding.device_id in polling,
                    value_fetch_failing=binding.device_id in failing,
                )
                for binding in bindings
            ),
        )

    # Published to the same retained command topics Home Assistant uses, so a
    # dashboard write survives a reconnect instead of being reverted by the
    # retained value the broker redelivers.  HTTP handlers only — never the
    # snapshot path.

    async def publish_consumer_command(
        self, device_id: str, consumer_id: str, field: str, payload: str | float | bool
    ) -> None:
        """Publish a retained per-consumer command (scalar payload).

        Callers hand this native scalars as well as strings — the dashboard
        mirrors the JSON value it was given. The command topic is text, and
        the reader parses it (``_parse_bool`` lower-cases, so ``True``
        round-trips), so stringify here rather than at every call site.
        """
        await self._publish_command(
            consumer_command_topic(
                self._config.base_topic, device_id, consumer_id, field
            ),
            str(payload).encode(),
        )

    async def publish_device_command(
        self, device_id: str, payload: dict[str, Any]
    ) -> None:
        """Publish a retained device-level command (JSON object payload)."""
        await self._publish_command(
            device_command_topic(self._config.base_topic, device_id),
            json.dumps(payload).encode(),
        )

    async def _publish_command(self, topic: str, payload: bytes) -> None:
        client = self._client
        if client is None:
            raise RuntimeError("MQTT Insights is not connected")
        await client.publish(topic, payload=payload, qos=1, retain=True)

    def _put_nowait(self, evt: _Event) -> None:
        try:
            self._queue.put_nowait(evt)
        except asyncio.QueueFull:
            # Drop oldest to make room
            self._queue_dropped += 1
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(evt)

    def _client_args(self, tls_context: ssl.SSLContext | None) -> dict[str, Any]:
        """Connection arguments shared by the service loop and the parting
        "offline" publish, which reconnects after the loop has been cancelled."""
        cfg = self._config
        return {
            "hostname": cfg.broker,
            "port": cfg.port,
            "username": cfg.username,
            "password": cfg.password,
            "tls_context": tls_context,
        }

    async def _announce(self, client: aiomqtt.Client) -> None:
        """Publish presence and discovery, then subscribe, on every connect."""
        cfg = self._config
        # Clear discovery on (re)connect so we re-publish.
        self._discovered.clear()
        await client.publish(
            system_status_topic(cfg.base_topic), payload=b"online", qos=1, retain=True
        )

        # Top-level "AstraMeter" hub device that the per-meter devices link up
        # to via via_device. Uses ADDON_SLUG as the identifier on the HA add-on,
        # falling back to a stable base-topic-derived id so the grouping also
        # works in standalone/Docker. Republished on every reconnect.
        if cfg.ha_discovery:
            topic, payload = build_addon_device_discovery(
                cfg.base_topic, self._hub_identifier(), cfg.ha_discovery_prefix
            )
            await _publish_json(client, topic, payload)
            await self._publish_bridge(client, cfg)

        await client.subscribe(consumer_command_filter(cfg.base_topic))
        await client.subscribe(device_command_filter(cfg.base_topic))

        # Store the client so register_marstek() called while already connected
        # can live-subscribe, and so the dashboard write path has a connection
        # to publish on — both need it whether or not Marstek MQTT is enabled.
        # Then subscribe every registered binding's App topics.
        async with self._marstek_lock:
            self._client = client
            if cfg.marstek_mqtt_enabled:
                for binding in self._marstek_bindings.values():
                    for topic in app_topics_for(binding):
                        await client.subscribe(topic)

    def _session_loops(self, client: aiomqtt.Client) -> list[Any]:
        """The concurrent loops one connection runs until it drops."""
        cfg = self._config
        loops: list[Any] = [self._publish_loop(client), self._listen_commands(client)]
        if cfg.marstek_mqtt_enabled and cfg.marstek_mqtt_interval > 0:
            loops.append(self._marstek_broadcast_loop(client))
        if self._powermeters and cfg.powermeter_health_interval > 0:
            loops.append(self._powermeter_health_loop(client))
        return loops

    async def _serve_connection(self, client: aiomqtt.Client) -> None:
        """Announce ourselves, then run the session loops until one ends."""
        await self._announce(client)
        self._connected.set()
        try:
            await asyncio.gather(*self._session_loops(client))
        finally:
            async with self._marstek_lock:
                self._client = None
            await self._cancel_marstek_tasks()

    async def _run(self) -> None:
        cfg = self._config
        tls_context = ssl.create_default_context() if cfg.tls else None

        while True:
            try:
                async with aiomqtt.Client(
                    **self._client_args(tls_context),
                    keepalive=60,
                    will=aiomqtt.Will(
                        topic=system_status_topic(cfg.base_topic),
                        payload=b"offline",
                        qos=1,
                        retain=True,
                    ),
                ) as client:
                    logger.info(
                        "MQTT Insights connected to %s:%s", cfg.broker, cfg.port
                    )
                    await self._serve_connection(client)

            except asyncio.CancelledError:
                self._connected.clear()
                # Graceful shutdown: publish offline in a shielded scope
                # so the pending cancellation doesn't abort the publish.
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._publish_offline(tls_context))
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

    def _discovered_count(self, kind: str) -> int:
        return sum(1 for k, _ in self._discovered if k == kind)

    def _consumer_count(self) -> int:
        """Total downstream consumers/batteries currently known to AstraMeter."""
        return self._discovered_count(CT002_CONSUMER) + self._discovered_count(
            SHELLY_BATTERY
        )

    def _hub_identifier(self) -> str:
        """Identifier of the top-level AstraMeter hub device the per-meter
        devices link to via ``via_device``.

        On the HA add-on this is ``ADDON_SLUG`` (unchanged, so existing add-on
        devices keep grouping). Outside the add-on it falls back to a stable
        base-topic-derived id so standalone/Docker deployments group too.
        """
        cfg = self._config
        return cfg.addon_slug or f"astrameter_{_sanitize_id(cfg.base_topic)}"

    async def _publish_bridge(
        self, client: aiomqtt.Client, cfg: MqttInsightsConfig
    ) -> None:
        """Publish the retained hub-device state ({base}/bridge).

        No-op unless HA discovery is on (the hub device is always published
        then, with an ADDON_SLUG-or-fallback identifier).
        """
        if not cfg.ha_discovery:
            return
        payload = {
            "version": get_version(),
            "consumer_count": self._consumer_count(),
        }
        await _publish_json(client, bridge_topic(cfg.base_topic), payload)

    async def _publish_offline(self, tls_context: ssl.SSLContext | None) -> None:
        """Reconnect just long enough to retract the retained "online" status."""
        async with aiomqtt.Client(**self._client_args(tls_context)) as client:
            await client.publish(
                system_status_topic(self._config.base_topic),
                payload=b"offline",
                qos=1,
                retain=True,
            )

    async def _publish_loop(self, client: aiomqtt.Client) -> None:
        cfg = self._config
        base = cfg.base_topic

        while True:
            evt = await self._queue.get()

            did, eid = evt.device_id, evt.entity_id
            try:
                if evt.kind == "ct002":
                    await self._handle_ct002_event(client, base, cfg, evt)
                elif evt.kind == "ct002_remove":
                    await self._mark_offline(
                        client,
                        cfg,
                        ct002_consumer_topic(base, did, eid),
                        CT002_CONSUMER,
                        f"{did}/{eid}",
                    )
                elif evt.kind == "shelly":
                    await self._handle_shelly_event(client, base, cfg, evt)
                elif evt.kind == "shelly_remove":
                    await self._mark_offline(
                        client,
                        cfg,
                        shelly_battery_topic(base, did, eid),
                        SHELLY_BATTERY,
                        f"{did}/{eid}",
                    )
            except aiomqtt.MqttError:
                raise
            except Exception:
                logger.exception("Error publishing MQTT Insights event")

    @staticmethod
    async def _publish_discovery(
        client: aiomqtt.Client,
        topic: str,
        payload: dict,
        *,
        retire: bool = False,
    ) -> None:
        """Publish a device discovery payload, retiring dropped entities first.

        An entity that merely stops appearing in a discovery payload lives on in
        Home Assistant, so payloads that used to carry one (see
        ``RETIRED_COMPONENTS``) publish the retirement update first and the
        current payload right after — the retained message the broker keeps is
        then the current one.
        """
        if retire:
            await _publish_json(client, topic, build_retirement_payload(payload))
        await _publish_json(client, topic, payload)

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

        consumer_key = f"{did}/{cid}"
        state_topic = ct002_consumer_topic(base, did, cid)
        avail_topic = availability_topic(state_topic)

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
            "answer_interval": data.get("answer_interval"),
            "last_seen": data.get("last_seen", ""),
            "manual_target": data.get("manual_target"),
            "auto_target": data.get("auto_target", True),
            "distribution_weight": data.get("distribution_weight", 1.0),
            "efficiency_window_weight": data.get("efficiency_window_weight", 1.0),
            "min_dc_output": data.get("min_dc_output"),
        }

        await _publish_json(client, state_topic, consumer_state)
        await client.publish(avail_topic, payload=b"online", retain=True)

        device_status = {
            "smooth_target": data.get("smooth_target", 0),
            "active_control": data.get("active_control", False),
            "consumer_count": data.get("consumer_count", 0),
            "control_quality": data.get("control_quality", "idle"),
            # Null, not 0: the score is absent while the loop has nothing to
            # be scored on, and a 0 would read as "as bad as it gets".  The
            # same holds for the evidence behind the verdict, which travels
            # with it so an MQTT-only client can act on "off target" without
            # the dashboard.
            "control_quality_score": data.get("control_quality_score"),
            "control_quality_error_w": data.get("control_quality_error_w"),
            "control_quality_in_band_pct": data.get("control_quality_in_band_pct"),
            "control_quality_crossings_per_min": data.get(
                "control_quality_crossings_per_min"
            ),
            "control_quality_band_w": data.get("control_quality_band_w"),
        }
        await _publish_json(client, ct002_status_topic(base, did), device_status)

        efficiency_rotation = bool(data.get("efficiency_rotation", False))
        await self._discover_once(
            client,
            CT002_DEVICE,
            did,
            lambda: build_ct002_device_discovery(
                base,
                did,
                cfg.ha_discovery_prefix,
                addon_slug=self._hub_identifier(),
                efficiency_rotation=efficiency_rotation,
            ),
        )
        # Deliberately not ``_discover_once``: the bridge count has to go out
        # between marking the consumer discovered and publishing its discovery
        # payload, and that publish order is on the wire.  The Shelly path below
        # publishes the bridge after instead; both orders are equally fine for
        # Home Assistant, but neither is worth changing on a live install.
        if (CT002_CONSUMER, consumer_key) not in self._discovered and cfg.ha_discovery:
            self._discovered.add((CT002_CONSUMER, consumer_key))
            await self._publish_bridge(client, cfg)
            topic, payload = build_ct002_consumer_discovery(
                base,
                did,
                cid,
                cfg.ha_discovery_prefix,
                device_type=data.get("device_type", ""),
                efficiency_rotation=efficiency_rotation,
            )
            await self._publish_discovery(client, topic, payload, retire=True)

    async def _mark_offline(
        self,
        client: aiomqtt.Client,
        cfg: MqttInsightsConfig,
        state_topic: str,
        kind: str,
        key: str,
    ) -> None:
        """A battery went silent: flip its availability and forget its
        discovery so a return republishes it."""
        await client.publish(
            availability_topic(state_topic), payload=b"offline", retain=True
        )
        self._discovered.discard((kind, key))
        await self._publish_bridge(client, cfg)

    async def _discover_once(
        self,
        client: aiomqtt.Client,
        kind: str,
        key: str,
        build: Callable[[], tuple[str, dict]],
        *,
        retire: bool = False,
    ) -> bool:
        """Publish a discovery payload the first time *key* is seen."""
        if not self._config.ha_discovery or (kind, key) in self._discovered:
            return False
        self._discovered.add((kind, key))
        topic, payload = build()
        await self._publish_discovery(client, topic, payload, retire=retire)
        return True

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
        state_topic = shelly_battery_topic(base, did, ip_slug)
        avail_topic = availability_topic(state_topic)

        battery_state = {
            "grid_power": data.get("grid_power", {}),
            "active": data.get("active", True),
            "poll_interval": data.get("poll_interval"),
            "last_seen": data.get("last_seen", ""),
        }

        await _publish_json(client, state_topic, battery_state)
        await client.publish(avail_topic, payload=b"online", retain=True)

        device_status = {
            "battery_count": data.get("battery_count", 0),
        }
        await _publish_json(client, shelly_status_topic(base, did), device_status)

        await self._discover_once(
            client,
            SHELLY_DEVICE,
            did,
            lambda: build_shelly_device_discovery(
                base, did, cfg.ha_discovery_prefix, addon_slug=self._hub_identifier()
            ),
        )
        if await self._discover_once(
            client,
            SHELLY_BATTERY,
            battery_key,
            lambda: build_shelly_battery_discovery(
                base, did, ip_slug, cfg.ha_discovery_prefix
            ),
            retire=True,
        ):
            await self._publish_bridge(client, cfg)

    async def _listen_commands(self, client: aiomqtt.Client) -> None:
        base = self._config.base_topic

        async for message in client.messages:
            topic_str = str(message.topic)
            if topic_str.startswith("hame_energy/") or topic_str.startswith(
                "marstek_energy/"
            ):
                await self._handle_marstek_message(client, message)
                continue
            parsed = parse_command_topic(base, topic_str)
            if parsed is None:
                continue

            raw = message.payload
            try:
                payload_str = raw.decode() if isinstance(raw, bytes) else str(raw)
            except UnicodeDecodeError:
                logger.warning("Invalid command payload on %s", topic_str)
                continue

            if isinstance(parsed, MalformedCommandTopic):
                logger.warning("Malformed consumer command topic %s", topic_str)
                continue
            if isinstance(parsed, ConsumerCommandTopic):
                self._handle_consumer_field_command(
                    parsed.device_id, parsed.consumer_id, parsed.field, payload_str
                )
                continue
            # Device-level: JSON body.
            try:
                cmd = json.loads(payload_str)
            except json.JSONDecodeError:
                logger.warning("Invalid command payload on %s", topic_str)
                continue
            if not isinstance(cmd, dict):
                logger.warning("Command payload is not a JSON object on %s", topic_str)
                continue
            self._handle_device_command(parsed.device_id, cmd)

    def _handle_consumer_field_command(
        self, device_id: str, consumer_id: str, field: str, payload: str
    ) -> None:
        # An empty payload is how a retained command gets cleared — ignore it
        # rather than logging a spurious "invalid value" warning, and forget any
        # buffered value so it isn't replayed to a late-registering handler.
        if not payload.strip():
            self._forget_consumer_command(device_id, consumer_id, field)
            return

        # Only a known field with a valid value is remembered for replay, so a
        # malformed retained command is never handed to a device later.
        if self._apply_consumer_command(device_id, consumer_id, field, payload):
            self._pending_consumer_commands.setdefault(device_id, {})[
                (consumer_id, field)
            ] = payload

    def _apply_consumer_command(
        self, device_id: str, consumer_id: str, field: str, payload: str
    ) -> bool:
        """Apply one consumer command; True when the field is known and the
        value valid, whether or not the device has registered yet."""
        control = CONSUMER_CONTROLS_BY_FIELD.get(field)
        if control is None:
            logger.debug(
                "Unknown consumer command field %r for %s/%s",
                field,
                device_id,
                consumer_id,
            )
            return False
        try:
            value = control.parse(payload)
        except ValueError as exc:
            logger.warning(
                "Rejected command for %s/%s: %s (got %r)",
                device_id,
                consumer_id,
                exc,
                payload,
            )
            return False
        device = self._devices.get(device_id)
        if device is None:
            logger.debug(
                "No device %s registered yet for %s of consumer %s",
                device_id,
                field,
                consumer_id,
            )
            return True
        try:
            control.apply(device, consumer_id, value)
        except Exception:
            logger.exception(
                "Applying %s to %s/%s failed", field, device_id, consumer_id
            )
        return True

    def _forget_consumer_command(
        self, device_id: str, consumer_id: str, field: str
    ) -> None:
        pending = self._pending_consumer_commands.get(device_id)
        if not pending:
            return
        pending.pop((consumer_id, field), None)
        if not pending:
            self._pending_consumer_commands.pop(device_id, None)

    def _replay_consumer_commands(self, device_id: str) -> None:
        """Apply the retained commands that arrived before *device_id*
        registered (the normal order on an app restart)."""
        pending = self._pending_consumer_commands.get(device_id)
        for (consumer_id, name), payload in list((pending or {}).items()):
            self._handle_consumer_field_command(device_id, consumer_id, name, payload)

    def _handle_device_command(self, device_id: str, cmd: dict) -> None:
        device = self._devices.get(device_id)
        if device is None:
            logger.debug("No device %s registered for %r", device_id, cmd)
            return
        names = []
        if cmd.get("force_rotation") is True:
            names.append("force_rotation")
        if "active_control" in cmd:
            names.append("active_control")
        for name in names:
            try:
                apply_device_control(device, name, cmd.get(name))
            except ValueError as exc:
                logger.warning("Rejected command for %s: %s", device_id, exc)
            except Exception:
                logger.exception("Applying %s to %s failed", name, device_id)

    async def _powermeter_health_loop(self, client: aiomqtt.Client) -> None:
        """Publish a per-powermeter "Online" diagnostic sensor.

        Push powermeters answer ``stream_online()`` with no I/O; pull
        powermeters reuse the control loop's most recent read, or — when idle
        (no battery polling them) — get one bounded probe per cycle.
        """
        cfg = self._config
        base = cfg.base_topic
        interval = cfg.powermeter_health_interval
        while True:
            for pm in self._powermeters:
                name = pm.name
                if not name:
                    continue
                online, values = await self._powermeter_status(pm)
                await self._publish_powermeter_health(
                    client, base, cfg, name, online, values
                )
            await asyncio.sleep(interval)

    async def _powermeter_status(
        self, pm: Powermeter
    ) -> tuple[bool, list[float] | None]:
        """Return ``(online, latest_values)`` for *pm* with minimal I/O.

        A push meter's ``stream_online()`` answers with no I/O and its readings
        are cached; a pull meter reuses the control loop's most recent read when
        fresh, otherwise a single bounded probe serves both online and readings.
        """
        try:
            stream = pm.stream_online()
        except Exception:
            # A single meter's health hook must never tear down the gather
            # (which would force a full MQTT reconnect for every device).
            logger.exception(
                "Powermeter health: stream_online() failed for %s",
                pm.name or pm.__class__.__name__,
            )
            return False, None
        if stream is not None:
            # Push meter: readings are cached (no network I/O).
            return stream, await self._read_powermeter_values(pm)
        # Pull meter: reuse the control loop's read while it is fresh.  The
        # outermost wrapper is always the health tracker (``powermeter/base.py``)
        # — the isinstance is how the type checker is told, not a fallback.
        if isinstance(pm, HealthTrackingPowermeter):
            last_attempt = pm.last_attempt
            if (
                last_attempt is not None
                and (time.monotonic() - last_attempt) <= POWERMETER_IDLE_THRESHOLD
            ):
                return pm.last_outcome_ok, pm.last_values
        # Idle pull meter: one bounded probe serves both online and readings.
        values = await self._read_powermeter_values(pm)
        return bool(values), values

    async def _read_powermeter_values(self, pm: Powermeter) -> list[float] | None:
        try:
            return await asyncio.wait_for(
                pm.get_powermeter_watts(), timeout=POWERMETER_PROBE_TIMEOUT
            )
        except Exception:
            return None

    @staticmethod
    def _grid_power_payload(values: list[float] | None) -> dict[str, float | None]:
        vals = list(values) if values else []
        return {
            "l1": vals[0] if len(vals) > 0 else None,
            "l2": vals[1] if len(vals) > 1 else None,
            "l3": vals[2] if len(vals) > 2 else None,
            "total": sum(vals) if vals else None,
        }

    async def _publish_powermeter_health(
        self,
        client: aiomqtt.Client,
        base: str,
        cfg: MqttInsightsConfig,
        name: str,
        online: bool,
        values: list[float] | None,
    ) -> None:
        pm_id = _sanitize_id(name)
        state = {"online": online, "grid_power": self._grid_power_payload(values)}
        await _publish_json(client, powermeter_topic(base, pm_id), state)
        await self._discover_once(
            client,
            POWERMETER,
            pm_id,
            lambda: build_powermeter_device_discovery(
                base,
                pm_id,
                name,
                cfg.ha_discovery_prefix,
                addon_slug=self._hub_identifier(),
            ),
        )

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
        try:
            if poll.echo_cd == 4:
                slaves = ""
                if binding.get_cd4_slave_csv is not None:
                    slaves = binding.get_cd4_slave_csv()
                payload = build_cd4_response(slaves)
            else:
                watts = await binding.get_values()
                n_slaves = 0
                if binding.get_connected_slave_count is not None:
                    n_slaves = binding.get_connected_slave_count()
                payload = build_response(
                    binding, list(watts), poll=poll, connected_slave_count=n_slaves
                )
        except Exception:
            # Log the first failure only: hm2mqtt polls every few seconds.
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
                "Marstek MQTT: poll value fetch recovered for %s", binding.device_id
            )
            self._marstek_get_values_failed.discard(binding.device_id)

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
