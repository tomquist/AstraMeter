"""Home Assistant add-on configuration backend.

Running with ``--addon`` swaps the ``config.ini`` file backend for the add-on's
own configuration sources: the user's options in ``/data/options.json`` and the
Supervisor API (MQTT credentials, the add-on slug, Home Assistant readiness).

:class:`AddonConfig` is that backend. It is not a converted or pre-rendered
config — nothing is written out and no values are copied up front. It answers
the same read API the rest of the app already uses (``get`` / ``getboolean`` /
``has_section`` / ``sections`` ...) by looking the requested ``[SECTION] KEY``
up in the add-on options at the moment it is asked, through the name maps
below.

All of this used to live in ``ha_addon/run.sh`` as bashio shell code that
rendered an intermediate ``config.ini``.
"""

from __future__ import annotations

import json
import os
import time
from collections import OrderedDict
from collections.abc import Callable
from configparser import ConfigParser, NoOptionError, NoSectionError
from typing import Any

import requests

from astrameter.config.config_loader import new_config_parser
from astrameter.config.logger import logger

OPTIONS_PATH = "/data/options.json"
"""Where the Supervisor stores the add-on's user options."""

ADDON_CONFIG_DIR = "/config"
"""The ``addon_config`` mount that may hold a user-supplied config file."""

SUPERVISOR_BASE_URL = "http://supervisor"
"""Base URL of the Supervisor API inside the add-on container."""

REQUEST_TIMEOUT = 10.0

HOME_ASSISTANT_ATTEMPTS = 60
HOME_ASSISTANT_DELAY = 5.0

Options = dict[str, Any]

# ---------------------------------------------------------------------------
# Name maps: config key -> add-on option (or a fixed value the add-on implies).
# ---------------------------------------------------------------------------

_GENERAL_OPTIONS: dict[str, str] = {
    "DEVICE_TYPE": "device_types",
    "THROTTLE_INTERVAL": "throttle_interval",
    "DEDUPE_TIME_WINDOW": "dedupe_time_window",
}

# The add-on panel links to the built-in web UI, so it is always served.
_GENERAL_CONSTANTS: dict[str, Any] = {"ENABLE_WEB_SERVER": True}

_CT_OPTIONS: dict[str, str] = {
    "CT_MAC": "ct_mac",
    "ACTIVE_CONTROL": "active_control",
    "MIN_EFFICIENT_POWER": "min_efficient_power",
    "EFFICIENCY_ROTATION_INTERVAL": "efficiency_rotation_interval",
    "MIN_DC_OUTPUT": "min_dc_output",
    "GRID_PREDICT_TRUST": "grid_predict_trust",
    # Balancer / active-control tuning (mirrors the web config editor's
    # "balancer" group).
    "FAIR_DISTRIBUTION": "fair_distribution",
    "BALANCE_GAIN": "balance_gain",
    "BALANCE_DEADBAND": "balance_deadband",
    "MAX_CORRECTION_PER_STEP": "max_correction_per_step",
    "ERROR_BOOST_THRESHOLD": "error_boost_threshold",
    "ERROR_BOOST_MAX": "error_boost_max",
    "ERROR_REDUCE_THRESHOLD": "error_reduce_threshold",
    "MAX_TARGET_STEP": "max_target_step",
    "PACE_BASE_STEP": "pace_base_step",
    "PACE_MAX_STEP": "pace_max_step",
    "OSC_DAMP_MAX": "osc_damp_max",
    "OSC_DAMP_ALPHA": "osc_damp_alpha",
    "OSC_DAMP_DECAY": "osc_damp_decay",
    "OSC_DAMP_THRESHOLD": "osc_damp_threshold",
    "CONCENTRATE_DEADBAND": "concentrate_deadband",
    "IMPORT_TRIM_W": "import_trim_w",
    # Opt-in HTTP cloud reporting (hamedata.com).
    "CLOUD_REPORTING": "cloud_reporting",
    "CLOUD_REPORTING_HOST": "cloud_reporting_host",
    "CLOUD_REPORTING_INTERVAL": "cloud_reporting_interval",
}

