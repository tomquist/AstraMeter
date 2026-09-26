"""`sensor: - platform: tibber_pulse` — see this package's docstring."""

from __future__ import annotations

import base64
import logging
import re

import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components import http_request, sensor
from esphome.const import (
    CONF_ID,
    CONF_PASSWORD,
    CONF_POWER,
    CONF_TIMEOUT,
    DEVICE_CLASS_POWER,
    PLATFORM_ESP32,
    PLATFORM_HOST,
    STATE_CLASS_MEASUREMENT,
    UNIT_WATT,
)

_LOGGER = logging.getLogger(__name__)

DEPENDENCIES = ["network"]
# Requests go through http_request; loading it here saves the user a block.
AUTO_LOAD = ["http_request"]

tibber_pulse_ns = cg.esphome_ns.namespace("tibber_pulse")
TibberPulseComponent = tibber_pulse_ns.class_(
    "TibberPulseComponent", cg.PollingComponent
)

CONF_HTTP_REQUEST_ID = "http_request_id"
CONF_HOST = "host"
CONF_USER = "user"
CONF_NODE_ID = "node_id"
CONF_POWER_L1 = "power_l1"
CONF_POWER_L2 = "power_l2"
CONF_POWER_L3 = "power_l3"
CONF_OBIS_POWER_CURRENT = "obis_power_current"
CONF_OBIS_POWER_L1 = "obis_power_l1"
CONF_OBIS_POWER_L2 = "obis_power_l2"
CONF_OBIS_POWER_L3 = "obis_power_l3"

# Mirrors DEFAULT_TIMEOUT_S in the Python source: the bridge's webserver
# regularly takes over a second to answer (#551).
DEFAULT_TIMEOUT = "5s"

_OBIS_SETTERS = {
    CONF_OBIS_POWER_CURRENT: "set_obis_total",
    CONF_OBIS_POWER_L1: "set_obis_l1",
    CONF_OBIS_POWER_L2: "set_obis_l2",
    CONF_OBIS_POWER_L3: "set_obis_l3",
}

_SENSOR_SETTERS = {
    CONF_POWER: "set_power_sensor",
    CONF_POWER_L1: "set_power_l1_sensor",
    CONF_POWER_L2: "set_power_l2_sensor",
    CONF_POWER_L3: "set_power_l3_sensor",
}

_OBIS_HEX_RE = re.compile(r"^[0-9a-fA-F]{12}$")
_OBIS_TEXT_RE = re.compile(
    r"^(\d{1,3})-(\d{1,3}):(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?:\*(\d{1,3}))?$"
)


def obis_code(value) -> list[int]:
    """An OBIS register as its six SML bytes.

    Takes the Python `[TIBBER_PULSE]` section's form — 12 hex digits, e.g.
    `0100100700ff` — or the `1-0:16.7.0` form ESPHome's own `sml` component
    uses, where a missing `*F` group means 255, as it does on the meter.
    """
    value = cv.string_strict(value).strip()
    if _OBIS_HEX_RE.match(value):
        return list(bytes.fromhex(value))
    match = _OBIS_TEXT_RE.match(value)
    if match is None:
        raise cv.Invalid(
            f"{value!r} is not an OBIS code: use 12 hex digits (0100100700ff) "
            "or the A-B:C.D.E form (1-0:16.7.0)"
        )
    groups = [int(g) if g is not None else 255 for g in match.groups()]
    if any(g > 255 for g in groups):
        raise cv.Invalid(f"{value!r}: every OBIS group must be 0-255")
    return groups


def node_id(value) -> str:
    """The Pulse's node id on the bridge (see http://<bridge>/nodes/)."""
    value = cv.string_strict(str(value) if isinstance(value, int) else value)
    if not re.fullmatch(r"[0-9A-Za-z_-]+", value):
        raise cv.Invalid(f"{value!r} is not a node id")
    return value


