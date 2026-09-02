"""``config.ini`` backend: settings read from a configuration file.

The one place that still knows about ``[SECTION]`` / ``KEY`` names when the app
is configured by file. Other backends (e.g. the Home Assistant add-on) read
their own source and answer the same :class:`AppConfig` interface.
"""

from __future__ import annotations

import configparser
import dataclasses
import typing
from typing import TYPE_CHECKING, Any

from astrameter.config.config_loader import (
    new_config_parser,
    read_all_powermeter_configs,
    read_mqtt_insights_config,
    read_option,
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

# A field's INI key is its name uppercased, except for these. The reader and
# the renderer below share the maps, and `ini_config_test.py` round-trips every
# field, so a key read one way and written another fails there.
_GENERAL_KEY_OVERRIDES = {
    "device_types": "DEVICE_TYPE",
    "dashboard": "DASHBOARD_ENABLED",
}

_SIGNAL_KEY_OVERRIDES = {
    "smooth_alpha": "SMOOTH_TARGET_ALPHA",
    "offsets": "POWER_OFFSET",
    "multipliers": "POWER_MULTIPLIER",
}


def _ini_key(field: str, overrides: dict[str, str]) -> str:
    return overrides.get(field, field.upper())


def general_key(field: str) -> str:
    """The ``[GENERAL]`` key backing *field* of :class:`GeneralSettings`.

    Lets another backend name a key in a message without hardcoding it, so a
    rename here cannot leave that message pointing at a key nobody reads.
    """
    return _ini_key(field, _GENERAL_KEY_OVERRIDES)


def _field_kinds(settings_type: type) -> dict[str, type]:
    """Each field's scalar type — ``int`` for both ``int`` and ``int | None``."""
    kinds = {}
    for name, hint in typing.get_type_hints(settings_type).items():
        members = [t for t in typing.get_args(hint) or (hint,) if t is not type(None)]
        kinds[name] = members[0]
    return kinds


def _read_fields(
    config: configparser.ConfigParser,
    section: str,
    settings_type: type[Any],
    key_overrides: dict[str, str] | None = None,
    *,
    skip: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Every field of *settings_type* but *skip*, parsed by the getter its type asks for.

    An absent key yields the dataclass default, so a field typed ``X | None``
    stays ``None`` — unset ``web_config_enabled`` is not "off", and unset
    ``consumer_ttl`` is adaptive rather than a number.
    """
    defaults = settings_type()
    kinds = _field_kinds(settings_type)
    values = {}
    for f in dataclasses.fields(settings_type):
        if f.name in skip:
            continue
        key = _ini_key(f.name, key_overrides or {})
        values[f.name] = read_option(
            config, section, key, kinds[f.name], getattr(defaults, f.name)
        )
    return values


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
            device_types=_split(
                config.get(GENERAL_SECTION, general_key("device_types"), fallback="")
            )
            or defaults.device_types,
            device_ids=_split(config.get(GENERAL_SECTION, "DEVICE_IDS", fallback="")),
            signal=read_signal_settings(
                GENERAL_SECTION, config, SignalSettings(), with_transform=False
            ),
            **_read_fields(
                config,
                GENERAL_SECTION,
                GeneralSettings,
                _GENERAL_KEY_OVERRIDES,
                skip=("device_types", "device_ids", "signal"),
            ),
        )

    def ct(self, device_type: str) -> CtSettings:
        config = self._config
        defaults = CtSettings()
        section = self.ct_section(device_type)
        # An emulator without a window of its own inherits [GENERAL]'s.
        global_dedupe = config.getfloat(
            GENERAL_SECTION, "DEDUPE_TIME_WINDOW", fallback=defaults.dedupe_time_window
        )
        # A blank host means the default, not "no host".
        host = config.get(section, "CLOUD_REPORTING_HOST", fallback="").strip()
        return CtSettings(
            dedupe_time_window=config.getfloat(
                section, "DEDUPE_TIME_WINDOW", fallback=global_dedupe
            ),
            cloud_reporting_host=host or defaults.cloud_reporting_host,
            **_read_fields(
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
            **_read_fields(self._config, MARSTEK_SECTION, MarstekSettings)
        )

    def mqtt_insights(self) -> MqttInsightsConfig | None:
        return read_mqtt_insights_config(self._config)

    def powermeters(self, general: GeneralSettings) -> list[ConfiguredPowermeter]:
        return read_all_powermeter_configs(self._config, general.signal)


def _split(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


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
        out.append((_ini_key(f.name, overrides), _render_value(value)))
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
