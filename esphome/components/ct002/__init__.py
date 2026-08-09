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
* `dashboard:` — serve AstraMeter's live status dashboard from the ESP32
  itself. On by default (ESP32 only); `dashboard: false` leaves it out of
  the firmware entirely, and the block is only needed to change an option.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

import esphome.codegen as cg
import esphome.config_validation as cv
import esphome.final_validate as fv
from esphome.components import http_request, sensor, web_server_base
from esphome.components.web_server_base import CONF_WEB_SERVER_BASE_ID
from esphome.const import (
    CONF_ALPHA,
    CONF_ID,
    CONF_JS_INCLUDE,
    CONF_LOCAL,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_TIMEZONE,
    CONF_UNIT_OF_MEASUREMENT,
    CONF_VERSION,
    CONF_WEB_SERVER,
    PLATFORM_ESP32,
)
from esphome.core import CORE

_LOGGER = logging.getLogger(__name__)

CODEOWNERS = ["@tomquist"]
DEPENDENCIES = ["sensor"]
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
cloud_reporting_ns = ct002_ns.namespace("cloud_reporting")
CloudReportingComponent = cloud_reporting_ns.class_(
    "CloudReportingComponent", cg.Component
)
dashboard_ns = ct002_ns.namespace("dashboard")
DashboardComponent = dashboard_ns.class_("DashboardComponent", cg.Component)

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
CONF_EFFICIENCY_DEMAND_ALPHA = "efficiency_demand_alpha"
CONF_MIN_DC_OUTPUT = "min_dc_output"
CONF_GRID_PREDICT_TRUST = "grid_predict_trust"
CONF_CONCENTRATE_DEADBAND = "concentrate_deadband"
CONF_IMPORT_TRIM_W = "import_trim_w"

# Saturation tracker sub-block
CONF_SATURATION = "saturation"
CONF_ENABLED = "enabled"
CONF_DECAY_FACTOR = "decay_factor"
CONF_GRACE_SECONDS = "grace_seconds"
CONF_STALL_TIMEOUT_SECONDS = "stall_timeout_seconds"
CONF_MIN_TARGET = "min_target"


# Power units the raw sensor state is auto-converted to watts from
# (issue #572: a kW sensor silently rounds to 0 W without conversion).
# Case matters: "mW" is milliwatts, "MW" megawatts. A sensor without a
# declared unit_of_measurement is assumed to already report watts.
POWER_UNIT_SCALES = {
    "W": 1.0,
    "kW": 1000.0,
    "MW": 1e6,
    "mW": 0.001,
}

_POWER_SENSOR_KEYS = (
    CONF_POWER_SENSOR_L1,
    CONF_POWER_SENSOR_L2,
    CONF_POWER_SENSOR_L3,
)


def _power_unit_scale(unit: str | None) -> float | None:
    """Scale factor that converts the sensor's declared unit to watts.

    None/empty (no declared unit) → 1.0 (assume watts, the historical
    behavior). A declared unit that isn't a power unit → None (reject).
    """
    if not unit:
        return 1.0
    return POWER_UNIT_SCALES.get(unit)


def _declared_unit(full_config, sensor_id) -> str | None:
    """Best-effort lookup of the referenced sensor's declared
    unit_of_measurement in the validated full config. Returns None when the
    sensor (or a unit) can't be found — e.g. `homeassistant` platform
    sensors that don't declare one.
    """
    get_path = getattr(full_config, "get_path_for_id", None)
    if get_path is None:
        return None
    try:
        path = get_path(sensor_id)[:-1]
        sensor_conf = full_config.get_config_for_path(path)
    except KeyError:
        return None
    if not isinstance(sensor_conf, dict):
        return None
    unit = sensor_conf.get(CONF_UNIT_OF_MEASUREMENT)
    return unit if isinstance(unit, str) and unit else None


def _validate_power_unit(conf_key: str, sensor_id, unit: str | None) -> None:
    """Raise cv.Invalid when a referenced sensor declares a non-power unit."""
    if _power_unit_scale(unit) is None:
        accepted = "/".join(POWER_UNIT_SCALES)
        raise cv.Invalid(
            f"{conf_key} '{sensor_id}' declares unit_of_measurement "
            f"'{unit}', which is not a power unit — ct002 needs grid power "
            f"in watts. Point it at a sensor reporting {accepted} "
            f"(kW-style units are converted to W automatically), or scale "
            f"the value to W and declare 'W'.",
            path=[conf_key],
        )


