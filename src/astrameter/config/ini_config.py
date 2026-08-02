"""``config.ini`` backend: settings read from a configuration file.

The one place that still knows about ``[SECTION]`` / ``KEY`` names when the app
is configured by file. Other backends (e.g. the Home Assistant add-on) read
their own source and answer the same :class:`AppConfig` interface.
"""

from __future__ import annotations

import configparser
import dataclasses
from typing import TYPE_CHECKING

from astrameter.config.config_loader import (
    new_config_parser,
    read_all_powermeter_configs,
    read_mqtt_insights_config,
    read_signal_settings,
)
from astrameter.config.settings import (
    AppConfig,
    CtSettings,
    GeneralSettings,
    MarstekSettings,
    SignalSettings,
)

if TYPE_CHECKING:
    from astrameter.config.settings import ConfiguredPowermeter
    from astrameter.mqtt_insights import MqttInsightsConfig

GENERAL_SECTION = "GENERAL"
MARSTEK_SECTION = "MARSTEK"


class IniAppConfig(AppConfig):
    """Settings backed by a ``config.ini`` file."""

    def __init__(
        self, config: configparser.ConfigParser, path: str | None = None
    ) -> None:
        self._config = config
        self.path = path

    @classmethod
    def from_file(cls, path: str) -> IniAppConfig:
        config = new_config_parser()
        config.read(path)
        return cls(config, path)

    def general(self) -> GeneralSettings:
        config = self._config
        defaults = GeneralSettings()
        return GeneralSettings(
            device_types=_split(config.get(GENERAL_SECTION, "DEVICE_TYPE", fallback=""))
            or defaults.device_types,
            device_ids=_split(config.get(GENERAL_SECTION, "DEVICE_IDS", fallback="")),
            skip_powermeter_test=config.getboolean(
                GENERAL_SECTION,
                "SKIP_POWERMETER_TEST",
                fallback=defaults.skip_powermeter_test,
            ),
            dedupe_time_window=config.getfloat(
                GENERAL_SECTION,
                "DEDUPE_TIME_WINDOW",
                fallback=defaults.dedupe_time_window,
            ),
            enable_web_server=config.getboolean(
                GENERAL_SECTION,
                "ENABLE_WEB_SERVER",
                fallback=defaults.enable_web_server,
            ),
            web_config_enabled=config.getboolean(
                GENERAL_SECTION,
                "WEB_CONFIG_ENABLED",
                fallback=defaults.web_config_enabled,
            ),
            web_server_port=config.getint(
                GENERAL_SECTION, "WEB_SERVER_PORT", fallback=defaults.web_server_port
            ),
            dashboard=config.getboolean(
                GENERAL_SECTION, "DASHBOARD_ENABLED", fallback=defaults.dashboard
            ),
            dashboard_allow_write=config.getboolean(
                GENERAL_SECTION,
                "DASHBOARD_ALLOW_WRITE",
                fallback=defaults.dashboard_allow_write,
            ),
            dashboard_direct_access=config.getboolean(
                GENERAL_SECTION,
                "DASHBOARD_DIRECT_ACCESS",
                fallback=defaults.dashboard_direct_access,
            ),
            signal=read_signal_settings(
                GENERAL_SECTION, config, SignalSettings(), with_transform=False
            ),
        )

    def ct(self, device_type: str) -> CtSettings:
        config = self._config
        defaults = CtSettings()
        section = self.ct_section(device_type)
        global_dedupe = config.getfloat(
            GENERAL_SECTION, "DEDUPE_TIME_WINDOW", fallback=defaults.dedupe_time_window
        )
        return CtSettings(
            ct_mac=config.get(section, "CT_MAC", fallback=defaults.ct_mac),
            udp_port=config.getint(section, "UDP_PORT", fallback=defaults.udp_port),
            wifi_rssi=config.getint(section, "WIFI_RSSI", fallback=defaults.wifi_rssi),
            dedupe_time_window=config.getfloat(
                section, "DEDUPE_TIME_WINDOW", fallback=global_dedupe
            ),
            consumer_ttl=config.getint(
                section, "CONSUMER_TTL", fallback=defaults.consumer_ttl
            ),
            debug_status=config.getboolean(
                section, "DEBUG_STATUS", fallback=defaults.debug_status
            ),
            cloud_reporting=config.getboolean(
                section, "CLOUD_REPORTING", fallback=defaults.cloud_reporting
            ),
            cloud_reporting_host=config.get(
                section, "CLOUD_REPORTING_HOST", fallback=""
            ).strip()
            or defaults.cloud_reporting_host,
            cloud_reporting_interval=config.getfloat(
                section,
                "CLOUD_REPORTING_INTERVAL",
                fallback=defaults.cloud_reporting_interval,
            ),
            active_control=config.getboolean(
                section, "ACTIVE_CONTROL", fallback=defaults.active_control
            ),
            fair_distribution=config.getboolean(
                section, "FAIR_DISTRIBUTION", fallback=defaults.fair_distribution
            ),
            balance_gain=config.getfloat(
                section, "BALANCE_GAIN", fallback=defaults.balance_gain
            ),
            balance_deadband=config.getint(
                section, "BALANCE_DEADBAND", fallback=defaults.balance_deadband
            ),
            max_correction_per_step=config.getint(
                section,
                "MAX_CORRECTION_PER_STEP",
                fallback=defaults.max_correction_per_step,
            ),
            error_boost_threshold=config.getint(
                section,
                "ERROR_BOOST_THRESHOLD",
                fallback=defaults.error_boost_threshold,
            ),
            error_boost_max=config.getfloat(
                section, "ERROR_BOOST_MAX", fallback=defaults.error_boost_max
            ),
            error_reduce_threshold=config.getint(
                section,
                "ERROR_REDUCE_THRESHOLD",
                fallback=defaults.error_reduce_threshold,
            ),
            max_target_step=config.getint(
                section, "MAX_TARGET_STEP", fallback=defaults.max_target_step
            ),
            pace_base_step=config.getint(
                section, "PACE_BASE_STEP", fallback=defaults.pace_base_step
            ),
            pace_max_step=config.getint(
                section, "PACE_MAX_STEP", fallback=defaults.pace_max_step
            ),
            osc_damp_max=config.getfloat(
                section, "OSC_DAMP_MAX", fallback=defaults.osc_damp_max
            ),
            osc_damp_alpha=config.getfloat(
                section, "OSC_DAMP_ALPHA", fallback=defaults.osc_damp_alpha
            ),
            osc_damp_decay=config.getfloat(
                section, "OSC_DAMP_DECAY", fallback=defaults.osc_damp_decay
            ),
            osc_damp_threshold=config.getfloat(
                section, "OSC_DAMP_THRESHOLD", fallback=defaults.osc_damp_threshold
            ),
            grid_predict_trust=config.getfloat(
                section, "GRID_PREDICT_TRUST", fallback=defaults.grid_predict_trust
            ),
            concentrate_deadband=config.getfloat(
                section, "CONCENTRATE_DEADBAND", fallback=defaults.concentrate_deadband
            ),
            import_trim_w=config.getfloat(
                section, "IMPORT_TRIM_W", fallback=defaults.import_trim_w
            ),
            saturation_detection=config.getboolean(
                section, "SATURATION_DETECTION", fallback=defaults.saturation_detection
            ),
            saturation_alpha=config.getfloat(
                section, "SATURATION_ALPHA", fallback=defaults.saturation_alpha
            ),
            min_target_for_saturation=config.getint(
                section,
                "MIN_TARGET_FOR_SATURATION",
                fallback=defaults.min_target_for_saturation,
            ),
            saturation_grace_seconds=config.getfloat(
                section,
                "SATURATION_GRACE_SECONDS",
                fallback=defaults.saturation_grace_seconds,
            ),
            saturation_stall_timeout_seconds=config.getfloat(
                section,
                "SATURATION_STALL_TIMEOUT_SECONDS",
                fallback=defaults.saturation_stall_timeout_seconds,
            ),
            saturation_decay_factor=config.getfloat(
                section,
                "SATURATION_DECAY_FACTOR",
                fallback=defaults.saturation_decay_factor,
            ),
            min_efficient_power=config.getint(
                section, "MIN_EFFICIENT_POWER", fallback=defaults.min_efficient_power
            ),
            probe_min_power=config.getint(
                section, "PROBE_MIN_POWER", fallback=defaults.probe_min_power
            ),
            efficiency_rotation_interval=config.getint(
                section,
                "EFFICIENCY_ROTATION_INTERVAL",
                fallback=defaults.efficiency_rotation_interval,
            ),
            efficiency_fade_alpha=config.getfloat(
                section,
                "EFFICIENCY_FADE_ALPHA",
                fallback=defaults.efficiency_fade_alpha,
            ),
            efficiency_saturation_threshold=config.getfloat(
                section,
                "EFFICIENCY_SATURATION_THRESHOLD",
                fallback=defaults.efficiency_saturation_threshold,
            ),
            efficiency_demand_alpha=config.getfloat(
                section,
                "EFFICIENCY_DEMAND_ALPHA",
                fallback=defaults.efficiency_demand_alpha,
            ),
            min_dc_output=config.getfloat(
                section, "MIN_DC_OUTPUT", fallback=defaults.min_dc_output
            ),
        )

    def ct_section(self, device_type: str) -> str:
        """The section a CT emulator reads: ``[CT003]`` only if it exists."""
        if device_type == "ct003" and self._config.has_section("CT003"):
            return "CT003"
        return "CT002"

    def marstek(self) -> MarstekSettings:
        config = self._config
        defaults = MarstekSettings()
        return MarstekSettings(
            enable=config.getboolean(
                MARSTEK_SECTION, "ENABLE", fallback=defaults.enable
            ),
            mailbox=config.get(MARSTEK_SECTION, "MAILBOX", fallback=defaults.mailbox),
            password=config.get(
                MARSTEK_SECTION, "PASSWORD", fallback=defaults.password
            ),
            base_url=config.get(
                MARSTEK_SECTION, "BASE_URL", fallback=defaults.base_url
            ),
            timezone=config.get(
                MARSTEK_SECTION, "TIMEZONE", fallback=defaults.timezone
            ),
        )

    def mqtt_insights(self) -> MqttInsightsConfig | None:
        return read_mqtt_insights_config(self._config)

    def powermeters(self, general: GeneralSettings) -> list[ConfiguredPowermeter]:
        return read_all_powermeter_configs(self._config, general.signal)


