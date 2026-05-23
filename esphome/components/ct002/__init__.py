"""ESPHome external component: CT002/CT003 grid-meter emulator.

Ports `src/astrameter/ct002/` (Python) to a native ESPHome component. Parent
schema accepts grid-power sensor IDs and the cross-phase filter pipeline
(Hampel/smoothing/deadband/PID). Sub-blocks `mqtt_insights:` and
`marstek_registration:` will land in subsequent commits.
"""

from __future__ import annotations

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import sensor
from esphome.const import CONF_ALPHA, CONF_ID, CONF_MODE

CODEOWNERS = ["@tomquist"]
DEPENDENCIES = ["sensor"]
AUTO_LOAD = ["socket"]
MULTI_CONF = False

ct002_ns = cg.esphome_ns.namespace("ct002")
CT002Component = ct002_ns.class_("CT002Component", cg.Component)
BalancerConfig = ct002_ns.struct("BalancerConfig")
PidMode = ct002_ns.enum("PidMode", is_class=True)
PID_MODES = {"bias": PidMode.BIAS, "replace": PidMode.REPLACE}

# Parent fields
CONF_POWER_SENSOR_L1 = "power_sensor_l1"
CONF_POWER_SENSOR_L2 = "power_sensor_l2"
CONF_POWER_SENSOR_L3 = "power_sensor_l3"
CONF_CT_TYPE = "ct_type"
CONF_CT_MAC = "ct_mac"
CONF_WIFI_RSSI = "wifi_rssi"
CONF_UDP_PORT = "udp_port"
CONF_ACTIVE_CONTROL = "active_control"
CONF_MAX_SENSOR_AGE = "max_sensor_age"
CONF_CONSUMER_TTL = "consumer_ttl"

# Filter sub-blocks
CONF_FILTERS = "filters"
CONF_HAMPEL = "hampel"
CONF_SMOOTHING = "smoothing"
CONF_DEADBAND = "deadband"
CONF_PID = "pid"
CONF_WINDOW = "window"
CONF_N_SIGMA = "n_sigma"
CONF_MIN_THRESHOLD = "min_threshold"
CONF_MAX_STEP = "max_step"
CONF_KP = "kp"
CONF_KI = "ki"
CONF_KD = "kd"
CONF_OUTPUT_MAX = "output_max"

# Balancer sub-block
CONF_BALANCER = "balancer"
CONF_FAIR_DISTRIBUTION = "fair_distribution"
CONF_BALANCE_GAIN = "balance_gain"
CONF_BALANCE_DEADBAND = "balance_deadband"
CONF_ERROR_BOOST_THRESHOLD = "error_boost_threshold"
CONF_ERROR_BOOST_MAX = "error_boost_max"
CONF_ERROR_REDUCE_THRESHOLD = "error_reduce_threshold"
CONF_MAX_CORRECTION_PER_STEP = "max_correction_per_step"
CONF_MAX_TARGET_STEP = "max_target_step"
CONF_MIN_EFFICIENT_POWER = "min_efficient_power"
CONF_PROBE_MIN_POWER = "probe_min_power"
CONF_EFFICIENCY_ROTATION_INTERVAL = "efficiency_rotation_interval"
CONF_EFFICIENCY_FADE_ALPHA = "efficiency_fade_alpha"
CONF_EFFICIENCY_SATURATION_THRESHOLD = "efficiency_saturation_threshold"

# Saturation tracker sub-block
CONF_SATURATION = "saturation"
CONF_ENABLED = "enabled"
CONF_DECAY_FACTOR = "decay_factor"
CONF_GRACE_SECONDS = "grace_seconds"
CONF_STALL_TIMEOUT_SECONDS = "stall_timeout_seconds"
CONF_MIN_TARGET = "min_target"


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


HAMPEL_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_WINDOW, default=7): cv.int_range(min=1, max=64),
        cv.Optional(CONF_N_SIGMA, default=3.0): cv.float_range(min=0.0),
        cv.Optional(CONF_MIN_THRESHOLD, default=50.0): cv.float_range(min=0.0),
    }
)

SMOOTHING_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_ALPHA): cv.float_range(min=0.0, max=1.0),
        cv.Optional(CONF_MAX_STEP, default=0.0): cv.float_range(min=0.0),
    }
)

DEADBAND_SCHEMA = cv.Schema({cv.Required(CONF_DEADBAND): cv.float_range(min=0.0)})

PID_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_KP, default=0.0): cv.float_,
        cv.Optional(CONF_KI, default=0.0): cv.float_,
        cv.Optional(CONF_KD, default=0.0): cv.float_,
        cv.Optional(CONF_OUTPUT_MAX, default=800.0): cv.float_range(min=0.0),
        cv.Optional(CONF_MODE, default="bias"): cv.enum(PID_MODES, lower=True),
    }
)

FILTERS_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_HAMPEL): HAMPEL_SCHEMA,
        cv.Optional(CONF_SMOOTHING): SMOOTHING_SCHEMA,
        cv.Optional(CONF_DEADBAND): DEADBAND_SCHEMA,
        cv.Optional(CONF_PID): PID_SCHEMA,
    }
)

