"""ESPHome external component: CT002/CT003 grid-meter emulator.

Ports `src/astrameter/ct002/` (Python) to a native ESPHome component. This
first iteration scaffolds the schema and codegen plumbing around the protocol
port (protocol.{h,cpp}). Subsequent commits will land the balancer, filter
wrappers, MQTT insights sub-block, and Marstek cloud registration sub-block;
their schema slots live below as TODO markers so the layout is stable.
"""

from __future__ import annotations

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import sensor
from esphome.const import CONF_ID

CODEOWNERS = ["@tomquist"]
DEPENDENCIES = ["sensor"]
AUTO_LOAD = ["socket"]
MULTI_CONF = False

ct002_ns = cg.esphome_ns.namespace("ct002")
CT002Component = ct002_ns.class_("CT002Component", cg.Component)

CONF_POWER_SENSOR_L1 = "power_sensor_l1"
CONF_POWER_SENSOR_L2 = "power_sensor_l2"
CONF_POWER_SENSOR_L3 = "power_sensor_l3"
CONF_CT_TYPE = "ct_type"
CONF_CT_MAC = "ct_mac"
CONF_WIFI_RSSI = "wifi_rssi"
CONF_UDP_PORT = "udp_port"
CONF_ACTIVE_CONTROL = "active_control"
CONF_MAX_SENSOR_AGE = "max_sensor_age"


def _validate_three_phase_sensors(config):
    """Enforce: l1 required; l2/l3 are both-or-neither.

    Single-phase use cases supply only l1; three-phase use cases must supply
    all three sensors. Permitting only l1+l2 or l1+l3 would silently feed an
    incomplete vector to the balancer.
    """
    has_l2 = CONF_POWER_SENSOR_L2 in config
    has_l3 = CONF_POWER_SENSOR_L3 in config
    if has_l2 != has_l3:
        raise cv.Invalid(
            f"{CONF_POWER_SENSOR_L2} and {CONF_POWER_SENSOR_L3} must both be set "
            f"or both be omitted (got l2={has_l2}, l3={has_l3})"
        )
    return config


def _validate_ct_mac(value: str) -> str:
    """Accept empty string (mirror-incoming) or a 12-hex-char MAC (no separators).

    Matches Python's CT_MAC semantics in src/astrameter/ct002/ct002.py.
    """
    if value == "":
        return value
    stripped = value.replace(":", "").replace("-", "").lower()
    if len(stripped) != 12 or any(c not in "0123456789abcdef" for c in stripped):
        raise cv.Invalid(
            f"{CONF_CT_MAC!r} must be empty or a 12-hex-char MAC address "
            f"(optionally separated by ':' or '-'); got {value!r}"
        )
    return stripped


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(CT002Component),
            cv.Required(CONF_POWER_SENSOR_L1): cv.use_id(sensor.Sensor),
            cv.Optional(CONF_POWER_SENSOR_L2): cv.use_id(sensor.Sensor),
            cv.Optional(CONF_POWER_SENSOR_L3): cv.use_id(sensor.Sensor),
            cv.Optional(CONF_CT_TYPE, default="HME-4"): cv.one_of(
                "HME-4", "HME-3", upper=True
            ),
            cv.Optional(CONF_CT_MAC, default=""): _validate_ct_mac,
            cv.Optional(CONF_WIFI_RSSI, default=-50): cv.int_range(min=-127, max=0),
            cv.Optional(CONF_UDP_PORT, default=12345): cv.port,
            cv.Optional(CONF_ACTIVE_CONTROL, default=True): cv.boolean,
            cv.Optional(
                CONF_MAX_SENSOR_AGE, default="30s"
            ): cv.positive_time_period_milliseconds,
        }
    ).extend(cv.COMPONENT_SCHEMA),
    _validate_three_phase_sensors,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    sensor_l1 = await cg.get_variable(config[CONF_POWER_SENSOR_L1])
    cg.add(var.set_power_sensor_l1(sensor_l1))
    if CONF_POWER_SENSOR_L2 in config:
        sensor_l2 = await cg.get_variable(config[CONF_POWER_SENSOR_L2])
        sensor_l3 = await cg.get_variable(config[CONF_POWER_SENSOR_L3])
        cg.add(var.set_power_sensor_l2(sensor_l2))
        cg.add(var.set_power_sensor_l3(sensor_l3))

    cg.add(var.set_ct_type(config[CONF_CT_TYPE]))
    cg.add(var.set_ct_mac(config[CONF_CT_MAC]))
    cg.add(var.set_wifi_rssi(config[CONF_WIFI_RSSI]))
    cg.add(var.set_udp_port(config[CONF_UDP_PORT]))
    cg.add(var.set_active_control(config[CONF_ACTIVE_CONTROL]))
    cg.add(var.set_max_sensor_age_ms(config[CONF_MAX_SENSOR_AGE].total_milliseconds))
