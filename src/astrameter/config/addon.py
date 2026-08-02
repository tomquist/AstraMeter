"""Home Assistant add-on backend: settings read from the add-on's own config.

The add-on is not configured by a file. Its configuration is the JSON the
Supervisor writes to ``/data/options.json`` from the Configuration tab, plus
what the Supervisor itself knows (the MQTT service, this add-on's slug).
:class:`AddonAppConfig` reads exactly that and answers the app's
:class:`~astrameter.config.settings.AppConfig` interface — there is no
``config.ini`` involved, generated or otherwise, and no section/key names: an
option maps straight onto the settings field it configures.

All of this used to live in ``ha_addon/run.sh``, which rendered the options
into a ``config.ini`` with bashio before starting the app.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import replace
from ipaddress import IPv4Network
from typing import TYPE_CHECKING, Any, TypeVar, cast
from urllib.parse import urlparse

import requests

from astrameter.config.config_loader import (
    ClientFilter,
    apply_signal_wrappers,
    parse_float_list,
    parse_mqtt_uri,
)
from astrameter.config.ini_config import IniAppConfig
from astrameter.config.logger import logger
from astrameter.config.settings import (
    AppConfig,
    CtSettings,
    GeneralSettings,
    MarstekSettings,
)
from astrameter.powermeter import HomeAssistant

if TYPE_CHECKING:
    from astrameter.config.settings import ConfiguredPowermeter
    from astrameter.mqtt_insights import MqttInsightsConfig

OPTIONS_PATH = os.environ.get("ASTRAMETER_ADDON_OPTIONS", "/data/options.json")
"""Where the Supervisor stores the add-on's user options.

The three locations below are fixed inside a real add-on container. Each takes
an environment override so the add-on path can be driven from outside one — by
the browser E2E, which runs the app as a subprocess against a stand-in
Supervisor, and by anyone debugging the add-on backend on their own machine.
"""

ADDON_CONFIG_DIR = os.environ.get("ASTRAMETER_ADDON_CONFIG_DIR", "/config")
"""The ``addon_config`` mount that may hold a user-supplied config file."""

SUPERVISOR_BASE_URL = os.environ.get("ASTRAMETER_SUPERVISOR_URL", "http://supervisor")
"""Base URL of the Supervisor API inside the add-on container."""

REQUEST_TIMEOUT = 10.0

HOME_ASSISTANT_ATTEMPTS = 60
HOME_ASSISTANT_DELAY = 5.0

POWER_SOURCE_NAME = "HOMEASSISTANT"
"""Label of the add-on's single power source.

It also identifies the source in MQTT discovery, so it must stay stable —
it is not a config section.
"""

Options = dict[str, Any]

# Add-on option -> the settings field it configures. Options the user left
# untouched are skipped, so the settings defaults apply.
_GENERAL_FIELDS: dict[str, str] = {
    "dedupe_time_window": "dedupe_time_window",
    "dashboard_allow_write": "dashboard_allow_write",
    "dashboard_direct_access": "dashboard_direct_access",
}

_GLOBAL_SIGNAL_FIELDS: dict[str, str] = {
    "throttle_interval": "throttle_interval",
}

_SOURCE_SIGNAL_FIELDS: dict[str, str] = {
    "wait_for_next_message": "wait_for_next_message",
    "smooth_alpha": "smooth_target_alpha",
    "max_smooth_step": "max_smooth_step",
    "deadband": "deadband",
    "hampel_window": "hampel_window",
    "hampel_n_sigma": "hampel_n_sigma",
    "hampel_min_threshold": "hampel_min_threshold",
    "pid_kp": "pid_kp",
    "pid_ki": "pid_ki",
    "pid_kd": "pid_kd",
    "pid_output_max": "pid_output_max",
    "pid_mode": "pid_mode",
}

_CT_FIELDS: dict[str, str] = {
    "ct_mac": "ct_mac",
    "active_control": "active_control",
    "min_efficient_power": "min_efficient_power",
    "efficiency_rotation_interval": "efficiency_rotation_interval",
    "min_dc_output": "min_dc_output",
    "grid_predict_trust": "grid_predict_trust",
    # Balancer / active-control tuning.
    "fair_distribution": "fair_distribution",
    "balance_gain": "balance_gain",
    "balance_deadband": "balance_deadband",
    "max_correction_per_step": "max_correction_per_step",
    "error_boost_threshold": "error_boost_threshold",
    "error_boost_max": "error_boost_max",
    "error_reduce_threshold": "error_reduce_threshold",
    "max_target_step": "max_target_step",
    "pace_base_step": "pace_base_step",
    "pace_max_step": "pace_max_step",
    "osc_damp_max": "osc_damp_max",
    "osc_damp_alpha": "osc_damp_alpha",
    "osc_damp_decay": "osc_damp_decay",
    "osc_damp_threshold": "osc_damp_threshold",
    "concentrate_deadband": "concentrate_deadband",
    "import_trim_w": "import_trim_w",
    # Opt-in HTTP cloud reporting (hamedata.com).
    "cloud_reporting": "cloud_reporting",
    "cloud_reporting_host": "cloud_reporting_host",
    "cloud_reporting_interval": "cloud_reporting_interval",
}

_MARSTEK_FIELDS: dict[str, str] = {
    "mailbox": "marstek_mailbox",
    "password": "marstek_password",
}

# Options a custom config file takes over.
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

SettingsT = TypeVar("SettingsT")


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
    values all count as unset, so an untouched optional field keeps whatever
    default the settings dataclass declares.
    """
    value = options.get(key)
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return value


