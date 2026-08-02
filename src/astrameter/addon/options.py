"""The Home Assistant add-on option ⇄ ``config.ini`` mapping.

One declarative table replaces ~150 lines of ``echo`` in ``run.sh``.  Keeping
it as data rather than control flow is the point: it can be asserted against
``ha_addon/config.yaml`` in a test, so an option that exists in the add-on UI
but silently never reaches the service is a test failure instead of a
mystery bug report.

``bashio::config.has_value`` treats null and the empty string as "not set",
and this module keeps that rule (:func:`_is_set`) so a fresh install produces
byte-identical output to the shell it replaces.
"""

from __future__ import annotations

import dataclasses

# Section placeholders resolved per install: the CT section depends on which
# device types the user emulates, and may be emitted twice.
CT = "__CT__"

GENERAL = "GENERAL"
HOMEASSISTANT = "HOMEASSISTANT"
#: Also `[HOMEASSISTANT]`, but emitted *above* the grid-power keys, which are
#: built by hand because one sensor and two sensors produce different keys.
HOMEASSISTANT_HEAD = "__HOMEASSISTANT_HEAD__"


@dataclasses.dataclass(frozen=True, slots=True)
class OptionMap:
    """One add-on option and the ``config.ini`` key it becomes."""

    option: str
    section: str
    key: str
    #: Emit even when the user left it unset, using whatever the Supervisor
    #: merged in from ``config.yaml``'s ``options:`` defaults.  Matches the
    #: handful of keys run.sh wrote unconditionally.
    always: bool = False


