"""Marstek cloud registration + the app-MQTT responder, native to Home Assistant.

Two optional, coupled CT002/CT003 features (no effect for Shelly):

* ``async_register_managed_ct`` registers a managed fake CT in the user's
  Marstek cloud account (reusing ``astrameter.marstek_api``) so the battery can
  be paired with it in the Marstek app. It returns the MAC/version the cloud
  assigned.

* ``MarstekResponder`` answers the Marstek app's ``cd=1``/``cd=4`` poll requests
  on MQTT so Hame Relay can forward the data to the cloud. It reuses the pure
  wire-format helpers from ``astrameter.mqtt_insights.marstek_mqtt`` but speaks
  MQTT through **Home Assistant's own** broker (``homeassistant.components.mqtt``)
  rather than ``aiomqtt`` — no extra dependency and no second broker config.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from homeassistant.components import mqtt
from homeassistant.core import HomeAssistant, callback

from astrameter.mqtt_insights.marstek_mqtt import (
    MarstekMqttBinding,
    MarstekPollContext,
    app_topics_for,
    build_cd4_response,
    build_response,
    device_topics_for,
    parse_app_topic,
    parse_marstek_poll_payload,
)

if TYPE_CHECKING:
    from homeassistant.components.mqtt.models import ReceiveMessage

logger = logging.getLogger("astrameter")


async def async_register_managed_ct(
    hass: HomeAssistant,
    device_type: str,
    mailbox: str,
    password: str,
    base_url: str,
) -> tuple[str, int] | None:
    """Register (or find) the managed CT in Marstek cloud; return ``(mac, ver_v)``.

    Runs the blocking ``astrameter.marstek_api`` HTTP calls in the executor.
    Returns ``None`` (and logs) on any failure so setup can continue with the
    UDP meter alone.
    """
    from astrameter.marstek_api import (
        MarstekApiError,
        MarstekConfig,
        ensure_managed_fake_device,
        normalize_mac,
        ver_v_from_marstek_api_version,
    )

    cfg = MarstekConfig(base_url=base_url, mailbox=mailbox, password=password)
    try:
        created = await hass.async_add_executor_job(
            ensure_managed_fake_device, cfg, device_type
        )
    except MarstekApiError as err:
        logger.error("Marstek registration failed: %s", err)
        return None
    except Exception:
        logger.exception("Unexpected Marstek registration error")
        return None
    if not created:
        return None
    mac = normalize_mac(str(created.get("mac", "")))
    if not mac:
        return None
    return mac, ver_v_from_marstek_api_version(created.get("version"))


class MarstekResponder:
    """Subscribe to the Marstek app topics and answer poll requests over MQTT."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        ct_type: str,
        mac: str,
        ver_v: int,
        wifi_rssi: int,
        get_values: Callable[[], Awaitable[list[float]]],
        get_connected_slave_count: Callable[[], int],
        get_cd4_slave_csv: Callable[[], str],
    ) -> None:
        self.hass = hass
        self._binding = MarstekMqttBinding(
            device_id=mac,
            ct_type=ct_type,
            mac=mac,
            get_values=get_values,
            wifi_rssi=wifi_rssi,
            ver_v=ver_v,
            get_connected_slave_count=get_connected_slave_count,
            get_cd4_slave_csv=get_cd4_slave_csv,
        )
        self._unsubs: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None

    async def async_start(self) -> bool:
        """Subscribe to the app topics. Returns False if MQTT isn't available."""
        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            logger.info(
                "Marstek MQTT responder not started for %s: no MQTT integration "
                "configured in Home Assistant",
                self._binding.mac,
            )
            return False
        for topic in app_topics_for(self._binding):
            self._unsubs.append(
                await mqtt.async_subscribe(
                    self.hass, topic, self._on_message, qos=0, encoding=None
                )
            )
        logger.info(
            "Marstek MQTT responder active for %s (%s)",
            self._binding.mac,
            self._binding.ct_type,
        )
        return True

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            with contextlib.suppress(Exception):
                unsub()
        self._unsubs.clear()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        self._task = None

    @callback
    def _on_message(self, msg: ReceiveMessage) -> None:
        parsed = parse_app_topic(str(msg.topic))
        if parsed is None:
            return
        body = msg.payload if isinstance(msg.payload, bytes) else b""
        poll = parse_marstek_poll_payload(body)
        if poll is None:
            return
        # Suppress overlapping polls so a slow grid source can't queue work.
        if self._task is not None and not self._task.done():
            return
        self._task = self.hass.async_create_task(self._serve(poll))

    async def _serve(self, poll: MarstekPollContext) -> None:
        b = self._binding
        try:
            if poll.echo_cd == 4:
                payload = build_cd4_response(b.get_cd4_slave_csv())
            else:
                watts = await b.get_values()
                n_slaves = b.get_connected_slave_count()
                payload = build_response(
                    b, list(watts), poll=poll, connected_slave_count=n_slaves
                )
        except Exception:
            logger.debug("Marstek MQTT: poll handling failed", exc_info=True)
            return
        for reply_topic in device_topics_for(b):
            with contextlib.suppress(Exception):
                await mqtt.async_publish(
                    self.hass, reply_topic, payload, qos=0, retain=False, encoding=None
                )