def _final_validate_dashboard_path(config, full):
    """Settle where the dashboard is mounted, now that `web_server:` is known.

    Both mount on the shared HTTP server, and the first handler that claims a
    URL wins — so two pages at `/` would resolve to whichever component
    happened to register first, which is codegen ordering, i.e. a coin flip.
    Since the dashboard is on by default, an unasked-for one must never be the
    reason somebody's `web_server:` build stops working: with no `path:` of its
    own it steps aside to `/astrameter` instead of contesting the root. A
    `path:` the user did write is theirs, so `path: /` alongside `web_server:`
    is the one case still worth refusing.
    """
    dashboard = config.get(CONF_DASHBOARD)
    if dashboard is None:
        return
    collides = CONF_WEB_SERVER in full
    if CONF_PATH not in dashboard:
        dashboard[CONF_PATH] = DASHBOARD_ASIDE_PATH if collides else ""
        return
    if dashboard[CONF_PATH] != "" or not collides:
        return
    raise cv.Invalid(
        "`web_server:` already serves ESPHome's own page at '/', so the "
        "AstraMeter dashboard cannot also be mounted there. Give it a path of "
        f"its own — set `path: {DASHBOARD_ASIDE_PATH}` (or any other) on this "
        "dashboard block, and the page will be served from there",
        path=[CONF_DASHBOARD, CONF_PATH],
    )


# The snippet handed to ESPHome's own web UI, which has no notion of a link:
# its frontends render a fixed set of entity domains, and every name and state
# is text-bound, so no entity can ever come out as an anchor. The one opening
# is `js_include:` — a file gzipped into flash, served at /0.js and loaded as a
# module ahead of <esp-app> — which leaves the link a few lines of DOM.
#
# Kept ASCII (→ rather than the arrow itself) so the bytes are the same
# whatever encoding the file is read back with. The colours are the frontend's
# own — its header background, and the text colour the `color-scheme` it sets
# implies — so the bar follows the page into dark mode without knowing which
# mode it is in. The font has to be restated rather than inherited: the
# frontend declares it on `:host`, inside <esp-app>'s shadow DOM, which does
# not reach an anchor sitting beside it in the light DOM.
#
# The leading `;` is the one that matters when a user's own `js_include:` runs
# ahead of this in the same module: theirs ending on an expression with no
# semicolon would otherwise take the IIFE below for its argument list.
_WEB_SERVER_LINK_TEMPLATE = """\
// astrameter-web-server-link — added by AstraMeter's ct002 component
// (dashboard: web_server_link: false switches it off).
// Regenerated on every build; edits here are lost.
;(() => {
  const link = document.createElement("a");
  link.href = %(href)s;
  link.textContent = "AstraMeter dashboard \\u2192";
  link.style.cssText =
    "display:block;padding:.6em 1.5em;text-align:center;color:inherit;" +
    "text-decoration:none;background:rgba(127,127,127,.3);" +
    "border-radius:0 0 12px 12px;" +
    "font-family:ui-monospace,system-ui,Helvetica,Roboto,Oxygen,Ubuntu,sans-serif";
  document.body.prepend(link);
})();
"""

# Where the generated snippet lands: ESPHome's data directory, under a
# subdirectory of our own.
#
# NOT the build directory, however much it looks like the natural home for a
# generated file. `write_cpp` calls `update_storage_json` first, which
# `clean_build(full=True)`s — an outright rmtree of the build directory —
# whenever the storage sidecar changed, which includes every first build. That
# lands between our final-validation and web_server's codegen, so a file
# written there is gone by the time it is read, and only on some builds.
#
# Not the user's config directory either: this is our artifact, not something
# they should find sitting next to their YAML.
WEB_SERVER_LINK_DIR = "astrameter"


def _web_server_link_path():
    """Where this device's generated snippet goes, or None if there is nowhere.

    Namespaced by device name because ESPHome's data directory is shared by
    every configuration beside it — two devices in one directory would
    otherwise write each other's link.
    """
    if CORE.config_path is None or not CORE.name:
        return None
    return CORE.relative_internal_path(
        WEB_SERVER_LINK_DIR, f"{CORE.name}.web-server-link.js"
    )


