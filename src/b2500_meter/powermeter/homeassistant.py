import asyncio
import contextlib
import json
import logging
from typing import Any

import aiohttp

from .base import Powermeter

# Stdlib logger: avoid importing b2500_meter.config (config_loader imports powermeter).
logger = logging.getLogger("b2500-meter")

# Home Assistant websocket subscribe_entities compressed state (homeassistant.const)
_HA_S = "s"
_HA_DIFF_ADD = "+"


class HomeAssistant(Powermeter):
    def __init__(
        self,
        ip: str,
        port: str,
        use_https: bool,
        access_token: str,
        current_power_entity: str | list[str],
        power_calculate: bool,
        power_input_alias: str | list[str],
        power_output_alias: str | list[str],
        path_prefix: str | None,
    ):
        self.ip = ip
        self.port = port
        self.use_https = use_https
        self.access_token = access_token
        self.current_power_entity = (
            [current_power_entity]
            if isinstance(current_power_entity, str)
            else current_power_entity
        )
        self.power_calculate = power_calculate
        self.power_input_alias = (
            [power_input_alias]
            if isinstance(power_input_alias, str)
            else power_input_alias
        )
        self.power_output_alias = (
            [power_output_alias]
            if isinstance(power_output_alias, str)
            else power_output_alias
        )
        self.path_prefix = path_prefix

        self._entity_values: dict[str, float | None] = {}
        self._tracked_entities = self._collect_entities()
        self._msg_id = 0
        self._subscribe_entities_id: int | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._entities_ready = asyncio.Event()

    def _collect_entities(self) -> set[str]:
        if self.power_calculate:
            entities = list(self.power_input_alias) + list(self.power_output_alias)
        else:
            entities = list(self.current_power_entity)
        return {e for e in entities if e}

    def _build_ws_url(self) -> str:
        scheme = "wss" if self.use_https else "ws"
        prefix = self.path_prefix or ""
        return f"{scheme}://{self.ip}:{self.port}{prefix}/api/websocket"

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def start(self) -> None:
        if self._session:
            return
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
        url = self._build_ws_url()
        while True:
            try:
                assert self._session is not None
                async with self._session.ws_connect(url, heartbeat=30) as ws:
                    logger.info(f"Home Assistant WebSocket connected to {self.ip}")
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
                    logger.info("Home Assistant WebSocket closed")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"Home Assistant WebSocket error: {e}")
            # Reset protocol state for reconnection; keep _entity_values
            # (stale values are preferable to ValueError during brief disconnect;
            # subscribe_entities re-sends initial states on reconnect)
            self._msg_id = 0
            self._subscribe_entities_id = None
            await asyncio.sleep(5)

    def _handle_compressed_entity_event(self, ev: dict[str, Any]) -> None:
        """Apply subscribe_entities payloads (initial + diffs)."""
        additions = ev.get("a")
        if isinstance(additions, dict):
            for eid, st in additions.items():
                if (
                    eid in self._tracked_entities
                    and isinstance(st, dict)
                    and _HA_S in st
                ):
                    self._update_entity_value(eid, st.get(_HA_S))
        changes = ev.get("c")
        if isinstance(changes, dict):
            for eid, diff in changes.items():
                if eid not in self._tracked_entities or not isinstance(diff, dict):
                    continue
                plus = diff.get(_HA_DIFF_ADD)
                if isinstance(plus, dict) and _HA_S in plus:
                    self._update_entity_value(eid, plus.get(_HA_S))
        removals = ev.get("r")
        if isinstance(removals, list):
            for eid in removals:
                if eid in self._tracked_entities:
                    self._update_entity_value(eid, None)

    async def _handle_message(
        self, ws: aiohttp.ClientWebSocketResponse, raw: str
    ) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.error(f"Home Assistant: failed to decode message: {raw}")
            return

        msg_type = msg.get("type")

        if msg_type == "auth_required":
            logger.debug("Home Assistant: auth required, sending token")
            await ws.send_json({"type": "auth", "access_token": self.access_token})
        elif msg_type == "auth_ok":
            logger.info("Home Assistant: authenticated")
            if not self._tracked_entities:
                logger.error(
                    "Home Assistant: no entity IDs configured for subscription"
                )
                return
            self._subscribe_entities_id = self._next_id()
            await ws.send_json(
                {
                    "id": self._subscribe_entities_id,
                    "type": "subscribe_entities",
                    "entity_ids": sorted(self._tracked_entities),
                }
            )
        elif msg_type == "auth_invalid":
            logger.error(f"Home Assistant auth failed: {msg.get('message', '')}")
        elif msg_type == "result":
            if msg.get("id") == self._subscribe_entities_id and not msg.get("success"):
                logger.error(
                    f"Home Assistant subscribe_entities failed: {msg.get('error')}"
                )
        elif msg_type == "event":
            ev = msg.get("event")
            if isinstance(ev, dict):
                self._handle_compressed_entity_event(ev)

    def _update_entity_value(self, entity_id: str, state_val: object) -> None:
        logger.debug(f"Home Assistant: update_entity_value: {entity_id}, {state_val}")
        if state_val is None:
            self._entity_values[entity_id] = None
            self._check_entities_ready()
            return
        try:
            value = float(state_val)  # type: ignore[arg-type]
            self._entity_values[entity_id] = value
        except (ValueError, TypeError):
            logger.warning(
                f"Home Assistant sensor {entity_id} state '{state_val}' is not numeric"
            )
            self._entity_values[entity_id] = None
        self._check_entities_ready()

    def _check_entities_ready(self) -> None:
        if all(self._entity_values.get(e) is not None for e in self._tracked_entities):
            self._entities_ready.set()
        else:
            self._entities_ready.clear()

    def _get_entity_value(self, entity_id: str) -> float:
        val = self._entity_values.get(entity_id)
        if val is None:
            raise ValueError(f"Home Assistant sensor {entity_id} has no state")
        return val

    async def get_powermeter_watts(self) -> list[float]:
        if not self.power_calculate:
            return [
                self._get_entity_value(entity) for entity in self.current_power_entity
            ]
        else:
            if len(self.power_input_alias) != len(self.power_output_alias):
                raise ValueError(
                    "Home Assistant power_input_alias and"
                    " power_output_alias lengths differ"
                )
            results = []
            for in_entity, out_entity in zip(
                self.power_input_alias, self.power_output_alias, strict=False
            ):
                power_in = self._get_entity_value(in_entity)
                power_out = self._get_entity_value(out_entity)
                results.append(power_in - power_out)
            return results

    async def wait_for_message(self, timeout: float = 5) -> None:
        try:
            await asyncio.wait_for(self._entities_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for Home Assistant state") from None
