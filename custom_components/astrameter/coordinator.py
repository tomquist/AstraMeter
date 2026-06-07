"""AstraMeterRuntime — owns the running emulated meter for one config entry.

Push model (not DataUpdateCoordinator): data arrives via UDP datagrams and HA
state-change callbacks on the event loop. One config entry == one emulated meter
(one CT002/CT003 *or* one Shelly).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from ipaddress import IPv4Network
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send

from astrameter.config.client_filter import ClientFilter
from astrameter.powermeter.base import Powermeter
from astrameter.powermeter.wrappers.apply import FilterOptions, apply_wrappers

from . import const
from .ha_powermeter import HAStatePowermeter

logger = logging.getLogger("astrameter")

# (powermeter, client_filter, wait_for_next_message)
_PowermeterEntry = tuple[Powermeter, ClientFilter, bool]


class AstraMeterRuntime:
    """Runs one emulated meter and fans events out to entities via dispatcher."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.device_type: str = entry.data[const.CONF_DEVICE_TYPE]
        self.device_id: str = entry.data.get(const.CONF_DEVICE_ID, entry.entry_id[:8])
        self.udp_port: int = const.udp_port_for(
            self.device_type, entry.data.get(const.CONF_UDP_PORT)
        )
        self._pm: Powermeter | None = None
        self._health_pm: Powermeter | None = None
        self._device: Any = None
        self._wait_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        # (device_id, consumer_id) -> latest event data (or None if removed)
        self.consumer_state: dict[tuple[str, str], dict[str, Any] | None] = {}
        self.known_consumers: set[tuple[str, str]] = set()
        self.options_snapshot: dict[str, Any] = dict(entry.options)
        self.last_grid_values: list[float] | None = None

    @property
    def device(self) -> Any:
        return self._device

    def grid_online(self) -> bool:
        """Whether the upstream grid source currently has usable values."""
        pm = self._health_pm
        if pm is None:
            return False
        return bool(pm.stream_online())

    def powermeter_state(self) -> dict[str, Any]:
        """Health-device payload: online flag + last per-phase grid power."""
        values = self.last_grid_values or []
        grid_power: dict[str, Any] = {
            "total": sum(values) if values else None,
            "l1": values[0] if len(values) > 0 else None,
            "l2": values[1] if len(values) > 1 else None,
            "l3": values[2] if len(values) > 2 else None,
        }
        return {"online": self.grid_online(), "grid_power": grid_power}

    # ── lifecycle ──────────────────────────────────────────────────────────

    async def async_start(self) -> None:
        pm = self._build_powermeter()
        self._health_pm = pm  # HAStatePowermeter exposes stream_online()
        pm = apply_wrappers(pm, self._filter_options())
        self._pm = pm
        powermeters: list[_PowermeterEntry] = [
            (pm, ClientFilter([IPv4Network("0.0.0.0/0")]), True)
        ]

        self._device = self._build_device(powermeters)
        self._device.event_listener = self._on_event

        await pm.start()
        try:
            await self._device.start()
        except OSError as err:
            await pm.stop()
            ir.async_create_issue(
                self.hass,
                const.DOMAIN,
                f"port_in_use_{self.entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="port_in_use",
                translation_placeholders={"port": str(self.udp_port)},
            )
            raise ConfigEntryNotReady(
                f"Could not bind UDP port {self.udp_port}: {err}"
            ) from err
        self._wait_task = self.entry.async_create_background_task(
            self.hass, self._device.wait(), name=f"astrameter_{self.entry.entry_id}"
        )

    async def async_stop(self) -> None:
        if self._wait_task is not None:
            self._wait_task.cancel()
            self._wait_task = None
        if self._device is not None:
            try:
                await self._device.stop()
            except Exception:
                logger.debug("AstraMeter: device stop failed", exc_info=True)
        if self._pm is not None:
            try:
                await self._pm.stop()
            except Exception:
                logger.debug("AstraMeter: powermeter stop failed", exc_info=True)

    def apply_filter_options(self) -> None:
        """Hot-swap the wrapper chain in place (filter-only option change)."""
        if self._health_pm is None or self._device is None:
            return
        self._pm = apply_wrappers(self._health_pm, self._filter_options())
        powermeters: list[_PowermeterEntry] = [
            (self._pm, ClientFilter([IPv4Network("0.0.0.0/0")]), True)
        ]
        self._device.before_send = self._make_before_send(powermeters)

    # ── construction helpers ───────────────────────────────────────────────

    def _build_powermeter(self) -> HAStatePowermeter:
        data = self.entry.data
        if data.get(const.CONF_PAIR_MODE):
            return HAStatePowermeter(
                self.hass,
                power_calculate=True,
                input_entities=data.get(const.CONF_INPUT_ENTITIES, []),
                output_entities=data.get(const.CONF_OUTPUT_ENTITIES, []),
            )
        return HAStatePowermeter(self.hass, data.get(const.CONF_GRID_ENTITIES, []))

    def _filter_options(self) -> FilterOptions:
        opts = self.entry.options
        o = FilterOptions()
        if const.CONF_POWER_OFFSET in opts or const.CONF_POWER_MULTIPLIER in opts:
            o.offsets = [float(opts.get(const.CONF_POWER_OFFSET, 0.0))]
            o.multipliers = [float(opts.get(const.CONF_POWER_MULTIPLIER, 1.0))]
        o.throttle_interval = float(opts.get(const.CONF_THROTTLE_INTERVAL, 0.0))
        o.hampel_window = int(opts.get(const.CONF_HAMPEL_WINDOW, 0))
        o.hampel_n_sigma = float(opts.get(const.CONF_HAMPEL_N_SIGMA, 3.0))
        o.hampel_min_threshold = float(opts.get(const.CONF_HAMPEL_MIN_THRESHOLD, 0.0))
        o.smooth_alpha = float(opts.get(const.CONF_SMOOTH_ALPHA, 0.0))
        o.max_smooth_step = float(opts.get(const.CONF_MAX_SMOOTH_STEP, 0.0))
        o.deadband = float(opts.get(const.CONF_DEADBAND, 0.0))
        o.pid_kp = float(opts.get(const.CONF_PID_KP, 0.0))
        o.pid_ki = float(opts.get(const.CONF_PID_KI, 0.0))
        o.pid_kd = float(opts.get(const.CONF_PID_KD, 0.0))
        o.pid_output_max = float(opts.get(const.CONF_PID_OUTPUT_MAX, 800.0))
        o.pid_mode = str(opts.get(const.CONF_PID_MODE, "bias"))
        o.health_name = self.device_id
        return o

    def _build_device(self, powermeters: list[_PowermeterEntry]) -> Any:
        if self.device_type in const.CT002_DEVICE_TYPES:
            from astrameter.ct002.ct002 import CT002

            opts = self.entry.options
            device = CT002(
                udp_port=self.udp_port,
                ct_type=self.entry.data.get(const.CONF_CT_TYPE, "HME-4"),
                ct_mac=self.entry.data.get(const.CONF_CT_MAC, ""),
                active_control=bool(opts.get(const.CONF_ACTIVE_CONTROL, True)),
                min_dc_output=float(opts.get(const.CONF_MIN_DC_OUTPUT, 0.0)),
                min_efficient_power=float(opts.get(const.CONF_MIN_EFFICIENT_POWER, 0)),
                efficiency_rotation_interval=int(
                    opts.get(const.CONF_EFFICIENCY_ROTATION_INTERVAL, 900)
                ),
                device_id=self.device_id,
            )
            device.before_send = self._make_before_send(powermeters)
            return device

        from astrameter.shelly.shelly import Shelly

        return Shelly(
            powermeters=powermeters,
            device_id=self.device_id,
            udp_port=self.udp_port,
        )

    def _make_before_send(self, powermeters: list[_PowermeterEntry]):
        """Reimplements main.read_ct_powermeter (2s wait cap, TimeoutError swallow).

        ``get_powermeter_watts`` exceptions are allowed to propagate so CT002's
        ``_call_before_send`` records a failure and ``_handle_request`` sends a
        ``[0,0,0]`` zero-delta hold instead of winding up on a stale reading.
        """

        async def before_send(addr, _fields=None, _consumer_id=None):
            pm: Powermeter | None = None
            wait_for_next = False
            for candidate, client_filter, wait_flag in powermeters:
                if client_filter.matches(addr[0]):
                    pm = candidate
                    wait_for_next = wait_flag
                    break
            if pm is None:
                return None
            if wait_for_next:
                with contextlib.suppress(TimeoutError):
                    await pm.wait_for_next_message(timeout=2)
            try:
                values = await pm.get_powermeter_watts()
            except Exception:
                # Grid source unavailable: let CT002 apply the [0,0,0] hold, but
                # refresh the health device so its "online" sensor flips off.
                self.last_grid_values = None
                self._dispatch_health()
                raise
            value1 = values[0] if len(values) > 0 else 0
            value2 = values[1] if len(values) > 1 else 0
            value3 = values[2] if len(values) > 2 else 0
            self.last_grid_values = [value1, value2, value3]
            self._dispatch_health()
            return [value1, value2, value3]

        return before_send

    def _dispatch_health(self) -> None:
        async_dispatcher_send(self.hass, const.signal_health(self.entry.entry_id))

    def call_setter(
        self, setter: str, value: Any = None, consumer_id: str | None = None
    ) -> None:
        """Map an entity setter id to the underlying CT002 method."""
        dev = self._device
        if dev is None:
            return
        if setter == "manual_target":
            dev.set_consumer_manual_target(consumer_id, float(value))
        elif setter == "distribution_weight":
            dev.set_consumer_distribution_weight(consumer_id, float(value))
        elif setter == "min_dc_output":
            dev.set_consumer_min_dc_output(consumer_id, float(value))
        elif setter == "auto_target":
            dev.set_consumer_auto_target(consumer_id, bool(value))
        elif setter == "active":
            dev.set_consumer_active(consumer_id, bool(value))
        elif setter == "force_rotation":
            dev.force_efficiency_rotation()

    # ── event fan-out ──────────────────────────────────────────────────────

    @callback
    def _on_event(self, device_id: str, consumer_id: str, data: dict[str, Any]) -> None:
        key = (device_id, consumer_id)
        if data.get("_removed"):
            self.consumer_state[key] = None
            async_dispatcher_send(
                self.hass, const.signal_update(self.entry.entry_id), key
            )
            return
        first_seen = key not in self.known_consumers
        self.consumer_state[key] = data
        if first_seen:
            self.known_consumers.add(key)
            async_dispatcher_send(
                self.hass, const.signal_new_consumer(self.entry.entry_id), key
            )
        async_dispatcher_send(self.hass, const.signal_update(self.entry.entry_id), key)