def _split(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


# -- rendering: the dual of the readers above -------------------------------
#
# Turning settings back into a ``config.ini`` is what lets the dashboard hand
# a guided-setup user a file to take over from. It is the inverse of the
# reading above and lives beside it deliberately: `ini_config_test.py` asserts
# the round trip, so a key that is read one way and written another fails
# there rather than handing someone a config that quietly loses a setting.
#
# Every key below is its field name uppercased; the exceptions are the two
# maps. If that ever stops being true for a new field, the round-trip test is
# what tells you.

#: Fields whose INI key is not simply the uppercased field name.
_GENERAL_KEY_OVERRIDES = {
    "device_types": "DEVICE_TYPE",
    "dashboard": "DASHBOARD_ENABLED",
}
_SIGNAL_KEY_OVERRIDES = {
    "smooth_alpha": "SMOOTH_TARGET_ALPHA",
    "offsets": "POWER_OFFSET",
    "multipliers": "POWER_MULTIPLIER",
}


def _render_value(value: object) -> str:
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value)
    return str(value)


def _changed(
    settings: object, defaults: object, overrides: dict[str, str]
) -> list[tuple[str, str]]:
    """The keys whose value differs from the dataclass default.

    Only writing what the user actually changed keeps the file short enough to
    read, and leaves the defaults in one place — ``settings.py`` — rather than
    freezing today's values into everybody's config file.
    """
    out: list[tuple[str, str]] = []
    for f in dataclasses.fields(settings):  # type: ignore[arg-type]
        if f.name == "signal":
            continue
        value = getattr(settings, f.name)
        if value is None or value == getattr(defaults, f.name):
            continue
        out.append((overrides.get(f.name, f.name.upper()), _render_value(value)))
    return out


