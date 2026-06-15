"""ESPHome external component: CT002/CT003 grid-meter emulator.

Ports `src/astrameter/ct002/` (Python) to a native ESPHome component. Parent
schema accepts grid-power sensor IDs and the cross-phase filter pipeline
(Hampel/smoothing/deadband/PID). Optional sub-blocks under the same
`ct002:` key:

* `mqtt_insights:` — publish Home Assistant Device Discovery + answer
  Marstek-app polls on the local broker. Requires an upstream `mqtt:`
  block in YAML.
* `marstek_registration:` — register a managed CT002/CT003 with the
  Marstek cloud on first boot; persist the MAC via ESPPreferences;
  apply it to this `ct002:` so UDP responses + MQTT topics use the
  cloud-side identity. Requires an upstream `http_request:` block.
"""

from __future__ import annotations

import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import http_request, sensor
from esphome.const import (
    CONF_ALPHA,
    CONF_ID,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_TIMEZONE,
)

CODEOWNERS = ["@tomquist"]
DEPENDENCIES = ["sensor"]
# json / md5 are auto-loaded so users don't need to add empty `json:` or
# `md5:` blocks to enable the sub-block infrastructure (they're cheap and
# only the sub-blocks ever reference them). mqtt / http_request have
# user-facing config of their own (broker, timeout, etc.) so we enforce
# user-declared blocks via `cv.requires_component` on the sub-schema
# instead of auto-loading them.
AUTO_LOAD = ["socket", "json", "md5"]
MULTI_CONF = False

ct002_ns = cg.esphome_ns.namespace("ct002")
CT002Component = ct002_ns.class_("CT002Component", cg.Component)
BalancerConfig = ct002_ns.struct("BalancerConfig")
PidMode = ct002_ns.enum("PidMode", is_class=True)
PID_MODES = {"bias": PidMode.BIAS, "replace": PidMode.REPLACE}

# Sub-component classes. Both are top-level ESPHome Components in the
# generated app — the YAML nesting under ct002: is the user-visible
# affordance, but each sub-block produces its own Application-tracked
# Component so ESPHome's setup_priority / loop scheduling work normally.
mqtt_insights_ns = ct002_ns.namespace("mqtt_insights")
MqttInsightsComponent = mqtt_insights_ns.class_("MqttInsightsComponent", cg.Component)
marstek_registration_ns = ct002_ns.namespace("marstek_registration")
MarstekRegistrationComponent = marstek_registration_ns.class_(
    "MarstekRegistrationComponent", cg.Component
)

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
CONF_DEDUPE_WINDOW = "dedupe_window"
# Test-only: enabling this compiles in a UDP control channel (grid
# injection + mock clock) used by the host-platform e2e suite. Absent in
# any real config; never document it as a user knob.
CONF_TEST_CONTROL_PORT = "test_control_port"

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
CONF_PACE_BASE_STEP = "pace_base_step"
CONF_PACE_MAX_STEP = "pace_max_step"
CONF_OSC_DAMP_MAX = "osc_damp_max"
CONF_OSC_DAMP_ALPHA = "osc_damp_alpha"
CONF_OSC_DAMP_DECAY = "osc_damp_decay"
CONF_OSC_DAMP_THRESHOLD = "osc_damp_threshold"
CONF_MIN_EFFICIENT_POWER = "min_efficient_power"
CONF_PROBE_MIN_POWER = "probe_min_power"
CONF_EFFICIENCY_ROTATION_INTERVAL = "efficiency_rotation_interval"
CONF_EFFICIENCY_FADE_ALPHA = "efficiency_fade_alpha"
CONF_EFFICIENCY_SATURATION_THRESHOLD = "efficiency_saturation_threshold"
CONF_MIN_DC_OUTPUT = "min_dc_output"
CONF_GRID_PREDICT_TRUST = "grid_predict_trust"
CONF_CONCENTRATE_DEADBAND = "concentrate_deadband"

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
        cv.Optional(CONF_BALANCE_DEADBAND, default=25.0): cv.float_range(min=0.0),
        cv.Optional(CONF_ERROR_BOOST_THRESHOLD, default=150.0): cv.float_range(min=0.0),
        cv.Optional(CONF_ERROR_BOOST_MAX, default=0.5): cv.float_range(min=0.0),
        cv.Optional(CONF_ERROR_REDUCE_THRESHOLD, default=20.0): cv.float_range(min=0.0),
        cv.Optional(CONF_MAX_CORRECTION_PER_STEP, default=80.0): cv.float_range(
            min=0.0
        ),
        cv.Optional(CONF_MAX_TARGET_STEP, default=0.0): cv.float_range(min=0.0),
        cv.Optional(CONF_PACE_BASE_STEP, default=30.0): cv.float_range(min=0.0),
        cv.Optional(CONF_PACE_MAX_STEP, default=100.0): cv.float_range(min=0.0),
        cv.Optional(CONF_OSC_DAMP_MAX, default=0.95): cv.float_range(min=0.0, max=1.0),
        cv.Optional(CONF_OSC_DAMP_ALPHA, default=0.3): cv.float_range(min=0.0, max=1.0),
        cv.Optional(CONF_OSC_DAMP_DECAY, default=0.05): cv.float_range(
            min=0.0, max=1.0
        ),
        cv.Optional(CONF_OSC_DAMP_THRESHOLD, default=300.0): cv.float_range(min=0.0),
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
        cv.Optional(CONF_MIN_DC_OUTPUT, default=0.0): cv.float_range(min=0.0),
        cv.Optional(CONF_GRID_PREDICT_TRUST, default=0.5): cv.float_range(
            min=0.0, max=1.0
        ),
        cv.Optional(CONF_CONCENTRATE_DEADBAND, default=60.0): cv.float_range(min=0.0),
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