# The order here is the order keys appear in the generated file, so a diff
# against a previously generated config stays readable.
OPTION_MAP: tuple[OptionMap, ...] = (
    # -- [GENERAL] ----------------------------------------------------
    OptionMap("device_types", GENERAL, "DEVICE_TYPE", always=True),
    OptionMap("throttle_interval", GENERAL, "THROTTLE_INTERVAL", always=True),
    OptionMap("dedupe_time_window", GENERAL, "DEDUPE_TIME_WINDOW"),
    # Written so the generated file documents what is running; the add-on
    # options are still what the service acts on, since a custom config has
    # no generated file to carry them.
    OptionMap("dashboard", GENERAL, "DASHBOARD_ENABLED", always=True),
    OptionMap("dashboard_allow_write", GENERAL, "DASHBOARD_ALLOW_WRITE", always=True),
    # -- [CT002] / [CT003] --------------------------------------------
    # CT_MAC is written even when empty, exactly as run.sh did: an empty
    # value means "accept any CT MAC", which is also the unset behaviour.
    OptionMap("ct_mac", CT, "CT_MAC", always=True),
    OptionMap("active_control", CT, "ACTIVE_CONTROL"),
    OptionMap("min_efficient_power", CT, "MIN_EFFICIENT_POWER"),
    OptionMap("efficiency_rotation_interval", CT, "EFFICIENCY_ROTATION_INTERVAL"),
    OptionMap("min_dc_output", CT, "MIN_DC_OUTPUT"),
    OptionMap("grid_predict_trust", CT, "GRID_PREDICT_TRUST"),
    # The balancer / active-control tuning group.
    OptionMap("fair_distribution", CT, "FAIR_DISTRIBUTION"),
    OptionMap("balance_gain", CT, "BALANCE_GAIN"),
    OptionMap("balance_deadband", CT, "BALANCE_DEADBAND"),
    OptionMap("max_correction_per_step", CT, "MAX_CORRECTION_PER_STEP"),
    OptionMap("error_boost_threshold", CT, "ERROR_BOOST_THRESHOLD"),
    OptionMap("error_boost_max", CT, "ERROR_BOOST_MAX"),
    OptionMap("error_reduce_threshold", CT, "ERROR_REDUCE_THRESHOLD"),
    OptionMap("max_target_step", CT, "MAX_TARGET_STEP"),
    OptionMap("pace_base_step", CT, "PACE_BASE_STEP"),
    OptionMap("pace_max_step", CT, "PACE_MAX_STEP"),
    OptionMap("osc_damp_max", CT, "OSC_DAMP_MAX"),
    OptionMap("osc_damp_alpha", CT, "OSC_DAMP_ALPHA"),
    OptionMap("osc_damp_decay", CT, "OSC_DAMP_DECAY"),
    OptionMap("osc_damp_threshold", CT, "OSC_DAMP_THRESHOLD"),
    OptionMap("concentrate_deadband", CT, "CONCENTRATE_DEADBAND"),
    OptionMap("import_trim_w", CT, "IMPORT_TRIM_W"),
    # Opt-in HTTP cloud reporting.
    OptionMap("cloud_reporting", CT, "CLOUD_REPORTING"),
    OptionMap("cloud_reporting_host", CT, "CLOUD_REPORTING_HOST"),
    OptionMap("cloud_reporting_interval", CT, "CLOUD_REPORTING_INTERVAL"),
    # -- [HOMEASSISTANT] ----------------------------------------------
    # power_input_alias / power_output_alias are handled separately: which
    # keys they become depends on whether import and export are two sensors.
    OptionMap(
        "wait_for_next_message",
        HOMEASSISTANT_HEAD,
        "WAIT_FOR_NEXT_MESSAGE",
        always=True,
    ),
    OptionMap("power_offset", HOMEASSISTANT, "POWER_OFFSET"),
    OptionMap("power_multiplier", HOMEASSISTANT, "POWER_MULTIPLIER"),
    OptionMap("smooth_target_alpha", HOMEASSISTANT, "SMOOTH_TARGET_ALPHA"),
    OptionMap("max_smooth_step", HOMEASSISTANT, "MAX_SMOOTH_STEP"),
    OptionMap("deadband", HOMEASSISTANT, "DEADBAND"),
    OptionMap("hampel_window", HOMEASSISTANT, "HAMPEL_WINDOW"),
    OptionMap("hampel_n_sigma", HOMEASSISTANT, "HAMPEL_N_SIGMA"),
    OptionMap("hampel_min_threshold", HOMEASSISTANT, "HAMPEL_MIN_THRESHOLD"),
    OptionMap("pid_kp", HOMEASSISTANT, "PID_KP"),
    OptionMap("pid_ki", HOMEASSISTANT, "PID_KI"),
    OptionMap("pid_kd", HOMEASSISTANT, "PID_KD"),
    OptionMap("pid_output_max", HOMEASSISTANT, "PID_OUTPUT_MAX"),
    OptionMap("pid_mode", HOMEASSISTANT, "PID_MODE"),
)

#: Options deliberately absent from OPTION_MAP because they steer the
#: generator rather than becoming a key.  Listed explicitly so the drift test
#: can tell "handled elsewhere" from "forgotten".
HANDLED_SEPARATELY: frozenset[str] = frozenset(
    {
        "custom_config",  # selects the config source entirely
        "log_level",  # a CLI flag, not a config key
        "dashboard_direct_access",  # a runtime policy, never a config key
        "mqtt_uri",  # [MQTT_INSIGHTS], with broker autodiscovery as fallback
        "power_input_alias",  # pairs with power_output_alias
        "power_output_alias",
        "marstek_auto_register_ct_device",  # gates the whole [MARSTEK] section
        "marstek_mailbox",
        "marstek_password",
    }
)


def is_set(value: object) -> bool:
    """Whether the user actually gave this option a value.

    Mirrors ``bashio::config.has_value``: ``null`` and ``""`` both count as
    unset, so an optional key the user left blank is omitted rather than
    written as an empty INI value the loader would then have to parse.
    """
    return value is not None and value != ""


def render(value: object) -> str:
    """Render an option value the way the shell did.

    JSON booleans must become ``true``/``false`` rather than Python's
    ``True``/``False``; ``configparser.getboolean`` accepts both, but the
    generated file is shown to users and pasted into bug reports, so it
    should look like the one they would have written by hand.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    # Strip stray newlines the way run.sh did for the multi-phase lists.
    return str(value).replace("\r", "").replace("\n", "")
