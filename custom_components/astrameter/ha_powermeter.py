"""In-process grid-power source backed by Home Assistant entity state.

Replicates the state-machine of ``astrameter.powermeter.homeassistant`` (numeric
parse, ``unavailable``/``unknown`` -> no value, readiness + message events, and
input/output pair subtraction) but reads from HA core state via
``async_track_state_change_event`` instead of a WebSocket. HA imports live only
here, so the ``astrameter`` package never depends on ``homeassistant``.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from astrameter.powermeter.base import Powermeter

logger = logging.getLogger("astrameter")


class HAStatePowermeter(Powermeter):
    """Grid power from one or more HA entity states (optionally in/out pairs)."""

    def __init__(
        self,
        hass: HomeAssistant,
        current_power_entities: list[str] | None = None,
        *,
        power_calculate: bool = False,
        input_entities: list[str] | None = None,
        output_entities: list[str] | None = None,
    ) -> None:
        self.hass = hass
        self.current_power_entities = list(current_power_entities or [])
        self.power_calculate = power_calculate
        self.input_entities = list(input_entities or [])
        self.output_entities = list(output_entities or [])

        if self.power_calculate and len(self.input_entities) != len(
            self.output_entities
        ):
            raise ValueError("input and output entity lists must be the same length")

        if self.power_calculate:
            tracked = self.input_entities + self.output_entities
        else:
            tracked = self.current_power_entities
        self._tracked_entities: list[str] = [e for e in tracked if e]
        self._entity_values: dict[str, float | None] = {
            e: None for e in self._tracked_entities
        }
        self._entities_ready = asyncio.Event()
        self._message_event = asyncio.Event()
        self._unsub: callable | None = None
        self._connected = False

    async def start(self) -> None:
        if self._unsub is not None:
            return
        # Seed the cache from current HA state, then subscribe to changes.
        for eid in self._tracked_entities:
            state = self.hass.states.get(eid)
            self._update_entity_value(eid, state.state if state else None)
        self._unsub = async_track_state_change_event(
            self.hass, self._tracked_entities, self._handle_state_event
        )
        self._connected = True

    async def stop(self) -> None:
        self._connected = False
        if self._unsub is not None:
            self._unsub()
            self._unsub = None

    @callback
    def _handle_state_event(self, event: Event[EventStateChangedData]) -> None:
        eid = event.data["entity_id"]
        new_state = event.data["new_state"]
        self._update_entity_value(eid, new_state.state if new_state else None)

    def _update_entity_value(self, entity_id: str, state_val: str | None) -> None:
        if state_val is None or state_val in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self._entity_values[entity_id] = None
            self._check_entities_ready()
            return
        try:
            self._entity_values[entity_id] = float(state_val)
        except (ValueError, TypeError):
            logger.warning(
                "AstraMeter: grid source %s state %r is not numeric",
                entity_id,
                state_val,
            )
            self._entity_values[entity_id] = None
        self._check_entities_ready()
        self._message_event.set()

    def _check_entities_ready(self) -> None:
        ready = bool(self._tracked_entities) and all(
            self._entity_values.get(e) is not None for e in self._tracked_entities
        )
        if ready:
            self._entities_ready.set()
        else:
            self._entities_ready.clear()

    def _get_entity_value(self, entity_id: str) -> float:
        val = self._entity_values.get(entity_id)
        if val is None:
            raise ValueError(f"grid source {entity_id} has no usable state")
        return val

    def stream_online(self) -> bool | None:
        return self._connected and self._entities_ready.is_set()

    async def get_powermeter_watts(self) -> list[float]:
        if not self.power_calculate:
            return [self._get_entity_value(e) for e in self.current_power_entities]
        results: list[float] = []
        for in_entity, out_entity in zip(
            self.input_entities, self.output_entities, strict=False
        ):
            results.append(
                self._get_entity_value(in_entity) - self._get_entity_value(out_entity)
            )
        return results

    async def wait_for_message(self, timeout: float = 5) -> None:
        try:
            await asyncio.wait_for(self._entities_ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for grid source state") from None

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        self._message_event.clear()
        try:
            await asyncio.wait_for(self._message_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError("Timeout waiting for grid source state") from None