# ────────────────────────────────────────────────────────────────────────
# Sub-block: mqtt_insights
# ────────────────────────────────────────────────────────────────────────

CONF_MQTT_INSIGHTS = "mqtt_insights"
CONF_BASE_TOPIC = "base_topic"
CONF_HA_DISCOVERY = "ha_discovery"
CONF_HA_DISCOVERY_PREFIX = "ha_discovery_prefix"
CONF_DEVICE_ID = "device_id"
CONF_MARSTEK_MQTT_ENABLED = "marstek_mqtt_enabled"
CONF_MARSTEK_MQTT_INTERVAL = "marstek_mqtt_interval"

# Fallback `device_id:` when the sub-block leaves it blank. Matches the Python
# add-on's default (see main.py) so both stacks publish the same HA discovery
# node (`astrameter_ct002_device-1`). Keep in sync with the C++ member default
# in mqtt_insights.h.
DEFAULT_MQTT_INSIGHTS_DEVICE_ID = "device-1"


def _resolve_mqtt_insights_device_id(device_id_opt: str) -> str:
    """Resolve the configured `device_id:` to the value handed to firmware.

    A blank/omitted value falls back to ``DEFAULT_MQTT_INSIGHTS_DEVICE_ID``.
    """
    return device_id_opt or DEFAULT_MQTT_INSIGHTS_DEVICE_ID


MQTT_INSIGHTS_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(MqttInsightsComponent),
            cv.Optional(CONF_BASE_TOPIC, default="astrameter"): cv.string_strict,
            # device_id defaults to DEFAULT_MQTT_INSIGHTS_DEVICE_ID at
            # to_code time when left blank (see _resolve_mqtt_insights_device_id),
            # matching the Python add-on so both stacks publish the same HA
            # discovery node_id.
            cv.Optional(CONF_DEVICE_ID, default=""): cv.string,
            cv.Optional(CONF_HA_DISCOVERY, default=True): cv.boolean,
            cv.Optional(
                CONF_HA_DISCOVERY_PREFIX, default="homeassistant"
            ): cv.string_strict,
            cv.Optional(CONF_MARSTEK_MQTT_ENABLED, default=True): cv.boolean,
            cv.Optional(
                CONF_MARSTEK_MQTT_INTERVAL, default="300s"
            ): cv.positive_time_period_milliseconds,
        }
    ),
    # Require an mqtt: block in the user's YAML — this sub-block talks to
    # whatever broker `mqtt:` is configured against and doesn't carry its
    # own credentials.
    cv.requires_component("mqtt"),
)


# ────────────────────────────────────────────────────────────────────────
# Sub-block: marstek_registration
# ────────────────────────────────────────────────────────────────────────

CONF_MARSTEK_REGISTRATION = "marstek_registration"
CONF_HTTP_REQUEST_ID = "http_request_id"
CONF_BASE_URL = "base_url"
CONF_MAILBOX = "mailbox"
CONF_DEVICE_TYPE = "device_type"
CONF_RETRY_INTERVAL = "retry_interval"
CONF_FORCE_REREGISTER = "force_reregister"

DEVICE_TYPES = ("ct002", "ct003")