# Home Assistant sensors are the add-on's power source, reached through the
# Supervisor proxy and authenticated with the add-on's own token.
_HOMEASSISTANT_CONSTANTS: dict[str, Any] = {
    "IP": "supervisor",
    "PORT": 80,
    "API_PATH_PREFIX": "/core",
}

_HOMEASSISTANT_OPTIONS: dict[str, str] = {
    "WAIT_FOR_NEXT_MESSAGE": "wait_for_next_message",
    "POWER_OFFSET": "power_offset",
    "POWER_MULTIPLIER": "power_multiplier",
    "SMOOTH_TARGET_ALPHA": "smooth_target_alpha",
    "MAX_SMOOTH_STEP": "max_smooth_step",
    "DEADBAND": "deadband",
    "HAMPEL_WINDOW": "hampel_window",
    "HAMPEL_N_SIGMA": "hampel_n_sigma",
    "HAMPEL_MIN_THRESHOLD": "hampel_min_threshold",
    "PID_KP": "pid_kp",
    "PID_KI": "pid_ki",
    "PID_KD": "pid_kd",
    "PID_OUTPUT_MAX": "pid_output_max",
    "PID_MODE": "pid_mode",
}

# Which entity keys the power source uses depends on whether the user gave a
# separate export entity, so these are resolved in code rather than by name.
_HOMEASSISTANT_POWER_KEYS = (
    "POWER_CALCULATE",
    "CURRENT_POWER_ENTITY",
    "POWER_INPUT_ALIAS",
    "POWER_OUTPUT_ALIAS",
)

_MARSTEK_CONSTANTS: dict[str, Any] = {
    "ENABLE": True,
    "BASE_URL": "https://eu.hamedata.com",
    "TIMEZONE": "Europe/Berlin",
}

_MARSTEK_OPTIONS: dict[str, str] = {
    "MAILBOX": "marstek_mailbox",
    "PASSWORD": "marstek_password",
}

# Home Assistant's own broker delivers these under different names.
_MQTT_SERVICE_KEYS: dict[str, str] = {
    "BROKER": "host",
    "PORT": "port",
    "USERNAME": "username",
    "PASSWORD": "password",
    "TLS": "ssl",
}

_MQTT_KEYS = ("URI", *_MQTT_SERVICE_KEYS, "HA_DISCOVERY", "ADDON_SLUG")

_CT_SECTIONS = ("CT002", "CT003")

# Options the add-on UI cannot honour once a custom config file takes over.
_IGNORED_WITH_CUSTOM_CONFIG: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("marstek_mailbox", "marstek_password", "marstek_auto_register_ct_device"),
        "App UI Marstek settings are ignored when custom_config is used; "
        "values from the custom config file take precedence",
    ),
    (
        ("mqtt_uri",),
        "App UI mqtt_uri is ignored when custom_config is used; the custom "
        "config file controls MQTT settings",
    ),
)

_UNSET = object()


def load_options(path: str = OPTIONS_PATH) -> Options:
    """Read the add-on options the Supervisor wrote for this run."""
    try:
        with open(path, encoding="utf-8") as handle:
            options = json.load(handle)
    except FileNotFoundError:
        logger.warning(
            "Add-on options file %s not found; falling back to built-in defaults",
            path,
        )
        return {}
    except (OSError, ValueError) as exc:
        logger.error("Cannot read add-on options from %s: %s", path, exc)
        return {}
    if not isinstance(options, dict):
        logger.error("Add-on options in %s are not a JSON object; ignoring them", path)
        return {}
    return options


def get_option(options: Options, key: str, default: Any = None) -> Any:
    """Return *key* only when the user actually set it, else *default*.

    Mirrors ``bashio::config.has_value``: missing, ``null`` and empty-string
    values all count as unset, so an untouched optional field reads as absent
    and the app's own default applies.
    """
    value = options.get(key)
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return value


