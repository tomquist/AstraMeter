"""Constants for the AstraMeter integration."""

from __future__ import annotations

from homeassistant.const import Platform

DOMAIN = "astrameter"

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.BUTTON,
]

# --- Config entry data keys ("what" — set in the config flow) ---
CONF_DEVICE_TYPE = "device_type"
CONF_GRID_ENTITIES = "grid_entities"
CONF_PAIR_MODE = "pair_mode"
CONF_INPUT_ENTITIES = "input_entities"
CONF_OUTPUT_ENTITIES = "output_entities"
CONF_UDP_PORT = "udp_port"
CONF_DEVICE_ID = "device_id"
CONF_CT_TYPE = "ct_type"
CONF_CT_MAC = "ct_mac"

# Device types this integration can emulate.
DEVICE_TYPE_CT002 = "ct002"
DEVICE_TYPE_CT003 = "ct003"
DEVICE_TYPE_SHELLY_PRO_3EM_OLD = "shellypro3em_old"
DEVICE_TYPE_SHELLY_PRO_3EM_NEW = "shellypro3em_new"
DEVICE_TYPE_SHELLY_EM_G3 = "shellyemg3"
DEVICE_TYPE_SHELLY_PRO_EM50 = "shellyproem50"

CT002_DEVICE_TYPES = (DEVICE_TYPE_CT002, DEVICE_TYPE_CT003)
SHELLY_DEVICE_TYPES = (
    DEVICE_TYPE_SHELLY_PRO_3EM_OLD,
    DEVICE_TYPE_SHELLY_PRO_3EM_NEW,
    DEVICE_TYPE_SHELLY_EM_G3,
    DEVICE_TYPE_SHELLY_PRO_EM50,
)
ALL_DEVICE_TYPES = CT002_DEVICE_TYPES + SHELLY_DEVICE_TYPES

# Default UDP port per device type. CT002/CT003 default to 12345 (user-editable).
DEFAULT_CT002_PORT = 12345
SHELLY_PORTS: dict[str, int] = {
    DEVICE_TYPE_SHELLY_PRO_3EM_OLD: 1010,
    DEVICE_TYPE_SHELLY_PRO_3EM_NEW: 2220,
    DEVICE_TYPE_SHELLY_EM_G3: 2222,
    DEVICE_TYPE_SHELLY_PRO_EM50: 2223,
}


def udp_port_for(device_type: str, configured: int | None = None) -> int:
    """Resolve the UDP port for a device type."""
    if device_type in SHELLY_PORTS:
        return SHELLY_PORTS[device_type]
    return configured or DEFAULT_CT002_PORT


# --- Options keys ("how" — filter/balancer tuning) ---
CONF_CT_TYPE_OPT = "ct_type"
# Filter pipeline (mirror config_loader / FilterOptions)
CONF_POWER_OFFSET = "power_offset"
CONF_POWER_MULTIPLIER = "power_multiplier"
CONF_THROTTLE_INTERVAL = "throttle_interval"
CONF_SMOOTH_ALPHA = "smooth_target_alpha"
CONF_MAX_SMOOTH_STEP = "max_smooth_step"
CONF_DEADBAND = "deadband"
CONF_HAMPEL_WINDOW = "hampel_window"
CONF_HAMPEL_N_SIGMA = "hampel_n_sigma"
CONF_HAMPEL_MIN_THRESHOLD = "hampel_min_threshold"
CONF_PID_KP = "pid_kp"
CONF_PID_KI = "pid_ki"
CONF_PID_KD = "pid_kd"
CONF_PID_OUTPUT_MAX = "pid_output_max"
CONF_PID_MODE = "pid_mode"
# CT002 tuning surfaced in options
CONF_ACTIVE_CONTROL = "active_control"
CONF_MIN_DC_OUTPUT = "min_dc_output"
CONF_EFFICIENCY_ROTATION_INTERVAL = "efficiency_rotation_interval"
CONF_MIN_EFFICIENT_POWER = "min_efficient_power"

# Defaults that mirror config_loader / CT002 fallbacks.
DEFAULTS: dict[str, object] = {
    CONF_THROTTLE_INTERVAL: 0.0,
    CONF_SMOOTH_ALPHA: 0.0,
    CONF_MAX_SMOOTH_STEP: 0.0,
    CONF_DEADBAND: 0.0,
    CONF_HAMPEL_WINDOW: 0,
    CONF_HAMPEL_N_SIGMA: 3.0,
    CONF_HAMPEL_MIN_THRESHOLD: 0.0,
    CONF_PID_KP: 0.0,
    CONF_PID_KI: 0.0,
    CONF_PID_KD: 0.0,
    CONF_PID_OUTPUT_MAX: 800.0,
    CONF_PID_MODE: "bias",
    CONF_ACTIVE_CONTROL: True,
    CONF_MIN_DC_OUTPUT: 0.0,
    CONF_EFFICIENCY_ROTATION_INTERVAL: 900,
    CONF_MIN_EFFICIENT_POWER: 0,
    CONF_CT_TYPE_OPT: "HME-4",
}

# Options that require a full reload (structural) vs hot-swappable (filter).
FILTER_OPTION_KEYS = frozenset(
    {
        CONF_POWER_OFFSET,
        CONF_POWER_MULTIPLIER,
        CONF_THROTTLE_INTERVAL,
        CONF_SMOOTH_ALPHA,
        CONF_MAX_SMOOTH_STEP,
        CONF_DEADBAND,
        CONF_HAMPEL_WINDOW,
        CONF_HAMPEL_N_SIGMA,
        CONF_HAMPEL_MIN_THRESHOLD,
        CONF_PID_KP,
        CONF_PID_KI,
        CONF_PID_KD,
        CONF_PID_OUTPUT_MAX,
        CONF_PID_MODE,
    }
)


# --- Dispatcher signals (per entry) ---
def signal_new_consumer(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_new_consumer"


def signal_update(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_update"


def signal_health(entry_id: str) -> str:
    return f"{DOMAIN}_{entry_id}_health"