MARSTEK_REGISTRATION_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(MarstekRegistrationComponent),
            cv.GenerateID(CONF_HTTP_REQUEST_ID): cv.use_id(
                http_request.HttpRequestComponent
            ),
            cv.Required(CONF_BASE_URL): cv.url,
            cv.Required(CONF_MAILBOX): cv.string_strict,
            cv.Required(CONF_PASSWORD): cv.string_strict,
            cv.Optional(CONF_TIMEZONE, default="Europe/Berlin"): cv.string_strict,
            cv.Optional(CONF_DEVICE_TYPE, default="ct002"): cv.one_of(
                *DEVICE_TYPES, lower=True
            ),
            cv.Optional(
                CONF_RETRY_INTERVAL, default="60s"
            ): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_FORCE_REREGISTER, default=False): cv.boolean,
        }
    ),
    # Cloud registration is HTTPS-only — http_request must be configured
    # by the user (it has its own timeout / verify_ssl knobs).
    cv.requires_component("http_request"),
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
            # Fixed TTL after which a silent consumer is evicted. Unset
            # (default) = adaptive eviction (~2 missed poll cycles per
            # consumer, like the real CT), matching Python's consumer_ttl
            # default. Set a fixed value if your network has long polling
            # gaps.
            cv.Optional(CONF_CONSUMER_TTL): cv.positive_time_period_seconds,
            # Drop repeat polls from the same battery within this window.
            # 0 (default) disables dedup, matching Python's
            # dedupe_time_window=0.0. Useful on noisy networks where a
            # battery retransmits the same poll.
            cv.Optional(
                CONF_DEDUPE_WINDOW, default="0s"
            ): cv.positive_time_period_milliseconds,
            # Test-only control channel (grid injection + mock clock) for
            # the host-platform e2e suite. Enabling it adds the
            # USE_CT002_TEST_HOOKS define; leave unset in real configs.
            cv.Optional(CONF_TEST_CONTROL_PORT): cv.port,
            cv.Optional(CONF_FILTERS): FILTERS_SCHEMA,
            cv.Optional(CONF_BALANCER): BALANCER_SCHEMA,
            cv.Optional(CONF_SATURATION): SATURATION_SCHEMA,
            cv.Optional(CONF_MQTT_INSIGHTS): MQTT_INSIGHTS_SCHEMA,
            cv.Optional(CONF_MARSTEK_REGISTRATION): MARSTEK_REGISTRATION_SCHEMA,
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
    if CONF_CONSUMER_TTL in config:
        cg.add(
            var.set_consumer_ttl_seconds(int(config[CONF_CONSUMER_TTL].total_seconds))
        )
    cg.add(var.set_dedupe_window_ms(int(config[CONF_DEDUPE_WINDOW].total_milliseconds)))

    if CONF_TEST_CONTROL_PORT in config:
        # Compile in the test-only control channel (test_hooks.cpp) and point
        # it at the requested port. The define gates all the hook code.
        cg.add_define("USE_CT002_TEST_HOOKS")
        cg.add(var.set_control_port(config[CONF_TEST_CONTROL_PORT]))

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

    # Time-period config values come back as TimePeriodSeconds objects
    # when the user supplied them (or when our schema default kicked in
    # for an explicitly-present block). When the block is absent the
    # fallback is a plain int. Coerce both to a plain float for the C++
    # struct field, which is a `float seconds`.
    def _seconds(value):
        return float(value.total_seconds if hasattr(value, "total_seconds") else value)

    bal = config.get(CONF_BALANCER, {})
    bcfg = cg.StructInitializer(
        BalancerConfig,
        ("fair_distribution", bal.get(CONF_FAIR_DISTRIBUTION, True)),
        ("balance_gain", bal.get(CONF_BALANCE_GAIN, 0.2)),
        ("balance_deadband", bal.get(CONF_BALANCE_DEADBAND, 25.0)),
        ("error_boost_threshold", bal.get(CONF_ERROR_BOOST_THRESHOLD, 150.0)),
        ("error_boost_max", bal.get(CONF_ERROR_BOOST_MAX, 0.5)),
        ("error_reduce_threshold", bal.get(CONF_ERROR_REDUCE_THRESHOLD, 20.0)),
        ("max_correction_per_step", bal.get(CONF_MAX_CORRECTION_PER_STEP, 80.0)),
        ("max_target_step", bal.get(CONF_MAX_TARGET_STEP, 0.0)),
        ("pace_base_step", bal.get(CONF_PACE_BASE_STEP, 30.0)),
        ("pace_max_step", bal.get(CONF_PACE_MAX_STEP, 100.0)),
        ("osc_damp_max", bal.get(CONF_OSC_DAMP_MAX, 0.95)),
        ("osc_damp_alpha", bal.get(CONF_OSC_DAMP_ALPHA, 0.3)),
        ("osc_damp_decay", bal.get(CONF_OSC_DAMP_DECAY, 0.05)),
        ("osc_damp_threshold", bal.get(CONF_OSC_DAMP_THRESHOLD, 300.0)),
        ("min_efficient_power", bal.get(CONF_MIN_EFFICIENT_POWER, 0.0)),
        ("probe_min_power", bal.get(CONF_PROBE_MIN_POWER, 80.0)),
        (
            "efficiency_rotation_interval",
            _seconds(bal.get(CONF_EFFICIENCY_ROTATION_INTERVAL, 900)),
        ),
        ("efficiency_fade_alpha", bal.get(CONF_EFFICIENCY_FADE_ALPHA, 0.15)),
        (
            "efficiency_saturation_threshold",
            bal.get(CONF_EFFICIENCY_SATURATION_THRESHOLD, 0.4),
        ),
        ("min_dc_output", bal.get(CONF_MIN_DC_OUTPUT, 0.0)),
        ("grid_predict_trust", bal.get(CONF_GRID_PREDICT_TRUST, 0.5)),
        ("concentrate_deadband", bal.get(CONF_CONCENTRATE_DEADBAND, 60.0)),
    )
    cg.add(var.set_balancer_config(bcfg))

    sat = config.get(CONF_SATURATION, {})
    cg.add(
        var.set_balancer_saturation(
            sat.get(CONF_ALPHA, 0.15),
            sat.get(CONF_MIN_TARGET, 20.0),
            sat.get(CONF_DECAY_FACTOR, 0.995),
            _seconds(sat.get(CONF_GRACE_SECONDS, 90)),
            _seconds(sat.get(CONF_STALL_TIMEOUT_SECONDS, 60)),
            sat.get(CONF_ENABLED, True),
        )
    )

    if CONF_MQTT_INSIGHTS in config:
        await _to_code_mqtt_insights(config, var)
    if CONF_MARSTEK_REGISTRATION in config:
        await _to_code_marstek_registration(config, var)


