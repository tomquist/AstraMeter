"""Config and options flow for AstraMeter."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from . import const

_DEVICE_TYPE_SELECTOR = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=list(const.ALL_DEVICE_TYPES),
        mode=selector.SelectSelectorMode.DROPDOWN,
        translation_key="device_type",
    )
)
# Only power sensors make sense as a grid source. Filtering by the `power`
# device class restricts the picker to measurement sensors reported in W.
_ENTITIES_SELECTOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
)


class AstraMeterConfigFlow(ConfigFlow, domain=const.DOMAIN):
    """Handle a config flow for AstraMeter (one entry per emulated meter)."""

    VERSION = 1

    def __init__(self) -> None:
        self._pending: dict[str, Any] = {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            device_type = user_input[const.CONF_DEVICE_TYPE]
            port = const.udp_port_for(device_type, user_input.get(const.CONF_UDP_PORT))
            self._pending = {
                const.CONF_DEVICE_TYPE: device_type,
                const.CONF_UDP_PORT: port,
                const.CONF_DEVICE_ID: f"{device_type}_{port}",
            }
            # Optional Marstek cloud credentials (CT002/CT003 only) for managed-CT
            # registration + the Hame Relay MQTT responder; ignored for Shelly.
            mailbox = (user_input.get(const.CONF_MARSTEK_MAILBOX) or "").strip()
            password = user_input.get(const.CONF_MARSTEK_PASSWORD) or ""
            if mailbox and password and device_type in const.CT002_DEVICE_TYPES:
                self._pending[const.CONF_MARSTEK_MAILBOX] = mailbox
                self._pending[const.CONF_MARSTEK_PASSWORD] = password
            if user_input.get(const.CONF_PAIR_MODE):
                return await self.async_step_pair()
            entities = user_input.get(const.CONF_GRID_ENTITIES, [])
            if not entities:
                errors["base"] = "no_grid_entities"
            else:
                await self.async_set_unique_id(f"{device_type}_{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"AstraMeter {device_type} ({port})",
                    data={
                        **self._pending,
                        const.CONF_PAIR_MODE: False,
                        const.CONF_GRID_ENTITIES: entities,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    const.CONF_DEVICE_TYPE, default=const.DEVICE_TYPE_CT002
                ): _DEVICE_TYPE_SELECTOR,
                vol.Optional(
                    const.CONF_UDP_PORT, default=const.DEFAULT_CT002_PORT
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=65535, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(const.CONF_GRID_ENTITIES): _ENTITIES_SELECTOR,
                vol.Optional(
                    const.CONF_PAIR_MODE, default=False
                ): selector.BooleanSelector(),
                vol.Optional(const.CONF_MARSTEK_MAILBOX): selector.TextSelector(),
                vol.Optional(const.CONF_MARSTEK_PASSWORD): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit the grid-power entity selection of an existing entry.

        Identity (device type + UDP port) is fixed; only the grid source changes.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            entities = user_input.get(const.CONF_GRID_ENTITIES, [])
            if not entities:
                errors["base"] = "no_grid_entities"
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        const.CONF_PAIR_MODE: False,
                        const.CONF_GRID_ENTITIES: entities,
                        const.CONF_INPUT_ENTITIES: [],
                        const.CONF_OUTPUT_ENTITIES: [],
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(
                    const.CONF_GRID_ENTITIES,
                    default=entry.data.get(const.CONF_GRID_ENTITIES, []),
                ): _ENTITIES_SELECTOR,
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    async def async_step_pair(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            inputs = user_input.get(const.CONF_INPUT_ENTITIES, [])
            outputs = user_input.get(const.CONF_OUTPUT_ENTITIES, [])
            if not inputs or len(inputs) != len(outputs):
                errors["base"] = "pair_mismatch"
            else:
                device_type = self._pending[const.CONF_DEVICE_TYPE]
                port = self._pending[const.CONF_UDP_PORT]
                await self.async_set_unique_id(f"{device_type}_{port}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"AstraMeter {device_type} ({port})",
                    data={
                        **self._pending,
                        const.CONF_PAIR_MODE: True,
                        const.CONF_INPUT_ENTITIES: inputs,
                        const.CONF_OUTPUT_ENTITIES: outputs,
                    },
                )

        schema = vol.Schema(
            {
                vol.Required(const.CONF_INPUT_ENTITIES): _ENTITIES_SELECTOR,
                vol.Required(const.CONF_OUTPUT_ENTITIES): _ENTITIES_SELECTOR,
            }
        )
        return self.async_show_form(step_id="pair", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return AstraMeterOptionsFlow()


class AstraMeterOptionsFlow(OptionsFlow):
    """Filter/balancer tuning knobs (stored in entry.options)."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        opts = self.config_entry.options

        if user_input is not None:
            # Merge over existing options so keys not shown in this form
            # (advanced filter/PID knobs, etc.) are preserved on save.
            return self.async_create_entry(title="", data={**opts, **user_input})

        def _num(**kw: Any) -> Any:
            return selector.NumberSelector(
                selector.NumberSelectorConfig(
                    mode=selector.NumberSelectorMode.BOX, **kw
                )
            )

        schema = vol.Schema(
            {
                vol.Optional(
                    const.CONF_ACTIVE_CONTROL,
                    default=opts.get(const.CONF_ACTIVE_CONTROL, True),
                ): selector.BooleanSelector(),
                vol.Optional(
                    const.CONF_THROTTLE_INTERVAL,
                    default=opts.get(const.CONF_THROTTLE_INTERVAL, 0.0),
                ): _num(min=0, step=0.1),
                vol.Optional(
                    const.CONF_SMOOTH_ALPHA,
                    default=opts.get(const.CONF_SMOOTH_ALPHA, 0.0),
                ): _num(min=0, max=1, step=0.01),
                vol.Optional(
                    const.CONF_DEADBAND,
                    default=opts.get(const.CONF_DEADBAND, 0.0),
                ): _num(min=0, step=1),
                vol.Optional(
                    const.CONF_MIN_DC_OUTPUT,
                    default=opts.get(const.CONF_MIN_DC_OUTPUT, 0.0),
                ): _num(min=0, max=1000, step=1),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
