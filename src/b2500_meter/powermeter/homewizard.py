import asyncio
import contextlib
import json
import logging
import os
import ssl

import aiohttp

from .base import Powermeter

# Stdlib logger: avoid importing b2500_meter.config (config_loader imports powermeter).
logger = logging.getLogger("b2500-meter")

# Certificate: https://api-documentation.homewizard.com/assets/files/homewizard-ca-cert-56d062ef8e71d1038f464ea905d42fc6.pem
# Docs: https://api-documentation.homewizard.com/docs/v2/authorization#https
CA_CERT_PATH = os.path.join(os.path.dirname(__file__), "homewizard_ca.pem")


class HomeWizardPowermeter(Powermeter):
    def __init__(
        self, ip: str, token: str, serial: str, verify_ssl: bool = True
    ) -> None:
        self.ip = ip
        self.token = token
        self.serial = serial
        self._verify_ssl = verify_ssl
        self.values: list[float] | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._message_event = asyncio.Event()

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
        self._message_event = asyncio.Event()
        self._session = aiohttp.ClientSession()
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self) -> None:
        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ws_task
            self._ws_task = None
        if self._session:
            await self._session.close()
            self._session = None

    async def _ws_loop(self) -> None:
        url = f"wss://{self.ip}/api/ws"
        ssl_context = self._build_ssl_context()
        server_hostname = f"appliance/p1dongle/{self.serial}"
        while True:
            try:
                assert self._session is not None
                async with self._session.ws_connect(
                    url, ssl=ssl_context, server_hostname=server_hostname
                ) as ws:
                    logger.info(f"HomeWizard WebSocket connected to {self.ip}")
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_message(ws, msg.data)
                        elif msg.type in (
                            aiohttp.WSMsgType.ERROR,
                            aiohttp.WSMsgType.CLOSE,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.CLOSED,
                        ):
                            break
                    logger.info("HomeWizard WebSocket closed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"HomeWizard WebSocket error: {e}")
            await asyncio.sleep(5)

    async def _handle_message(
        self, ws: aiohttp.ClientWebSocketResponse, raw: str
    ) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.error(f"HomeWizard: failed to decode message: {raw}")
            return

        if not isinstance(msg, dict):
            logger.error(f"HomeWizard: unexpected message format: {raw}")
            return

        msg_type = msg.get("type")
        if msg_type == "authorization_requested":
            await ws.send_json({"type": "authorization", "data": self.token})
        elif msg_type == "authorized":
            logger.info("HomeWizard: authorized, subscribing to measurements")
            await ws.send_json({"type": "subscribe", "data": "measurement"})
        elif msg_type == "measurement":
            data = msg.get("data")
            if isinstance(data, dict):
                self._handle_measurement(data)
        elif msg_type == "error":
            error_data = msg.get("data", {})
            logger.error(f"HomeWizard error: {error_data.get('message', msg)}")
        else:
            logger.debug(f"HomeWizard: unknown message type: {msg_type}")

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

        self.values = values
        self._message_event.set()

    async def get_powermeter_watts_async(self) -> list[float]:
        if self.values is not None:
            return list(self.values)
        raise ValueError("No value received from HomeWizard")

    async def wait_for_message_async(self, timeout: float = 5) -> None:
        try:
            await asyncio.wait_for(self._message_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for HomeWizard measurement") from None