def _coerce(value: Any, default: Any) -> Any:
    """Fit an option value to the type of the settings field it feeds.

    The Supervisor already hands out real JSON types (the add-on schema says
    ``bool`` / ``int`` / ``float`` / ``str``), so this only bridges the
    harmless mismatches, e.g. an integer where a float is expected.
    """
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value)
    if isinstance(default, float):
        return float(value)
    if isinstance(default, str):
        return str(value).strip()
    return value


def _apply_options(
    settings: SettingsT, options: Options, fields: dict[str, str]
) -> SettingsT:
    """Overlay the options the user set onto *settings*."""
    values: dict[str, Any] = {}
    for name, option_key in fields.items():
        value = get_option(options, option_key)
        if value is not None:
            values[name] = _coerce(value, getattr(settings, name))
    if not values:
        return settings
    return cast("SettingsT", replace(cast("Any", settings), **values))


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


class AddonAppConfig(AppConfig):
    """Settings taken from the Home Assistant add-on options."""

    def __init__(
        self, options: Options, supervisor: SupervisorClient | None = None
    ) -> None:
        self._options = options
        self._supervisor = SupervisorClient() if supervisor is None else supervisor
        self._service: dict[str, Any] | None = None
        self._service_resolved = False
        self._slug: str | None = None

    def _option(self, key: str, default: Any = None) -> Any:
        return get_option(self._options, key, default)

    def _mqtt_service(self) -> dict[str, Any] | None:
        """Home Assistant's MQTT broker, asked for once and remembered."""
        if not self._service_resolved:
            self._service_resolved = True
            self._service = self._supervisor.mqtt_service()
        return self._service

    def _addon_slug(self) -> str:
        if self._slug is None:
            self._slug = self._supervisor.addon_slug()
        return self._slug

    def prefetch(self) -> None:
        """Ask the Supervisor for the MQTT broker and the add-on slug now.

        Both are HTTP round trips behind a blocking client, and both are
        remembered, so resolving them before the event loop starts keeps them
        off it.
        """
        self._mqtt_service()
        self._addon_slug()

    def general(self) -> GeneralSettings:
        defaults = GeneralSettings()
        device_types = [
            device_type.strip()
            for device_type in str(self._option("device_types", "")).split(",")
            if device_type.strip()
        ]
        general = replace(
            defaults,
            device_types=device_types or defaults.device_types,
            # The add-on panel links to the built-in web UI, so it is always
            # served; its config editor is for config files only.
            enable_web_server=True,
            web_config_enabled=False,
            # The add-on's sidebar panel *is* the dashboard, so there is no
            # option to turn it off: doing so would leave a panel that opens
            # onto nothing. Unlike a bare Docker run it costs nothing to serve
            # either, because Home Assistant authenticates every ingress
            # request. Writing is on by default and can be turned off.
            dashboard=True,
            dashboard_allow_write=True,
            signal=_apply_options(
                defaults.signal, self._options, _GLOBAL_SIGNAL_FIELDS
            ),
        )
        return _apply_options(general, self._options, _GENERAL_FIELDS)

    def ct(self, device_type: str) -> CtSettings:
        # The add-on configures one CT emulator; when both are enabled they
        # share these settings.
        settings = replace(
            CtSettings(), dedupe_time_window=self.general().dedupe_time_window
        )
        return _apply_options(settings, self._options, _CT_FIELDS)

    def marstek(self) -> MarstekSettings:
        # Without the auto-register opt-in the account is not used at all, so
        # the credentials are not handed out either.
        if not self._option("marstek_auto_register_ct_device"):
            return MarstekSettings()
        settings = _apply_options(MarstekSettings(), self._options, _MARSTEK_FIELDS)
        return replace(settings, enable=bool(settings.mailbox and settings.password))

    def mqtt_insights(self) -> MqttInsightsConfig | None:
        from astrameter.mqtt_insights import MqttInsightsConfig

        uri = self._option("mqtt_uri")
        if uri is not None:
            logger.info("Using custom MQTT broker URL from configuration")
            parts = parse_mqtt_uri(str(uri))
            broker, port = parts.host, parts.port
            username, password, tls = parts.username, parts.password, parts.tls
        else:
            service = self._mqtt_service()
            if service is None:
                logger.info(
                    "No MQTT broker configured and none provided by Home Assistant; "
                    "MQTT insights disabled"
                )
                return None
            logger.info("Using Home Assistant's internal MQTT broker")
            broker = str(service.get("host", ""))
            port = int(service.get("port") or 1883)
            username = str(service.get("username") or "") or None
            password = str(service.get("password") or "") or None
            tls = bool(service.get("ssl", False))

        return MqttInsightsConfig(
            broker=broker,
            port=port,
            username=username,
            password=password,
            tls=tls,
            ha_discovery=True,
            addon_slug=self._addon_slug() or None,
        )

    def powermeters(self, general: GeneralSettings) -> list[ConfiguredPowermeter]:
        """The add-on's single power source: Home Assistant sensors.

        They are read through the Supervisor proxy, authenticated with the
        add-on's own token.
        """
        power_input = self._entities("power_input_alias")
        power_output = self._entities("power_output_alias")
        calculated = bool(power_output)
        # Same host as the REST calls, so there is one definition of where the
        # Supervisor is (and tests can point both at a stand-in).
        supervisor = urlparse(self._supervisor.base_url)
        meter = HomeAssistant(
            supervisor.hostname or "supervisor",
            str(supervisor.port or 80),
            False,
            lambda: os.environ.get("SUPERVISOR_TOKEN", ""),
            current_power_entity=[] if calculated else power_input,
            power_calculate=calculated,
            power_input_alias=power_input if calculated else [],
            power_output_alias=power_output,
            path_prefix="/core",
        )

        signal = _apply_options(general.signal, self._options, _SOURCE_SIGNAL_FIELDS)
        signal = replace(
            signal,
            offsets=self._float_list("power_offset"),
            multipliers=self._float_list("power_multiplier"),
        )
        return [
            (
                apply_signal_wrappers(meter, POWER_SOURCE_NAME, signal),
                # The add-on serves whichever battery asks.
                ClientFilter([IPv4Network("0.0.0.0/0")]),
                signal.wait_for_next_message,
            )
        ]

    def render_powermeters_ini(self) -> str:
        """``[HOMEASSISTANT]`` matching what :meth:`powermeters` builds.

        Mirrors that method key for key. The two are pinned together by
        ``addon_test.py``; when power sources grow a settings type this can be
        rendered generically and both this and the hook go away.
        """
        power_input = self._entities("power_input_alias")
        power_output = self._entities("power_output_alias")
        supervisor = urlparse(self._supervisor.base_url)
        lines = [
            "[HOMEASSISTANT]",
            f"IP = {supervisor.hostname or 'supervisor'}",
            f"PORT = {supervisor.port or 80}",
            "API_PATH_PREFIX = /core",
            "# The add-on authenticates with its own Supervisor token; a standalone",
            "# install needs a long-lived access token here instead.",
            "ACCESSTOKEN =",
        ]
        if power_output:
            lines += [
                "POWER_CALCULATE = True",
                f"POWER_INPUT_ALIAS = {', '.join(power_input)}",
                f"POWER_OUTPUT_ALIAS = {', '.join(power_output)}",
            ]
        else:
            lines += [
                "POWER_CALCULATE = False",
                f"CURRENT_POWER_ENTITY = {', '.join(power_input)}",
            ]
        for key in ("power_offset", "power_multiplier"):
            value = self._option(key)
            if value not in (None, ""):
                lines.append(f"{key.upper()} = {value}")
        return "\n".join(lines) + "\n"

    def _entities(self, key: str) -> list[str]:
        """Entity ids of an option; one per phase for three-phase setups."""
        raw = self._option(key, "")
        return [entity.strip() for entity in str(raw).split(",") if entity.strip()]

    def _float_list(self, key: str) -> list[float] | None:
        value = self._option(key)
        if value is None:
            return None
        return parse_float_list(str(value), key, "add-on options")


def custom_config_path(
    options: Options, config_dir: str = ADDON_CONFIG_DIR
) -> str | None:
    """Path of the user's own config file, when they configured a usable one.

    The option is a file name inside the add-on's config mount, so anything
    that resolves outside it — an absolute path, or one climbing out with
    ``..`` — is refused rather than read from somewhere else on the host.
    """
    name = get_option(options, "custom_config")
    if name is None:
        return None
    base = os.path.realpath(config_dir)
    path = os.path.realpath(os.path.join(base, str(name).strip()))
    if base != path and not path.startswith(base + os.sep):
        logger.warning(
            "Custom config file '%s' resolves outside %s; using the add-on "
            "configuration options instead",
            name,
            config_dir,
        )
        return None
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
) -> AppConfig:
    """Pick the add-on's configuration source.

    The add-on options unless the user pointed the add-on at their own config
    file, in which case that file takes over completely.
    """
    path = custom_config_path(options, config_dir)
    if path is None:
        return AddonAppConfig(options, supervisor)

    logger.info("Using custom config file: %s", path)
    _warn_ignored_options(options)
    return IniAppConfig.from_file(path)
