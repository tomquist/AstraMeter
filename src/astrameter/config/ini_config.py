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
    GENERAL_KEY_OVERRIDES,
    SIGNAL_KEY_OVERRIDES,
    general_key,
    ini_key,
    new_config_parser,
    read_all_powermeter_configs,
    read_fields,
    read_mqtt_insights_config,
    read_signal_settings,
    split_csv,
)
from astrameter.config.settings import (
    AppConfig,
    CtSettings,
    GeneralSettings,
    MarstekSettings,
    SignalSettings,
    is_ct,
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

    def declared_general_keys(self, *fields: str) -> list[str]:
        """Which of *fields* the file actually sets, named as its INI keys.

        A backend that overrides a setting uses this to tell a user their
        value is being ignored, without also nagging the majority whose file
        never mentioned it.
        """
        keys = [general_key(field) for field in fields]
        return [key for key in keys if self._config.has_option(GENERAL_SECTION, key)]

    def general(self) -> GeneralSettings:
        config = self._config
        defaults = GeneralSettings()
        return GeneralSettings(
            device_types=split_csv(
                config.get(GENERAL_SECTION, general_key("device_types"), fallback="")
            )
            or defaults.device_types,
            device_ids=split_csv(
                config.get(GENERAL_SECTION, general_key("device_ids"), fallback="")
            ),
            signal=read_signal_settings(
                GENERAL_SECTION, config, SignalSettings(), with_transform=False
            ),
            **read_fields(
                config,
                GENERAL_SECTION,
                GeneralSettings,
                GENERAL_KEY_OVERRIDES,
                skip=("device_types", "device_ids", "signal"),
            ),
        )

    def ct(self, device_type: str) -> CtSettings:
        config = self._config
        defaults = CtSettings()
        section = self.ct_section(device_type)
        # An emulator without a window of its own inherits [GENERAL]'s.
        global_dedupe = config.getfloat(
            GENERAL_SECTION,
            general_key("dedupe_time_window"),
            fallback=defaults.dedupe_time_window,
        )
        # A blank host means the default, not "no host".
        host = config.get(section, "CLOUD_REPORTING_HOST", fallback="").strip()
        return CtSettings(
            dedupe_time_window=config.getfloat(
                section, "DEDUPE_TIME_WINDOW", fallback=global_dedupe
            ),
            cloud_reporting_host=host or defaults.cloud_reporting_host,
            **read_fields(
                config,
                section,
                CtSettings,
                skip=("dedupe_time_window", "cloud_reporting_host"),
            ),
        )

    def ct_section(self, device_type: str) -> str:
        """The section a CT emulator reads: ``[CT003]`` only if it exists."""
        if device_type == "ct003" and self._config.has_section("CT003"):
            return "CT003"
        return "CT002"

    def marstek(self) -> MarstekSettings:
        return MarstekSettings(
            **read_fields(self._config, MARSTEK_SECTION, MarstekSettings)
        )

    def mqtt_insights(self) -> MqttInsightsConfig | None:
        return read_mqtt_insights_config(self._config)

    def powermeters(self, general: GeneralSettings) -> list[ConfiguredPowermeter]:
        return read_all_powermeter_configs(self._config, general.signal)


# Rendering settings back into a ``config.ini`` is what lets the dashboard hand
# a guided-setup user a file to take over from. It is the readers' inverse and
# lives beside them so the round-trip test keeps the two in step.


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
        out.append((ini_key(f.name, overrides), _render_value(value)))
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
        _changed(general, GeneralSettings(), GENERAL_KEY_OVERRIDES)
        + _changed(general.signal, SignalSettings(), SIGNAL_KEY_OVERRIDES),
    )

    for device_type in device_types or general.device_types:
        if not is_ct(device_type):
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
