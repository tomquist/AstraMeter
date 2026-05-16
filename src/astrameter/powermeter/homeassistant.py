import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import aiohttp

from .base import Powermeter

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")

# Home Assistant websocket subscribe_entities compressed state (homeassistant.const)
_HA_S = "s"
_HA_LU = "lu"
_HA_LC = "lc"
_HA_DIFF_ADD = "+"

# WebSocket heartbeat (seconds) — same rationale as HomeWizard.
WS_HEARTBEAT_SECONDS = 30.0

# An entity older than this is considered stale by the local push timer.
# Crossing this threshold triggers the REST fallback (see below), not an
# immediate error: HA's ``subscribe_entities`` only forwards
# ``state_changed`` events, so a sensor with a constant value (e.g. solar
# production on an unloaded phase) produces no pushes even when HA itself
# is up to date.
DEFAULT_MAX_STATE_AGE_SECONDS = 60.0

# Total wall-clock budget for the REST staleness fallback. When local push
# silence exceeds ``max_state_age_seconds`` for any tracked entity, we
# fan out parallel ``GET /api/states/{entity}`` requests bounded by this
# deadline; HA returns ``last_reported`` (mutated on every state write,
# including same-value reports), which we use as the authoritative
# freshness signal. Bounded so a battery's UDP request never stalls.
REST_REFRESH_TIMEOUT_SECONDS = 1.0


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
        *,
        max_state_age_seconds: float = DEFAULT_MAX_STATE_AGE_SECONDS,
        clock: Callable[[], float] | None = None,
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
        self._max_state_age_seconds = max(0.0, max_state_age_seconds)
        self._clock = clock or time.monotonic

        self._entity_values: dict[str, float | None] = {}
        # Per-entity timestamp of the most recent state update, used
        # for staleness detection (None = never received).
        self._entity_update_time: dict[str, float | None] = {}
        self._tracked_entities = self._collect_entities()
        self._msg_id = 0
        self._subscribe_entities_id: int | None = None
        self._session: aiohttp.ClientSession | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._entities_ready = asyncio.Event()
        self._message_event = asyncio.Event()

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

    def _build_rest_state_url(self, entity_id: str) -> str:
        scheme = "https" if self.use_https else "http"
        prefix = self.path_prefix or ""
        return f"{scheme}://{self.ip}:{self.port}{prefix}/api/states/{entity_id}"

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
                async with self._session.ws_connect(
                    url, heartbeat=WS_HEARTBEAT_SECONDS
                ) as ws:
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
                logger.error("Home Assistant WebSocket error: %s", e, exc_info=True)
            # Reset protocol state for reconnection; keep _entity_values
            # as a courtesy, but mark them all stale so the staleness
            # check in _get_entity_value falls back to REST (or raises)
            # until fresh state pushes arrive from the reconnect.
            # ``_entities_ready`` must also clear, otherwise
            # ``wait_for_message()`` would return immediately for any
            # caller relying on it as a readiness signal even though
            # every entity is effectively stale until the next
            # ``subscribe_entities`` snapshot.
            self._msg_id = 0
            self._subscribe_entities_id = None
            for eid in list(self._entity_update_time):
                self._entity_update_time[eid] = None
            self._entities_ready.clear()
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
                if not isinstance(plus, dict):
                    continue
                if _HA_S in plus:
                    self._update_entity_value(eid, plus.get(_HA_S))
                elif _HA_LU in plus or _HA_LC in plus:
                    # state_reported (value unchanged): HA omits ``s`` and
                    # sends only ``lu``. Treat as a keepalive so the
                    # staleness check does not fire on sensors whose value
                    # is legitimately constant (e.g. solar production at
                    # night, an unloaded phase).
                    self._mark_entity_alive(eid)
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
            self._entity_update_time[entity_id] = None
            self._check_entities_ready()
            return
        try:
            value = float(state_val)  # type: ignore[arg-type]
            self._entity_values[entity_id] = value
            self._entity_update_time[entity_id] = self._clock()
        except (ValueError, TypeError):
            logger.warning(
                f"Home Assistant sensor {entity_id} state '{state_val}' is not numeric"
            )
            self._entity_values[entity_id] = None
            self._entity_update_time[entity_id] = None
        self._check_entities_ready()
        self._message_event.set()

    def _mark_entity_alive(self, entity_id: str) -> None:
        if self._entity_values.get(entity_id) is None:
            return
        self._entity_update_time[entity_id] = self._clock()
        self._message_event.set()

    def _check_entities_ready(self) -> None:
        ready = all(
            self._entity_values.get(e) is not None
            and self._entity_update_time.get(e) is not None
            for e in self._tracked_entities
        )
        if ready:
            self._entities_ready.set()
        else:
            self._entities_ready.clear()

    def _locally_stale_entities(self) -> list[str]:
        if self._max_state_age_seconds <= 0:
            return []
        now = self._clock()
        stale: list[str] = []
        for eid in self._tracked_entities:
            if self._entity_values.get(eid) is None:
                stale.append(eid)
                continue
            last = self._entity_update_time.get(eid)
            if last is None or (now - last) > self._max_state_age_seconds:
                stale.append(eid)
        return stale

    async def _refresh_stale_via_rest(
        self, timeout: float = REST_REFRESH_TIMEOUT_SECONDS
    ) -> None:
        """REST-poll any entity whose local push timer has crossed the
        staleness threshold, bounded by ``timeout`` total wall-clock.

        ``subscribe_entities`` only forwards ``state_changed``; sensors with
        a constant value (e.g. solar production on an unloaded phase) never
        push, so the per-entity timer is not a reliable freshness signal.
        ``GET /api/states/{eid}`` returns HA's ``last_reported``, which is
        mutated on every state write — including same-value reports — and
        is the authoritative source of truth.
        """
        if self._session is None:
            return
        stale = self._locally_stale_entities()
        if not stale:
            return
        # Whatever finishes in-budget is already applied; anything still
        # stale after the timeout will be caught by ``_get_entity_value``.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                asyncio.gather(
                    *(self._fetch_rest_state(eid) for eid in stale),
                    return_exceptions=True,
                ),
                timeout=timeout,
            )

    async def _fetch_rest_state(self, entity_id: str) -> None:
        assert self._session is not None
        # Snapshot the local update time so we can detect a concurrent
        # websocket push (or another in-flight REST refresh) and avoid
        # clobbering newer data with a potentially-older REST response.
        pre_update = self._entity_update_time.get(entity_id)
        url = self._build_rest_state_url(entity_id)
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            async with self._session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    logger.debug(
                        "Home Assistant REST refresh for %s: HTTP %s",
                        entity_id,
                        resp.status,
                    )
                    return
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.debug(
                "Home Assistant REST refresh for %s failed: %s", entity_id, exc
            )
            return
        if self._entity_update_time.get(entity_id) != pre_update:
            return
        if isinstance(data, dict):
            self._apply_rest_state(entity_id, data)

    def _apply_rest_state(self, entity_id: str, data: dict[str, Any]) -> None:
        state_val = data.get("state")
        if state_val in (None, "unknown", "unavailable"):
            return
        try:
            value = float(state_val)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return
        # Trust HA's ``last_reported`` (mutated on every state write).
        # If HA itself hasn't seen an update within the staleness window,
        # don't refresh local cache — let the staleness check raise.
        if self._max_state_age_seconds > 0:
            reported_iso = data.get("last_reported") or data.get("last_updated")
            if not isinstance(reported_iso, str):
                return
            try:
                reported_dt = datetime.fromisoformat(reported_iso)
            except ValueError:
                return
            if reported_dt.tzinfo is None:
                reported_dt = reported_dt.replace(tzinfo=timezone.utc)
            ha_age = (datetime.now(timezone.utc) - reported_dt).total_seconds()
            if ha_age > self._max_state_age_seconds:
                return
        self._entity_values[entity_id] = value
        self._entity_update_time[entity_id] = self._clock()
        self._check_entities_ready()
        self._message_event.set()

    def _get_entity_value(self, entity_id: str) -> float:
        val = self._entity_values.get(entity_id)
        if val is None:
            raise ValueError(f"Home Assistant sensor {entity_id} has no state")
        if self._max_state_age_seconds > 0:
            last = self._entity_update_time.get(entity_id)
            if last is None:
                raise ValueError(
                    f"Home Assistant sensor {entity_id} has no update timestamp"
                )
            age = self._clock() - last
            if age > self._max_state_age_seconds:
                raise ValueError(
                    f"Home Assistant sensor {entity_id} is stale "
                    f"({age:.1f}s old, max {self._max_state_age_seconds:.1f}s)"
                )
        return val

    async def get_powermeter_watts(self) -> list[float]:
        await self._refresh_stale_via_rest()
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

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        self._message_event.clear()
        try:
            await asyncio.wait_for(self._message_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for Home Assistant state") from None