async def _to_code_mqtt_insights(config, ct002_var):
    """Codegen for the optional `mqtt_insights:` sub-block.

    Each sub-block produces its own Application-tracked Component. The
    ct002 variable is passed in via set_ct002() so the insights component
    can register listeners and read snapshots. The MQTT client is
    resolved at runtime through `mqtt::global_mqtt_client`, so no ID
    plumbing is needed here.
    """
    sub = config[CONF_MQTT_INSIGHTS]
    var = cg.new_Pvariable(sub[CONF_ID])
    await cg.register_component(var, sub)
    cg.add(var.set_ct002(ct002_var))
    device_id = _resolve_mqtt_insights_device_id(sub[CONF_DEVICE_ID])
    cg.add(var.set_device_id(device_id))
    cg.add(var.set_base_topic(sub[CONF_BASE_TOPIC]))
    cg.add(var.set_ha_discovery(sub[CONF_HA_DISCOVERY]))
    cg.add(var.set_ha_discovery_prefix(sub[CONF_HA_DISCOVERY_PREFIX]))
    cg.add(var.set_marstek_mqtt_enabled(sub[CONF_MARSTEK_MQTT_ENABLED]))
    cg.add(
        var.set_marstek_mqtt_interval_ms(
            int(sub[CONF_MARSTEK_MQTT_INTERVAL].total_milliseconds)
        )
    )


async def _to_code_marstek_registration(config, ct002_var):
    """Codegen for the optional `marstek_registration:` sub-block.

    On first boot this drives an HTTPS state machine against the Marstek
    cloud, persists the resulting MAC via ESPPreferences, and feeds it
    back into the parent ct002 component via set_ct_mac().
    """
    sub = config[CONF_MARSTEK_REGISTRATION]
    # Gate the marstek_registration .cpp on this define — without it the
    # file lives in ct002/ but compiles to an empty translation unit on
    # ct002-only builds that don't pull in http_request.h.
    cg.add_define("USE_CT002_MARSTEK_REGISTRATION")
    var = cg.new_Pvariable(sub[CONF_ID])
    await cg.register_component(var, sub)
    cg.add(var.set_ct002(ct002_var))

    http_var = await cg.get_variable(sub[CONF_HTTP_REQUEST_ID])
    cg.add(var.set_http(http_var))

    cg.add(var.set_base_url(sub[CONF_BASE_URL]))
    cg.add(var.set_mailbox(sub[CONF_MAILBOX]))
    cg.add(var.set_password(sub[CONF_PASSWORD]))
    cg.add(var.set_timezone(sub[CONF_TIMEZONE]))
    cg.add(var.set_device_type(sub[CONF_DEVICE_TYPE]))
    cg.add(var.set_retry_interval_ms(int(sub[CONF_RETRY_INTERVAL].total_milliseconds)))
    cg.add(var.set_force_reregister(sub[CONF_FORCE_REREGISTER]))