def _final_validate_web_server_link(config, full):
    """Give ESPHome's own page a way through to the dashboard beside it.

    With `web_server:` configured the dashboard steps aside to /astrameter
    (above), which nothing on ESPHome's page points at — so a device serving
    both hides one of them behind a URL the user has to already know. ESPHome
    offers no config-level way to add a link, so this writes the anchor into
    `web_server:`'s `js_include:` on the user's behalf.

    Reaching into another component's config is only sound because
    final-validation mutations *are* what codegen reads: `fv.full_config` is
    the same object that becomes `CORE.config`, and web_server's `to_code`
    runs afterwards (this is the same mechanism that defaults our own `path:`
    just above). It has to happen here rather than in `to_code`, though —
    web_server generates at CoroPriority.WEB and we generate at 0, so by the
    time our codegen runs it has already read the file.

    Anything that leaves the link out is a no-op and never an error: the page
    is reachable either way, and a build must not fail over a convenience.
    """
    dashboard = config.get(CONF_DASHBOARD)
    if dashboard is None or not dashboard.get(CONF_WEB_SERVER_LINK, True):
        return
    web_server = full.get(CONF_WEB_SERVER)
    if web_server is None:
        # Nothing to link *from* — with no ESPHome page, the dashboard has the
        # root to itself.
        return
    if web_server.get(CONF_VERSION, 2) == 1:
        # v1 builds its page in C++ and does have a `/0.js` tag — but behind
        # `js_include_ != nullptr`, and web_server's codegen never calls
        # `set_js_include()`, so the pointer is null for every v1 build and the
        # tag is never emitted. The file would reach flash and nothing would
        # load it.
        _LOGGER.info(
            "AstraMeter: `web_server: version: 1` serves a page that never "
            "loads its `js_include:`, so it cannot carry a link to the "
            "dashboard. The dashboard is served from http://<device>%s/",
            dashboard[CONF_PATH],
        )
        return
    if web_server.get(CONF_LOCAL):
        _LOGGER.info(
            "AstraMeter: `web_server: local: true` serves a prebuilt page that "
            "cannot carry a link to the dashboard. It is served from "
            "http://<device>%s/",
            dashboard[CONF_PATH],
        )
        return
    generated = _web_server_link_path()
    if generated is None:
        return

    existing = web_server.get(CONF_JS_INCLUDE)
    snippet = _WEB_SERVER_LINK_TEMPLATE % {"href": json.dumps(_link_href(config))}
    prior = ""
    # UnicodeError as much as OSError: `cv.file_` checks only that the path
    # exists, so a user's `js_include:` can be any bytes at all, and decoding
    # it is the one step here that can fail on a file that is otherwise fine.
    # Both have to leave the build alone rather than take it down.
    try:
        if existing:
            prior = _prior_js(CORE.relative_config_path(existing), generated)
        _write_atomically(generated, prior + snippet)
    except (OSError, UnicodeError):
        return
    web_server[CONF_JS_INCLUDE] = generated


# Marks our own output, for the benefit of the "is this the user's file?" test
# below. Kept in the snippet itself rather than inferred from the path, so a
# copy of it is recognised too.
_WEB_SERVER_LINK_MARKER = "astrameter-web-server-link"


def _prior_js(existing_path, generated):
    """The user's own `js_include:`, to run ahead of ours in the same module.

    Empty when what we are pointed at is our own previous output. That is not
    hypothetical: the config still names the user's file, but a path can reach
    ours by another spelling — a symlink, a `..` hop, a bind-mounted config
    directory — and each build would then prepend the last build's copy, so
    the number of links on the page (and the bytes in flash) would grow on
    every compile. Both the resolved path and the content are checked, since
    a *copy* of our output has neither the same path nor any business being
    prepended.
    """
    if existing_path.resolve() == generated.resolve():
        return ""
    content = existing_path.read_text(encoding="utf-8")
    if _WEB_SERVER_LINK_MARKER in content:
        return ""
    return content.rstrip("\n") + "\n\n"


