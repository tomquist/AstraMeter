import asyncio
import logging

import aioesphomeapi
from aioesphomeapi import EntityInfo, EntityState, SensorState
from aioesphomeapi.reconnect_logic import ReconnectLogic

from .base import PushPowermeter

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")


class ESPHomeNative(PushPowermeter):
    _TIMEOUT_MESSAGE = "Timeout waiting for ESPHome message"

    def __init__(
        self, address: str, port: str, api_key: str, object_id: str, client_info: str
    ):
        super().__init__()
        self.object_id = object_id
        self.address = address
        self.port = int(port)
        # address/port/password are positional in aioesphomeapi's APIClient
        # (older releases, e.g. the one pinned on Python 3.10, reject them as
        # keywords). Noise-encrypted devices don't use the password, so pass "".
        self.api = aioesphomeapi.APIClient(
            address,
            self.port,
            "",
            noise_psk=api_key,
            client_info=client_info,
            keepalive=5.0,  # Ping interval used to detect a dropped connection.
        )
        self.reconnect_logic = ReconnectLogic(
            client=self.api,
            on_connect=self.connect_callback,
            on_disconnect=self.disconnect_callback,
            on_connect_error=self.connect_error_callback,
        )
        self.last_value: float = 0
        self.entity_info: EntityInfo | None = None
        self.is_connected: bool = False
        # Stays set across messages; only a (re)connect clears it.
        self._any_message_event = asyncio.Event()
        logger.debug(
            f"Initialized ESPHomeNative Api: Connection: {address}:{port} ClientInfo: {client_info} ObjectId: {self.object_id}"
        )

    def reset_connection_state(self):
        self.is_connected = False
        self._any_message_event.clear()
        self._message_event.clear()
        self.entity_info = None

    async def start(self) -> None:
        await self.reconnect_logic.start()

    async def stop(self) -> None:
        await self.reconnect_logic.stop()
        await self.api.disconnect()
        self.reset_connection_state()

    async def connect_callback(self):
        self.is_connected = True
        logger.debug(
            f"Connected to {self.address}:{self.port}. Api version: {self.api.api_version}"
        )

        device_info = await self.api.device_info()
        logger.info(
            f"Connected to {device_info.name} (EspHome: {device_info.esphome_version})"
        )

        entity_infos, _ = await self.api.list_entities_services()

        for entity_info in entity_infos:
            if entity_info.object_id == self.object_id:
                self.entity_info = entity_info

        if self.entity_info is None:
            # Raising here would bubble up through ReconnectLogic's on_connect and
            # trigger an immediate reconnect + relist loop that never resolves the
            # misconfiguration. Stay connected instead and just log it clearly.
            logger.error(
                f"Cannot subscribe to objectId {self.object_id}. ObjectId is not provided by the device. Available objectIds are: {[e.object_id for e in entity_infos]}"
            )
            return

        logger.info(
            f"Subscribing to entity ObjectId: {self.entity_info.object_id} Name:{self.entity_info.name} Key:{self.entity_info.key}"
        )
        self.api.subscribe_states(self.change_callback)

    async def connect_error_callback(self, err: Exception):
        self.reset_connection_state()
        logger.error(f"Connection failed: {err}")

    async def disconnect_callback(self, expected_disconnect: bool):
        self.reset_connection_state()

        if expected_disconnect:
            logger.info("Expected disconnect occurred")
        else:
            logger.warning("Unexpected disconnect. Trying to reconnect")

    def change_callback(self, state: EntityState):
        if self.entity_info is None:
            return

        if state.key != self.entity_info.key:
            return

        if not isinstance(state, SensorState):
            logger.error(f"Subscribed EntityState {state} is not a SensorState")
            return

        # When the upstream sensor goes unavailable, aioesphomeapi delivers a
        # SensorState with missing_state=True and often NaN. Feeding that into
        # active control would corrupt the grid reading, so drop the update and
        # keep the last known-good value.
        if state.missing_state or state.state != state.state:
            logger.debug("Ignoring unavailable/NaN sensor state")
            return

        self.last_value = state.state
        self._message_event.set()
        self._any_message_event.set()
        logger.debug(f"Got new sensor state: {state.state}")

    async def get_powermeter_watts(self) -> list[float]:
        if self._any_message_event.is_set():
            return [self.last_value]
        return []

    def stream_online(self) -> bool | None:
        return self.is_connected

    async def wait_for_message(self, timeout: float = 5) -> None:
        await self._wait(self._any_message_event, timeout)