def _power_sensor_schema():
    return sensor.sensor_schema(
        unit_of_measurement=UNIT_WATT,
        accuracy_decimals=0,
        device_class=DEVICE_CLASS_POWER,
        state_class=STATE_CLASS_MEASUREMENT,
    )


def _require_a_sensor(config):
    if not any(key in config for key in _SENSOR_SETTERS):
        raise cv.Invalid(
            "tibber_pulse needs at least one of power, power_l1, power_l2, power_l3"
        )
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(TibberPulseComponent),
            cv.GenerateID(CONF_HTTP_REQUEST_ID): cv.use_id(
                http_request.HttpRequestComponent
            ),
            cv.Required(CONF_HOST): cv.string_strict,
            cv.Required(CONF_PASSWORD): cv.string_strict,
            cv.Optional(CONF_USER, default="admin"): cv.string_strict,
            cv.Optional(CONF_NODE_ID, default="1"): node_id,
            cv.Optional(
                CONF_TIMEOUT, default=DEFAULT_TIMEOUT
            ): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_OBIS_POWER_CURRENT): obis_code,
            cv.Optional(CONF_OBIS_POWER_L1): obis_code,
            cv.Optional(CONF_OBIS_POWER_L2): obis_code,
            cv.Optional(CONF_OBIS_POWER_L3): obis_code,
            cv.Optional(CONF_POWER): _power_sensor_schema(),
            cv.Optional(CONF_POWER_L1): _power_sensor_schema(),
            cv.Optional(CONF_POWER_L2): _power_sensor_schema(),
            cv.Optional(CONF_POWER_L3): _power_sensor_schema(),
        }
    ).extend(cv.polling_component_schema("2s")),
    # The request runs on a worker task (FreeRTOS on the ESP32, a thread on the
    # host test platform). ESP8266 has neither, and a blocking poll of this
    # slow bridge from the main loop is what the component exists to avoid.
    cv.only_on([PLATFORM_ESP32, PLATFORM_HOST]),
    _require_a_sensor,
)


def _final_validate(config):
    """Give the bridge at least `timeout` to answer.

    http_request has one timeout for every request on the device, and it is
    what bounds the wait for the bridge's slow answer. Rather than have a
    second number that cannot take effect, raise that one to ours when it is
    shorter — never lower it, since another user of http_request may need
    longer. Final-validation edits are what codegen reads (`fv.full_config`
    becomes `CORE.config`), and http_request generates after validation.
    """
    http_conf = fv.full_config.get().get("http_request")
    if not isinstance(http_conf, dict):
        return config
    ours = config[CONF_TIMEOUT]
    current = http_conf.get(CONF_TIMEOUT)
    if current is None or current < ours:
        _LOGGER.info(
            "tibber_pulse: raising http_request's timeout to %s so the slow "
            "Pulse Bridge has time to answer",
            ours,
        )
        http_conf[CONF_TIMEOUT] = ours
    return config


FINAL_VALIDATE_SCHEMA = _final_validate


def authorization_header(user: str, password: str) -> str:
    """The HTTP Basic `Authorization` value, built once here, not per request."""
    token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    http = await cg.get_variable(config[CONF_HTTP_REQUEST_ID])
    cg.add(var.set_http(http))
    cg.add(var.set_host(config[CONF_HOST]))
    cg.add(var.set_node_id(config[CONF_NODE_ID]))
    cg.add(var.set_user(config[CONF_USER]))
    cg.add(
        var.set_authorization(
            authorization_header(config[CONF_USER], config[CONF_PASSWORD])
        )
    )

    for key, setter in _OBIS_SETTERS.items():
        if key in config:
            code = cg.ArrayInitializer(*config[key])
            cg.add(getattr(var, setter)(code))

    for key, setter in _SENSOR_SETTERS.items():
        if key in config:
            sens = await sensor.new_sensor(config[key])
            cg.add(getattr(var, setter)(sens))