BALANCER_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_FAIR_DISTRIBUTION, default=True): cv.boolean,
        cv.Optional(CONF_BALANCE_GAIN, default=0.2): cv.float_range(min=0.0, max=1.0),
        cv.Optional(CONF_BALANCE_DEADBAND, default=15.0): cv.float_range(min=0.0),
        cv.Optional(CONF_ERROR_BOOST_THRESHOLD, default=150.0): cv.float_range(min=0.0),
        cv.Optional(CONF_ERROR_BOOST_MAX, default=0.5): cv.float_range(min=0.0),
        cv.Optional(CONF_ERROR_REDUCE_THRESHOLD, default=20.0): cv.float_range(min=0.0),
        cv.Optional(CONF_MAX_CORRECTION_PER_STEP, default=80.0): cv.float_range(
            min=0.0
        ),
        cv.Optional(CONF_MAX_TARGET_STEP, default=0.0): cv.float_range(min=0.0),
        cv.Optional(CONF_MIN_EFFICIENT_POWER, default=0.0): cv.float_range(min=0.0),
        cv.Optional(CONF_PROBE_MIN_POWER, default=80.0): cv.float_range(min=0.0),
        cv.Optional(
            CONF_EFFICIENCY_ROTATION_INTERVAL, default="15min"
        ): cv.positive_time_period_seconds,
        cv.Optional(CONF_EFFICIENCY_FADE_ALPHA, default=0.15): cv.float_range(
            min=0.01, max=1.0
        ),
        cv.Optional(CONF_EFFICIENCY_SATURATION_THRESHOLD, default=0.4): cv.float_range(
            min=0.0, max=1.0
        ),
    }
)

SATURATION_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_ENABLED, default=True): cv.boolean,
        cv.Optional(CONF_ALPHA, default=0.15): cv.float_range(min=0.01, max=1.0),
        cv.Optional(CONF_MIN_TARGET, default=20.0): cv.float_range(min=1.0),
        cv.Optional(CONF_DECAY_FACTOR, default=0.995): cv.float_range(min=0.0, max=1.0),
        cv.Optional(CONF_GRACE_SECONDS, default="90s"): cv.positive_time_period_seconds,
        cv.Optional(
            CONF_STALL_TIMEOUT_SECONDS, default="60s"
        ): cv.positive_time_period_seconds,
    }
)

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
            # TTL after which a silent consumer is evicted, matching
            # Python's consumer_ttl default. Lower it for fleets with
            # short-lived bench-test batteries; raise it if your network
            # has long polling gaps.
            cv.Optional(
                CONF_CONSUMER_TTL, default="120s"
            ): cv.positive_time_period_seconds,
            cv.Optional(CONF_FILTERS): FILTERS_SCHEMA,
            cv.Optional(CONF_BALANCER): BALANCER_SCHEMA,
            cv.Optional(CONF_SATURATION): SATURATION_SCHEMA,
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
    cg.add(var.set_consumer_ttl_seconds(int(config[CONF_CONSUMER_TTL].total_seconds)))

    filters = config.get(CONF_FILTERS, {})
    if CONF_HAMPEL in filters:
        h = filters[CONF_HAMPEL]
        cg.add(
            var.enable_hampel(h[CONF_WINDOW], h[CONF_N_SIGMA], h[CONF_MIN_THRESHOLD])
        )
    if CONF_SMOOTHING in filters:
        s = filters[CONF_SMOOTHING]
        cg.add(var.enable_smoothing(s[CONF_ALPHA], s[CONF_MAX_STEP]))
    if CONF_DEADBAND in filters:
        d = filters[CONF_DEADBAND]
        cg.add(var.enable_deadband(d[CONF_DEADBAND]))
    if CONF_PID in filters:
        p = filters[CONF_PID]
        cg.add(
            var.enable_pid(
                p[CONF_KP], p[CONF_KI], p[CONF_KD], p[CONF_OUTPUT_MAX], p[CONF_MODE]
            )
        )

    bal = config.get(CONF_BALANCER, {})
    bcfg = cg.StructInitializer(
        BalancerConfig,
        ("fair_distribution", bal.get(CONF_FAIR_DISTRIBUTION, True)),
        ("balance_gain", bal.get(CONF_BALANCE_GAIN, 0.2)),
        ("balance_deadband", bal.get(CONF_BALANCE_DEADBAND, 15.0)),
        ("error_boost_threshold", bal.get(CONF_ERROR_BOOST_THRESHOLD, 150.0)),
        ("error_boost_max", bal.get(CONF_ERROR_BOOST_MAX, 0.5)),
        ("error_reduce_threshold", bal.get(CONF_ERROR_REDUCE_THRESHOLD, 20.0)),
        ("max_correction_per_step", bal.get(CONF_MAX_CORRECTION_PER_STEP, 80.0)),
        ("max_target_step", bal.get(CONF_MAX_TARGET_STEP, 0.0)),
        ("min_efficient_power", bal.get(CONF_MIN_EFFICIENT_POWER, 0.0)),
        ("probe_min_power", bal.get(CONF_PROBE_MIN_POWER, 80.0)),
        (
            "efficiency_rotation_interval",
            float(bal.get(CONF_EFFICIENCY_ROTATION_INTERVAL, 900)),
        ),
        ("efficiency_fade_alpha", bal.get(CONF_EFFICIENCY_FADE_ALPHA, 0.15)),
        (
            "efficiency_saturation_threshold",
            bal.get(CONF_EFFICIENCY_SATURATION_THRESHOLD, 0.4),
        ),
    )
    cg.add(var.set_balancer_config(bcfg))

    sat = config.get(CONF_SATURATION, {})
    cg.add(
        var.set_balancer_saturation(
            sat.get(CONF_ALPHA, 0.15),
            sat.get(CONF_MIN_TARGET, 20.0),
            sat.get(CONF_DECAY_FACTOR, 0.995),
            float(sat.get(CONF_GRACE_SECONDS, 90)),
            float(sat.get(CONF_STALL_TIMEOUT_SECONDS, 60)),
            sat.get(CONF_ENABLED, True),
        )
    )