def _format(value: Any) -> str:
    """Render an option value the way the config API hands values out: as text."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


class SupervisorClient:
    """The handful of Supervisor API calls the add-on needs (bashio's job)."""

    def __init__(
        self,
        base_url: str = SUPERVISOR_BASE_URL,
        token: str | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = os.environ.get("SUPERVISOR_TOKEN", "") if token is None else token
        self.timeout = timeout

    def _get(self, path: str) -> requests.Response | None:
        try:
            return requests.get(
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            logger.debug("Supervisor request %s failed: %s", path, exc, exc_info=False)
            return None

    def _get_data(self, path: str) -> dict[str, Any] | None:
        """GET *path* and unwrap the Supervisor's ``{"result", "data"}`` body."""
        response = self._get(path)
        if response is None:
            return None
        if response.status_code != 200:
            logger.debug(
                "Supervisor request %s returned HTTP %d", path, response.status_code
            )
            return None
        try:
            payload = response.json()
        except ValueError:
            logger.debug("Supervisor request %s returned a non-JSON body", path)
            return None
        if not isinstance(payload, dict) or payload.get("result") != "ok":
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def addon_slug(self) -> str:
        """This add-on's slug, used to link discovered meters via ``via_device``."""
        data = self._get_data("/addons/self/info") or {}
        slug = str(data.get("slug") or "").strip()
        if slug:
            logger.info("Resolved add-on slug for HA discovery: %s", slug)
        else:
            logger.warning(
                "Failed to resolve add-on slug from supervisor; meter devices "
                "will not be linked via_device"
            )
        return slug

    def mqtt_service(self) -> dict[str, Any] | None:
        """Credentials of Home Assistant's own MQTT broker, if one is offered."""
        data = self._get_data("/services/mqtt")
        if not data or not data.get("host"):
            return None
        return data

    def home_assistant_ready(self) -> bool:
        response = self._get("/core/api/")
        return response is not None and response.status_code == 200


def wait_for_home_assistant(
    supervisor: SupervisorClient,
    attempts: int = HOME_ASSISTANT_ATTEMPTS,
    delay: float = HOME_ASSISTANT_DELAY,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Block until the Home Assistant core API answers, or the attempts run out.

    A timeout is not fatal: the power source retries on its own, so we log and
    let the app start anyway.
    """
    logger.info("Waiting for Home Assistant to be ready...")
    for attempt in range(1, attempts + 1):
        if supervisor.home_assistant_ready():
            logger.info(
                "Home Assistant is ready! Proceeding with AstraMeter startup..."
            )
            return True
        logger.debug(
            "Home Assistant not ready yet (attempt %d/%d), waiting %.0f seconds...",
            attempt,
            attempts,
            delay,
        )
        sleep(delay)
    logger.warning(
        "Home Assistant may not be fully ready after %.0f seconds, but continuing anyway...",
        attempts * delay,
    )
    return False


class AddonConfig(ConfigParser):
    """The add-on options, read through the app's configuration API.

    Every lookup is answered from the live options (and, for MQTT, from the
    Supervisor) — there is no generated config file or copied-over set of
    values behind this. Values assigned at runtime (CLI overrides via ``set``)
    are kept in the parser's own storage and take precedence.

    Reads must use the classic API (``get`` / ``getint`` / ``getboolean`` /
    ``has_section`` / ``has_option`` / ``sections`` / ``options``), which is
    what the app uses throughout; the mapping protocol (``cfg[section]``,
    ``items()``) sees the runtime overrides only.
    """

    def __init__(
        self, options: Options, supervisor: SupervisorClient | None = None
    ) -> None:
        super().__init__(dict_type=OrderedDict, interpolation=None)
        self._options = options
        self._supervisor = SupervisorClient() if supervisor is None else supervisor
        self._mqtt_service: dict[str, Any] | None = None
        self._mqtt_resolved = False
        self._addon_slug: str | None = None

    # -- configuration sources ------------------------------------------

    def _option(self, key: str) -> Any:
        return get_option(self._options, key)

    def _service(self) -> dict[str, Any] | None:
        """Home Assistant's MQTT broker, asked for once and remembered."""
        if not self._mqtt_resolved:
            self._mqtt_resolved = True
            self._mqtt_service = self._supervisor.mqtt_service()
            if self._mqtt_service is not None:
                logger.info("Using Home Assistant's internal MQTT broker")
        return self._mqtt_service

    def _slug(self) -> str:
        if self._addon_slug is None:
            self._addon_slug = self._supervisor.addon_slug()
        return self._addon_slug

    # -- section layout --------------------------------------------------

    def _ct_sections(self) -> tuple[str, ...]:
        """CT sections the configured device types imply."""
        device_types = str(self._option("device_types") or "").lower()
        has_ct002 = "ct002" in device_types
        has_ct003 = "ct003" in device_types
        if has_ct003 and not has_ct002:
            return ("CT003",)
        if has_ct002 and has_ct003:
            return _CT_SECTIONS
        return ("CT002",)

    def _marstek_enabled(self) -> bool:
        """Marstek credentials are only used with the auto-register opt-in."""
        return bool(
            self._option("marstek_auto_register_ct_device")
            and self._option("marstek_mailbox")
            and self._option("marstek_password")
        )

    def _mqtt_enabled(self) -> bool:
        if self._option("mqtt_uri") is not None:
            return True
        return self._service() is not None

    def _addon_sections(self) -> list[str]:
        sections = ["GENERAL", *self._ct_sections()]
        if self._marstek_enabled():
            sections.append("MARSTEK")
        sections.append("HOMEASSISTANT")
        if self._mqtt_enabled():
            sections.append("MQTT_INSIGHTS")
        return sections

    def _keys(self, section: str) -> list[str]:
        """Keys *section* can answer (not all of them are always set)."""
        if section == "GENERAL":
            return [*_GENERAL_CONSTANTS, *_GENERAL_OPTIONS]
        if section in self._ct_sections():
            return list(_CT_OPTIONS)
        if section == "HOMEASSISTANT":
            return [
                *_HOMEASSISTANT_CONSTANTS,
                *_HOMEASSISTANT_POWER_KEYS,
                *_HOMEASSISTANT_OPTIONS,
            ]
        if section == "MARSTEK" and self._marstek_enabled():
            return [*_MARSTEK_CONSTANTS, *_MARSTEK_OPTIONS]
        if section == "MQTT_INSIGHTS" and self._mqtt_enabled():
            return list(_MQTT_KEYS)
        return []

    # -- value lookup ----------------------------------------------------

    def _from_maps(
        self, key: str, constants: dict[str, Any], option_keys: dict[str, str]
    ) -> str | None:
        if key in constants:
            return _format(constants[key])
        option_key = option_keys.get(key)
        if option_key is None:
            return None
        value = self._option(option_key)
        return None if value is None else _format(value)

    def _power_source(self, key: str) -> str | None:
        """[HOMEASSISTANT] — one entity, or an import/export pair to combine."""
        power_output = self._option("power_output_alias")
        calculated = power_output is not None
        power_input = _format(self._option("power_input_alias") or "")
        if key == "POWER_CALCULATE":
            return _format(calculated)
        if key == "POWER_OUTPUT_ALIAS":
            return _format(power_output) if calculated else None
        if key == "POWER_INPUT_ALIAS":
            return power_input if calculated else None
        if key == "CURRENT_POWER_ENTITY":
            return None if calculated else power_input
        return self._from_maps(key, _HOMEASSISTANT_CONSTANTS, _HOMEASSISTANT_OPTIONS)

    def _mqtt(self, key: str) -> str | None:
        """[MQTT_INSIGHTS] — a user-supplied broker URI wins over HA's broker."""
        uri = self._option("mqtt_uri")
        if uri is not None:
            if key == "URI":
                return _format(uri)
        else:
            service = self._service()
            if service is None:
                return None
            if key in _MQTT_SERVICE_KEYS:
                value = service.get(_MQTT_SERVICE_KEYS[key])
                return None if value is None else _format(value)
        if key == "HA_DISCOVERY":
            return _format(True)
        if key == "ADDON_SLUG":
            return self._slug() or None
        return None

    def _lookup(self, section: str, option: str) -> str | None:
        """Resolve ``[section] option`` against the add-on options.

        Section names are matched exactly (as ConfigParser does), option names
        case-insensitively (as ConfigParser's ``optionxform`` does).
        """
        key = option.upper()
        if section == "GENERAL":
            return self._from_maps(key, _GENERAL_CONSTANTS, _GENERAL_OPTIONS)
        if section in self._ct_sections():
            return self._from_maps(key, {}, _CT_OPTIONS)
        if section == "HOMEASSISTANT":
            return self._power_source(key)
        if section == "MARSTEK":
            if not self._marstek_enabled():
                return None
            return self._from_maps(key, _MARSTEK_CONSTANTS, _MARSTEK_OPTIONS)
        if section == "MQTT_INSIGHTS":
            return self._mqtt(key)
        return None

    # -- ConfigParser read API -------------------------------------------

    def sections(self) -> list[str]:
        sections = self._addon_sections()
        sections.extend(s for s in super().sections() if s not in sections)
        return sections

    def has_section(self, section: str) -> bool:
        return section in self._addon_sections() or super().has_section(section)

    def options(self, section: str) -> list[str]:
        keys = [k for k in self._keys(section) if self._lookup(section, k) is not None]
        if super().has_section(section):
            keys.extend(k.upper() for k in super().options(section) if k not in keys)
        return keys

    def has_option(self, section: str, option: str) -> bool:
        if super().has_section(section) and super().has_option(section, option):
            return True
        return self._lookup(section, option) is not None

    def get(  # type: ignore[override]  # base method is overloaded on `fallback`
        self,
        section: str,
        option: str,
        *,
        raw: bool = False,
        vars: dict[str, str] | None = None,  # ConfigParser's parameter name
        fallback: Any = _UNSET,
    ) -> Any:
        if super().has_section(section):
            value = super().get(section, option, raw=raw, vars=vars, fallback=_UNSET)
            if value is not _UNSET:
                return value
        value = self._lookup(section, option)
        if value is not None:
            return value
        if fallback is _UNSET:
            if not self.has_section(section):
                raise NoSectionError(section)
            raise NoOptionError(option, section)
        return fallback

    def set(self, section: str, option: str, value: str | None = None) -> None:
        """Runtime overrides (CLI flags) win over the add-on options."""
        if not super().has_section(section):
            super().add_section(section)
        super().set(section, option, value)


def custom_config_path(
    options: Options, config_dir: str = ADDON_CONFIG_DIR
) -> str | None:
    """Path of the user's own config file, when they configured a usable one."""
    name = get_option(options, "custom_config")
    if name is None:
        return None
    path = os.path.join(config_dir, str(name).strip())
    if not os.path.isfile(path):
        logger.warning(
            "Custom config file '%s' not found in %s; using the add-on "
            "configuration options instead",
            name,
            config_dir,
        )
        return None
    return path


def _warn_ignored_options(options: Options) -> None:
    for keys, message in _IGNORED_WITH_CUSTOM_CONFIG:
        if any(get_option(options, key) is not None for key in keys):
            logger.warning(message)


def load_config(
    options: Options,
    supervisor: SupervisorClient | None = None,
    config_dir: str = ADDON_CONFIG_DIR,
) -> tuple[ConfigParser, str | None]:
    """Return the add-on's configuration plus the file it came from.

    The path is ``None`` unless the user pointed the add-on at their own config
    file — the options themselves are not backed by a file the web UI could
    show or edit; the add-on's Configuration tab owns them.
    """
    path = custom_config_path(options, config_dir)
    if path is None:
        return AddonConfig(options, supervisor), None

    logger.info("Using custom config file: %s", path)
    _warn_ignored_options(options)
    cfg = new_config_parser()
    cfg.read(path)
    return cfg, path


def log_config(cfg: ConfigParser) -> None:
    """Log the effective configuration (the logger masks the credentials)."""
    lines: list[str] = []
    for section in cfg.sections():
        lines.append(f"[{section}]")
        lines.extend(f"{key} = {cfg.get(section, key)}" for key in cfg.options(section))
        lines.append("")
    logger.info("Effective configuration:\n%s", "\n".join(lines).strip())
