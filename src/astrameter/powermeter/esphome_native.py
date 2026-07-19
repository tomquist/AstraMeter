import aioesphomeapi

from .base import Powermeter
from astrameter.config.logger import logger
from aioesphomeapi import EntityState, EntityInfo, SensorState
from aioesphomeapi.reconnect_logic import ReconnectLogic



class ESPHomeNative(Powermeter):
    def __init__(self, address: str, port: str, apiKey: str, objectId: str, clientInfo: str):
        self.objectId = objectId
        self.api = aioesphomeapi.APIClient(
            address=address,
            port = int(port),
            noise_psk=apiKey,
            client_info=clientInfo,
            keepalive=5.0 # Timout for connect/reconnect and connection loss detection
            )
        self.reconnectLogic = ReconnectLogic(
            client= self.api,
            on_connect=self.connect_callback,
            on_disconnect= self.disconnect_callback,
            on_connect_error=self.connect_error_callback
        )
        self.newValues: list[float] = []
        self.lastValue: float = 0
        self.entityInfo: EntityInfo|None = None
        logger.debug(f"Initialized ESPHomeNative Api: Connection: {address}:{port} ClientInfo: {clientInfo} ObjectId: {id}")

    async def start(self) -> None:
        await self.reconnectLogic.start()

    async def connect_callback(self):
        logger.debug(f'Connected to {self.api.address}:{self.api.port}. Api version: {self.api.api_version}')

        device_info = await self.api.device_info()
        logger.info(f'Connected to {device_info.name} (EspHome: {device_info.esphome_version})')

        entityInfos, _  = await self.api.list_entities_services()
        
        for entityInfo in entityInfos:
            if entityInfo.object_id == self.objectId:
                self.entityInfo = entityInfo

        if self.entityInfo is None:
            logger.error(f'Cannot subscribe to objectId {self.objectId}. ObjectId is not provided by the device. Available objectIds are: {[e.object_id for e in entityInfo]}')
            raise AssertionError(f'ObjectId {self.objectId} not found')
        
        logger.info(f'Subscribing to entity ObjectId: {self.entityInfo.object_id} Name:{self.entityInfo.name} Key:{self.entityInfo.key}')
        self.api.subscribe_states(self.change_callback)

    async def connect_error_callback(self, err: Exception):
        logger.error(f'Connection failed: {err}')
        
    async def disconnect_callback(self, expected_disconnect: bool):
        self.entityInfo = None

        if expected_disconnect:
            logger.info('Excpected disconnect occurred')
        else:
            logger.warning('Unexpected disconnect. Trying to reconnect')


    def change_callback(self, state: EntityState):
        if self.entityInfo is None:
            return

        if state.key != self.entityInfo.key:
            return

        if not isinstance(state, SensorState):
            logger.error(f'Subscribed EntityState {state} is not an SensorState')
            raise AssertionError(f'Subscribed EntityState {state} is not an SensorState')

        self.newValues.append(state.state)
        self.lastValue = state.state
        logger.debug(f'Got new sensor state: {state.state}')

    async def stop(self) -> None:
        await self.reconnectLogic.stop()

    async def get_powermeter_watts(self) -> list[float]:
        if self.newValues:
            values = self.newValues.copy()
            self.newValues.clear()
            return values
        return [self.lastValue]