def _section(
    name: str, pairs: list[tuple[str, str]], *, always: bool = False
) -> list[str]:
    if not pairs and not always:
        return []
    return [f"[{name}]", *(f"{key} = {value}" for key, value in pairs), ""]


def render_ini(config: AppConfig, device_types: list[str] | None = None) -> str:
    """Render *config*'s settings as a ``config.ini`` the reader accepts.

    Round-trips: reading the result back yields the same settings. What it
    cannot carry is the power sources — those are built objects rather than
    settings (see ``AppConfig.powermeters``), so a caller that knows how its
    source was configured appends that section itself.
    """
    general = config.general()
    lines = _section(
        GENERAL_SECTION,
        _changed(general, GeneralSettings(), _GENERAL_KEY_OVERRIDES)
        + _changed(general.signal, SignalSettings(), _SIGNAL_KEY_OVERRIDES),
    )

    for device_type in device_types or general.device_types:
        if device_type not in ("ct002", "ct003"):
            continue
        # Emitted even when every value is a default: `ct_section` falls back
        # to [CT002] for a missing [CT003], so leaving an all-default CT003
        # out would silently hand it CT002's settings on the way back in.
        lines += _section(
            device_type.upper(),
            _changed(config.ct(device_type), CtSettings(), {}),
            always=True,
        )

    marstek = config.marstek()
    if marstek.enable:
        lines += _section(MARSTEK_SECTION, _changed(marstek, MarstekSettings(), {}))

    return "\n".join(lines)