def _write_atomically(path, content):
    """Write *content* to *path* as one replacement, never a truncated file.

    Generating during validation means this runs far more often than a build
    does — every `esphome config`, and the ESPHome dashboard's editor
    validates as you type. A plain truncating write can therefore land in the
    middle of a concurrent compile reading the same file, which would embed a
    half-written module and take the user's own script down with it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique per call, not merely per process. The overlap this guards against
    # is two validations of the same device inside one process, so a pid alone
    # would give both writers the same temporary path — and each one's `finally`
    # would then be free to delete the other's file out from under it.
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(content, encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _link_href(config) -> str:
    """The dashboard's URL as the injected anchor should spell it.

    With a trailing slash: `/astrameter` answers with a redirect to
    `/astrameter/`, so linking the bare prefix would cost every visitor a
    round trip.
    """
    return (config[CONF_DASHBOARD][CONF_PATH] or "") + "/"


def _final_validate(config):
    """Cross-component checks that need the whole validated configuration."""
    full = fv.full_config.get()
    # Reject power_sensor_lX references whose declared unit is not a power
    # unit (e.g. °C, %, kWh) — a wrong-unit sensor otherwise fails silently
    # at runtime (issue #572).
    for conf_key in _POWER_SENSOR_KEYS:
        if conf_key in config:
            _validate_power_unit(
                conf_key, config[conf_key], _declared_unit(full, config[conf_key])
            )
    _final_validate_dashboard_path(config, full)
    # Strictly after the path is settled: the link points at wherever the
    # dashboard ended up.
    _final_validate_web_server_link(config, full)


FINAL_VALIDATE_SCHEMA = _final_validate


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
        cv.Optional(CONF_EFFICIENCY_DEMAND_ALPHA, default=0.1): cv.float_range(
            min=0.01, max=1.0
        ),
        cv.Optional(CONF_MIN_DC_OUTPUT, default=0.0): cv.float_range(min=0.0),
        cv.Optional(CONF_GRID_PREDICT_TRUST, default=0.5): cv.float_range(
            min=0.0, max=1.0
        ),
        cv.Optional(CONF_CONCENTRATE_DEADBAND, default=60.0): cv.float_range(min=0.0),
        cv.Optional(CONF_IMPORT_TRIM_W, default=15.0): cv.float_range(min=0.0),
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

# ────────────────────────────────────────────────────────────────────────
# Sub-block: cloud_reporting (opt-in HTTP status reporting to hamedata.com)
# ────────────────────────────────────────────────────────────────────────

CONF_CLOUD_REPORTING = "cloud_reporting"
CONF_HOST = "host"
CONF_FCV = "fcv"
CONF_SV = "sv"
CONF_INTERVAL = "interval"

CLOUD_REPORTING_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(CloudReportingComponent),
            cv.GenerateID(CONF_HTTP_REQUEST_ID): cv.use_id(
                http_request.HttpRequestComponent
            ),
            cv.Optional(CONF_HOST, default="eu.hamedata.com"): cv.string_strict,
            cv.Optional(CONF_FCV, default="202409090159"): cv.string_strict,
            # getDateInfo writes sv -> the cloud record's version field, so it
            # defaults to the version managed devices are registered with (121).
            cv.Optional(CONF_SV, default=121): cv.int_,
            cv.Optional(
                CONF_INTERVAL, default="60s"
            ): cv.positive_time_period_milliseconds,
        }
    ),
    # Plain-HTTP GETs go through the http_request component.
    cv.requires_component("http_request"),
)


# ────────────────────────────────────────────────────────────────────────
# Sub-block: dashboard (live status web UI served from the ESP32; opt-out)
# ────────────────────────────────────────────────────────────────────────

CONF_DASHBOARD = "dashboard"
CONF_PATH = "path"
CONF_CONTROLS = "controls"
CONF_WEB_SERVER_LINK = "web_server_link"

# Where a default-on dashboard goes when `web_server:` already holds the root.
DASHBOARD_ASIDE_PATH = "/astrameter"


def _validate_dashboard_path(value):
    """Mount prefix, normalized to '' (root) or '/segment' with no trailing slash."""
    path = cv.string_strict(value)
    if not path.startswith("/"):
        raise cv.Invalid(f"{CONF_PATH!r} must start with '/'; got {value!r}")
    if "?" in path or "#" in path:
        raise cv.Invalid(f"{CONF_PATH!r} must be a plain path; got {value!r}")
    return path.rstrip("/")


def _dashboard_toggle(value):
    """The value as a bool if it is one, else None.

    Substitutions expand to *strings*, so a packaged config saying
    `dashboard: ${enable_dashboard}` hands this "false", not False. Deferring
    to cv.boolean keeps every spelling it accepts equivalent, instead of the
    dict form failing with "expected a dictionary".
    """
    try:
        return cv.boolean(value)
    except cv.Invalid:
        return None


def _dashboard_shorthand(value):
    """`dashboard:` and `dashboard: true` mean "on, with the defaults".

    Turning it on should not require knowing a single option name, and the
    dict form stays available for the rare setup that needs one.
    """
    if value is None or _dashboard_toggle(value) is True:
        return {}
    return value


DASHBOARD_OPTIONS_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(DashboardComponent),
        # ESPHome's shared HTTP server — the same one `web_server:` and
        # `captive_portal:` mount on, so they can coexist on one port.
        cv.GenerateID(CONF_WEB_SERVER_BASE_ID): cv.use_id(
            web_server_base.WebServerBase
        ),
        # No default: whether the unset path means the root or a corner of
        # its own depends on `web_server:`, which is only known once the whole
        # configuration is validated (_final_validate_dashboard_path fills it).
        cv.Optional(CONF_PATH): _validate_dashboard_path,
        # Off by default, like DASHBOARD_ALLOW_WRITE on the Python side:
        # the page has no login of its own, so steering someone's
        # batteries from the LAN stays an explicit choice.
        cv.Optional(CONF_CONTROLS, default=False): cv.boolean,
        # Only bites when `web_server:` is configured, which is the only case
        # where the dashboard is not at the root and so needs pointing at.
        cv.Optional(CONF_WEB_SERVER_LINK, default=True): cv.boolean,
    }
).extend(cv.COMPONENT_SCHEMA)

DASHBOARD_SCHEMA = cv.All(
    _dashboard_shorthand,
    DASHBOARD_OPTIONS_SCHEMA,
    # ESPHome's HTTP server exists on ESP32 (and the other ESP-family targets)
    # only — there is no implementation for `host`, and the smaller targets
    # would not fit the page anyway.
    cv.only_on([PLATFORM_ESP32]),
)


def _resolve_dashboard(config):
    """Settle whether there is a dashboard at all, before the schema runs.

    This is what makes the feature opt-out: an absent key becomes the default
    block, and only `dashboard: false` removes it. Afterwards the key is
    present if and only if the device gets a dashboard, which is the single
    fact everything downstream — the schema, codegen and AUTO_LOAD — reads.

    ESP32 only, because ESPHome's HTTP server is: there is no implementation
    for `host`, and the smaller targets could not hold the page anyway.
    """
    if not isinstance(config, dict):
        return config
    if _dashboard_toggle(config.get(CONF_DASHBOARD)) is False:
        # Removed outright, so a disabled dashboard pulls in nothing at all —
        # no HTTP server, no page in flash.
        del config[CONF_DASHBOARD]
    elif CONF_DASHBOARD not in config and CORE.is_esp32:
        config[CONF_DASHBOARD] = {}
    return config


def AUTO_LOAD(config):
    """Components pulled in on the user's behalf.

    json / md5 are always loaded so nobody has to add empty `json:` or `md5:`
    blocks for the sub-block infrastructure (they're cheap and only the
    sub-blocks reference them). web_server_base only when there is a dashboard
    to serve, which is by default — `dashboard: false` is what leaves the HTTP
    server out. mqtt / http_request have user-facing config of their own
    (broker, timeout, ...) so those stay `cv.requires_component` on the
    sub-schema instead.

    A parameterized AUTO_LOAD runs *after* CONFIG_SCHEMA, not before it
    (AddDynamicAutoLoadsValidationStep, priority -5.0), so what arrives here is
    the validated config with _resolve_dashboard already applied: the key is
    present exactly when a dashboard was asked for. Testing the raw spelling
    instead would read `dashboard: false` as an absent key and load the server
    for a build that opted out.
    """
    loads = ["socket", "json", "md5"]
    if isinstance(config, dict) and CONF_DASHBOARD in config:
        loads.append("web_server_base")
    return loads


def _astrameter_version() -> str:
    """AstraMeter's version, read from the repo this component ships in.

    External components are fetched as a whole repo checkout, so pyproject.toml
    sits three levels up. Best-effort: an unusual layout just means the page
    shows no version rather than a wrong one.
    """
    pyproject = Path(__file__).resolve().parents[3] / "pyproject.toml"
    try:
        for line in pyproject.read_text(encoding="utf-8").splitlines():
            if line.startswith("version = "):
                return line.split('"')[1]
    except (OSError, IndexError):
        pass
    return ""


def _is_sha(value: str) -> bool:
    """40 hex chars, or 64 for a repo using SHA-256 object names."""
    return len(value) in (40, 64) and all(c in "0123456789abcdef" for c in value)


def _ref_dirs(git_dir: Path) -> list[Path]:
    """Directories a ref may live in, nearest first.

    A linked worktree keeps its own HEAD but shares the branch refs, so the
    directory `.git` pointed at has to fall back to the one its ``commondir``
    names — otherwise a worktree checked out to a branch resolves to nothing.
    """
    dirs = [git_dir]
    commondir = git_dir / "commondir"
    if commondir.is_file():
        common = Path(commondir.read_text(encoding="utf-8").strip())
        if not common.is_absolute():
            common = (git_dir / common).resolve()
        dirs.append(common)
    return dirs


def _read_ref(git_dir: Path, ref: str) -> str:
    """Resolve one ref out of *git_dir*, loose file first, then `packed-refs`."""
    loose = git_dir / ref
    if loose.is_file():
        sha = loose.read_text(encoding="utf-8").strip()
        return sha if _is_sha(sha) else ""
    # Packed by `git gc`, so the loose file is gone.
    packed = git_dir / "packed-refs"
    if packed.is_file():
        for line in packed.read_text(encoding="utf-8").splitlines():
            sha, _, name = line.partition(" ")
            if name.strip() == ref and _is_sha(sha):
                return sha
    return ""


def _astrameter_git_commit() -> str:
    """SHA of the checkout this component ships in, or "" when there is none.

    The Python stack gets its SHA from ``GIT_COMMIT_SHA``, baked in when CI
    builds the image; a firmware has no such build step, so the SHA is read
    out of the repo `esphome compile` is reading the sources from. `.git` is
    read directly rather than shelling out to `git`, which need not be
    installed wherever ESPHome runs — and a shallow clone (what ESPHome makes
    of a `github://` source) has HEAD like any other.

    Best-effort, like the version above: no repo, an odd layout, or a ref
    this cannot resolve means the page shows no commit rather than a wrong
    one.
    """
    root = Path(__file__).resolve().parents[3]
    git_dir = root / ".git"
    try:
        if git_dir.is_file():
            # A linked worktree: `.git` is a pointer to a private directory
            # holding this worktree's own HEAD.
            pointer = git_dir.read_text(encoding="utf-8").split("gitdir:", 1)[1]
            git_dir = Path(pointer.strip())
            if not git_dir.is_absolute():
                git_dir = (root / git_dir).resolve()
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            # Detached — checking out a tag or a SHA, as a release build does.
            return head if _is_sha(head) else ""
        ref = head.split(":", 1)[1].strip()
        for ref_dir in _ref_dirs(git_dir):
            sha = _read_ref(ref_dir, ref)
            if sha:
                return sha
    # Anything unreadable or not a repository at all, including a `.git` file
    # whose bytes are not text (UnicodeDecodeError is a ValueError, not an
    # OSError): codegen must not fail over a cosmetic field.
    except (OSError, IndexError, UnicodeDecodeError):
        pass
    return ""


CONFIG_SCHEMA = cv.All(
    _resolve_dashboard,
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
            cv.Optional(CONF_CLOUD_REPORTING): CLOUD_REPORTING_SCHEMA,
            cv.Optional(CONF_DASHBOARD): DASHBOARD_SCHEMA,
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

    # Unit → watts conversion (issue #572). A declared power unit yields a
    # per-phase scale; declaring any unit (even W) also disables the runtime
    # kW-suspicion heuristic for that phase. Non-power units were already
    # rejected by FINAL_VALIDATE_SCHEMA.
    for idx, conf_key in enumerate(_POWER_SENSOR_KEYS):
        if conf_key not in config:
            continue
        unit = _declared_unit(CORE.config, config[conf_key])
        if unit is not None:
            scale = _power_unit_scale(unit)
            assert scale is not None  # guaranteed by final validate
            cg.add(var.set_power_unit_scale(idx, scale))

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
        ("efficiency_demand_alpha", bal.get(CONF_EFFICIENCY_DEMAND_ALPHA, 0.1)),
        ("min_dc_output", bal.get(CONF_MIN_DC_OUTPUT, 0.0)),
        ("grid_predict_trust", bal.get(CONF_GRID_PREDICT_TRUST, 0.5)),
        ("concentrate_deadband", bal.get(CONF_CONCENTRATE_DEADBAND, 60.0)),
        ("import_trim_w", bal.get(CONF_IMPORT_TRIM_W, 15.0)),
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
    if CONF_CLOUD_REPORTING in config:
        await _to_code_cloud_reporting(config, var)
    if CONF_DASHBOARD in config:
        await _to_code_dashboard(config, var)


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
    # The broker locator, for the dashboard's Diagnostics card. Taken from the
    # user's `mqtt:` block rather than the client at runtime: the client keeps
    # its credentials struct private, and the address sits next to the
    # username and password there.
    mqtt_config = CORE.config.get("mqtt") or {}
    cg.add(var.set_broker(str(mqtt_config.get("broker", ""))))
    cg.add(var.set_broker_port(int(mqtt_config.get("port", 0))))


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


async def _to_code_cloud_reporting(config, ct002_var):
    """Codegen for the optional `cloud_reporting:` sub-block.

    Mirrors src/astrameter/cloud_reporting.py: a loop()-driven state machine
    that runs the getDateInfo handshake once, then periodically GETs
    setCtReporting with live CT data. Plain HTTP via the http_request
    component; reads grid/bucket data from the parent ct002 via set_ct002().
    """
    sub = config[CONF_CLOUD_REPORTING]
    # Gate the cloud_reporting runtime .cpp on this define — the URL builders
    # compile unconditionally (for the host test), but the http_request-using
    # component body only when this sub-block is present.
    cg.add_define("USE_CT002_CLOUD_REPORTING")
    var = cg.new_Pvariable(sub[CONF_ID])
    await cg.register_component(var, sub)
    cg.add(var.set_ct002(ct002_var))

    http_var = await cg.get_variable(sub[CONF_HTTP_REQUEST_ID])
    cg.add(var.set_http(http_var))

    cg.add(var.set_host(sub[CONF_HOST]))
    cg.add(var.set_fcv(sub[CONF_FCV]))
    cg.add(var.set_sv(sub[CONF_SV]))
    cg.add(var.set_interval_ms(int(sub[CONF_INTERVAL].total_milliseconds)))


async def _to_code_dashboard(config, ct002_var):
    """Codegen for the optional `dashboard:` sub-block.

    Mounts the status page + `api/status` on ESPHome's shared HTTP server.
    The define gates dashboard.cpp, dashboard_state.cpp and the embedded page;
    with AUTO_LOAD leaving the HTTP server out too, `dashboard: false` saves
    about 90 KB of flash and the server's ~6 KB of heap.
    """
    sub = config[CONF_DASHBOARD]
    cg.add_define("USE_CT002_DASHBOARD")
    var = cg.new_Pvariable(sub[CONF_ID])
    await cg.register_component(var, sub)
    cg.add(var.set_ct002(ct002_var))

    base = await cg.get_variable(sub[CONF_WEB_SERVER_BASE_ID])
    cg.add(var.set_base(base))
    cg.add(var.set_path(sub[CONF_PATH]))
    cg.add(var.set_controls(sub[CONF_CONTROLS]))

    # When MQTT Insights is configured too, the page shows that integration's
    # state — the same card the Python add-on's dashboard has.
    if CONF_MQTT_INSIGHTS in config:
        insights = await cg.get_variable(config[CONF_MQTT_INSIGHTS][CONF_ID])
        cg.add(var.set_mqtt_insights(insights))

    # Shown on the page's Diagnostics tab, so a user can tell which build
    # they are looking at without reading the firmware log.
    cg.add(var.set_version(_astrameter_version()))
    cg.add(var.set_git_commit(_astrameter_git_commit()))
    logger_config = CORE.config.get("logger") or {}
    cg.add(var.set_log_level(str(logger_config.get("level", ""))))
