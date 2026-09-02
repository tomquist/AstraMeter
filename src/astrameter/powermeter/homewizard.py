import asyncio
import json
import logging
import os
import ssl
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import aiohttp

from .base import stream_fresh
from .ws_client import WS_HEARTBEAT_SECONDS, WebSocket, WebSocketPowermeter, cancel

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")

# Certificate: https://api-documentation.homewizard.com/assets/files/homewizard-ca-cert-56d062ef8e71d1038f464ea905d42fc6.pem
# Docs: https://api-documentation.homewizard.com/docs/v2/authorization#https
CA_CERT_PATH = os.path.join(os.path.dirname(__file__), "homewizard_ca.pem")

# Maximum age of the last-received measurement before ``get_powermeter_watts``
# considers the value stale and raises.  HomeWizard P1 dongles push
# measurements roughly once per second, so 30 s of silence is a very
# large safety margin.
DEFAULT_MAX_MEASUREMENT_AGE_SECONDS = 30.0

# Independent software watchdog: if no measurement has arrived within
# this many seconds, the ws loop force-closes and reconnects even when
# aiohttp's heartbeat hasn't tripped (e.g. the dongle is ACKing ping
# frames but has stopped sending measurement events).
WATCHDOG_TIMEOUT_SECONDS = 45.0


class HomeWizardPowermeter(WebSocketPowermeter):
    _TIMEOUT_MESSAGE = "Timeout waiting for HomeWizard measurement"
    _LOG_NAME = "HomeWizard"

    def __init__(
        self,
        ip: str,
        token: str,
        serial: str,
        verify_ssl: bool = True,
        *,
        max_measurement_age_seconds: float = DEFAULT_MAX_MEASUREMENT_AGE_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__()
        self.ip = ip
        self.token = token
        self.serial = serial
        self._verify_ssl = verify_ssl
        self._max_measurement_age_seconds = max(0.0, max_measurement_age_seconds)
        self._clock = clock or time.monotonic
        self.values: list[float] | None = None
        self._last_measurement_time: float | None = None
        # True only while measurements arrive as a *continuous* stream (each one
        # before the previous goes stale).  A broken P1 dongle still accepts the
        # WebSocket and replays a single cached value every time the watchdog
        # force-reconnects; that lone sample would otherwise reset the freshness
        # window and flap the "Online" sensor on/off.  See stream_online().
        self._stream_healthy = False
        # Set whenever we receive a new measurement; the read watchdog clears it
        # after checking staleness to re-arm the timer.
        self._fresh_measurement_event = asyncio.Event()

        if not verify_ssl:
            logger.warning(
                "HomeWizard: TLS certificate verification is disabled "
                "(VERIFY_SSL=False); use only on a trusted LAN"
            )

    def _build_ssl_context(self) -> ssl.SSLContext:
        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        if self._verify_ssl:
            ssl_context.load_verify_locations(CA_CERT_PATH)
            ssl_context.check_hostname = True
            ssl_context.verify_mode = ssl.CERT_REQUIRED
        else:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context

    async def start(self) -> None:
        if self._session:
            return
        self.values = None
        self._last_measurement_time = None
        self._stream_healthy = False
        self._message_event.clear()
        self._fresh_measurement_event.clear()
        await super().start()

    def _connect(
        self, session: aiohttp.ClientSession
    ) -> AbstractAsyncContextManager[WebSocket]:
        return session.ws_connect(
            f"wss://{self.ip}/api/ws",
            ssl=self._build_ssl_context(),
            server_hostname=f"appliance/p1dongle/{self.serial}",
            heartbeat=WS_HEARTBEAT_SECONDS,
        )

    async def _read(self, ws: WebSocket) -> None:
        # A watchdog alongside the reader force-closes the socket when no
        # measurement arrives, which the aiohttp heartbeat cannot catch: the
        # dongle's TCP keepalives keep answering while the measurement stream
        # has stalled a layer above them.
        watchdog = asyncio.create_task(self._measurement_watchdog(ws))
        try:
            await super()._read(ws)
        finally:
            await cancel(watchdog)

    async def _measurement_watchdog(self, ws: WebSocket) -> None:
        """Force-close *ws* when no measurement has arrived within
        :data:`WATCHDOG_TIMEOUT_SECONDS`.

        HomeWizard P1 dongles normally push a measurement every ~1 s. A dongle
        that stops streaming without closing the TCP connection would otherwise
        sit in the read loop forever.
        """
        while True:
            self._fresh_measurement_event.clear()
            try:
                await asyncio.wait_for(
                    self._fresh_measurement_event.wait(),
                    timeout=WATCHDOG_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "HomeWizard watchdog: no measurement for %.0fs, "
                    "force-closing WebSocket to trigger a reconnect",
                    WATCHDOG_TIMEOUT_SECONDS,
                )
                await ws.close()
                return

    async def _on_text(self, ws: WebSocket, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.error("HomeWizard: failed to decode message: %s", raw)
            return

        if not isinstance(msg, dict):
            logger.error("HomeWizard: unexpected message format: %s", raw)
            return

        msg_type = msg.get("type")
        if msg_type == "authorization_requested":
            await ws.send_json({"type": "authorization", "data": self.token})
        elif msg_type == "authorized":
            logger.info("HomeWizard: authorized, subscribing to measurements")
            await ws.send_json({"type": "subscribe", "data": "measurement"})
            self._connected = True
        elif msg_type == "measurement":
            data = msg.get("data")
            if isinstance(data, dict):
                self._handle_measurement(data)
        elif msg_type == "error":
            error_data = msg.get("data", {})
            logger.error("HomeWizard error: %s", error_data.get("message", msg))
        else:
            logger.debug("HomeWizard: unknown message type: %s", msg_type)

    def _handle_measurement(self, data: dict) -> None:
        if "power_l1_w" in data:
            values = [
                data["power_l1_w"],
                data.get("power_l2_w", 0),
                data.get("power_l3_w", 0),
            ]
        elif "power_w" in data:
            values = [data["power_w"]]
        else:
            return

        now = self._clock()
        max_age = self._max_measurement_age_seconds
        prev = self._last_measurement_time
        if max_age <= 0:
            # Staleness check disabled: treat every sample as a live stream.
            self._stream_healthy = True
        else:
            # Healthy only if this sample arrived before the previous went stale;
            # a lone sample after a gap (a broken dongle's replayed cache) is not a
            # live stream and must not flip stream_online() back on.
            self._stream_healthy = prev is not None and (now - prev) <= max_age

        self.values = values
        self._last_measurement_time = now
        self._message_event.set()
        self._fresh_measurement_event.set()

    def stream_online(self) -> bool | None:
        return (
            self._connected
            and self._stream_healthy
            and stream_fresh(
                self._last_measurement_time,
                self._max_measurement_age_seconds,
                self._clock,
            )
        )

    async def get_powermeter_watts(self) -> list[float]:
        last = self._last_measurement_time
        if self.values is None or last is None:
            raise ValueError("No value received from HomeWizard")
        max_age = self._max_measurement_age_seconds
        if not stream_fresh(last, max_age, self._clock):
            age = self._clock() - last
            raise ValueError(
                f"HomeWizard measurement is stale ({age:.1f}s old, max {max_age:.1f}s)"
            )
        return list(self.values)
