from __future__ import annotations

import configparser
import dataclasses
import os
import typing
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from astrameter.mqtt_insights import MqttInsightsConfig

from astrameter.config.logger import logger
from astrameter.config.settings import ConfiguredPowermeter, SignalSettings
from astrameter.powermeter import (
    AmisReader,
    Emlog,
    Envoy,
    ESPHome,
    ESPHomeNative,
    FritzSmartEnergy,
    Fronius,
    HomeAssistant,
    HomeWizardPowermeter,
    IoBroker,
    JsonHttpPowermeter,
    ModbusPowermeter,
    MqttPowermeter,
    PidPowermeter,
    Powermeter,
    Refoss,
    Script,
    Shelly1PM,
    Shelly3EMPro,
    ShellyEM,
    ShellyPlus1PM,
    Shrdzm,
    SmaEnergyMeter,
    Sml,
    Tasmota,
    ThrottledPowermeter,
    TibberPulse,
    TQEnergyManager,
    TransformedPowermeter,
    VZLogger,
    parse_sml_obis_config,
)
from astrameter.powermeter.wrappers.hampel import HampelPowermeter
from astrameter.powermeter.wrappers.health import HealthTrackingPowermeter
from astrameter.powermeter.wrappers.smoothing import (
    DeadbandPowermeter,
    SmoothedPowermeter,
)

SHELLY_SECTION = "SHELLY"
TASMOTA_SECTION = "TASMOTA"
SHRDZM_SECTION = "SHRDZM"
EMLOG_SECTION = "EMLOG"
IOBROKER_SECTION = "IOBROKER"
HOMEASSISTANT_SECTION = "HOMEASSISTANT"
VZLOGGER_SECTION = "VZLOGGER"
SCRIPT_SECTION = "SCRIPT"
SML_SECTION = "SML"
ESPHOME_SECTION = "ESPHOME"
ESPHOMENATIVE_SECTION = "ESPHOMENATIVE"
AMIS_READER_SECTION = "AMIS_READER"
MODBUS_SECTION = "MODBUS"
JSON_HTTP_SECTION = "JSON_HTTP"
TQ_EM_SECTION = "TQ_EM"
HOMEWIZARD_SECTION = "HOMEWIZARD"
ENVOY_SECTION = "ENVOY"
SMA_ENERGY_METER_SECTION = "SMA_ENERGY_METER"
FRITZ_SECTION = "FRITZ"
FRONIUS_SECTION = "FRONIUS"
REFOSS_SECTION = "REFOSS"
MEROSS_SECTION = "MEROSS"
TIBBER_PULSE_SECTION = "TIBBER_PULSE"
MQTT_SECTION = "MQTT"
MQTT_INSIGHTS_SECTION = "MQTT_INSIGHTS"

DEFAULT_MQTT_PORT = 1883
DEFAULT_MQTTS_PORT = 8883

_GETTERS: dict[type, str] = {
    bool: "getboolean",
    int: "getint",
    float: "getfloat",
    str: "get",
}


def read_option(
    config: configparser.ConfigParser,
    section: str,
    key: str,
    kind: type,
    fallback: Any = None,
) -> Any:
    """*key* through the configparser getter for *kind*.

    One place picks the getter, so ``yes``/``no`` booleans and integers that
    refuse ``1.5`` parse the same in every backend.
    """
    return getattr(config, _GETTERS[kind])(section, key, fallback=fallback)


def _declared(
    config: configparser.ConfigParser,
    section: str,
    *,
    blank_is_unset: bool = False,
    **keys: tuple[str, type],
) -> dict[str, Any]:
    """Keyword arguments for the keys *section* sets; the class default covers the rest.

    ``blank_is_unset`` also skips a key written with no value, so
    ``HA_DISCOVERY =`` means "leave it to the class" rather than a parse
    error or a zero.
    """
    out: dict[str, Any] = {}
    for arg, (key, kind) in keys.items():
        if not config.has_option(section, key):
            continue
        if blank_is_unset and not config.get(section, key, fallback="").strip():
            continue
        out[arg] = read_option(config, section, key, kind)
    return out


# A field's INI key is its name uppercased, except for these. Readers and the
# renderer in ``ini_config`` share the maps, and ``ini_config_test.py``
# round-trips every field, so a key read one way and written another fails there.
GENERAL_KEY_OVERRIDES = {
    "device_types": "DEVICE_TYPE",
    "dashboard": "DASHBOARD_ENABLED",
}

SIGNAL_KEY_OVERRIDES = {
    "smooth_alpha": "SMOOTH_TARGET_ALPHA",
    "offsets": "POWER_OFFSET",
    "multipliers": "POWER_MULTIPLIER",
}


def ini_key(field: str, overrides: dict[str, str]) -> str:
    return overrides.get(field, field.upper())


def general_key(field: str) -> str:
    """The ``[GENERAL]`` key backing *field* of :class:`GeneralSettings`.

    Lets another backend name a key in a message without hardcoding it, so a
    rename here cannot leave that message pointing at a key nobody reads.
    """
    return ini_key(field, GENERAL_KEY_OVERRIDES)


def _field_kinds(settings_type: type) -> dict[str, type]:
    """Each field's scalar type — ``int`` for both ``int`` and ``int | None``."""
    kinds = {}
    for name, hint in typing.get_type_hints(settings_type).items():
        members = [t for t in typing.get_args(hint) or (hint,) if t is not type(None)]
        kinds[name] = members[0]
    return kinds


def read_fields(
    config: configparser.ConfigParser,
    section: str,
    settings_type: type[Any],
    key_overrides: dict[str, str] | None = None,
    *,
    skip: tuple[str, ...] = (),
    defaults: Any = None,
) -> dict[str, Any]:
    """Every field of *settings_type* but *skip*, parsed by the getter its type asks for.

    An absent key yields the dataclass default, so a field typed ``X | None``
    stays ``None`` — unset ``web_config_enabled`` is not "off", and unset
    ``consumer_ttl`` is adaptive rather than a number. *defaults* overrides
    those per-field fallbacks with another instance's values, which is how a
    section inherits what ``[GENERAL]`` was configured with.
    """
    if defaults is None:
        defaults = settings_type()
    kinds = _field_kinds(settings_type)
    values = {}
    for f in dataclasses.fields(settings_type):
        if f.name in skip:
            continue
        key = ini_key(f.name, key_overrides or {})
        values[f.name] = read_option(
            config, section, key, kinds[f.name], getattr(defaults, f.name)
        )
    return values


def split_csv(raw: str) -> list[str]:
    """The comma-separated items of *raw*, trimmed, blanks dropped."""
    return [item.strip() for item in raw.split(",") if item.strip()]


def one_or_many(raw: str) -> str | list[str]:
    """Like :func:`split_csv`, but a lone item stays a plain string.

    Power sources take either shape and treat a list as one value per phase,
    so a single-entry list would not mean the same thing as the entry itself.
    """
    parts = split_csv(raw)
    return parts[0] if len(parts) == 1 else parts


def new_config_parser() -> configparser.ConfigParser:
    """Parser used for every config backend.

    Interpolation is disabled so a literal ``%`` in a credential (e.g.
    ``MARSTEK.PASSWORD``) is read as-is.
    """
    return configparser.ConfigParser(dict_type=OrderedDict, interpolation=None)


class ClientFilter:
    def __init__(self, netmasks: list[IPv4Network]):
        self.netmasks = netmasks

    def matches(self, client_ip) -> bool:
        try:
            client_ip_addr = IPv4Address(client_ip)
            for netmask in self.netmasks:
                if client_ip_addr in netmask:
                    return True
        except ValueError:
            logger.error(
                "Client %r is not an IPv4 address; it matches none of %s",
                client_ip,
                [str(netmask) for netmask in self.netmasks],
            )
            return False
        return False


@dataclass
class MqttUriParts:
    """Parsed connection details from an MQTT URI."""

    host: str
    port: int
    username: str | None
    password: str | None
    tls: bool


def parse_mqtt_uri(uri: str) -> MqttUriParts:
    """Parse an ``mqtt://`` / ``mqtts://`` URI into its connection parts.

    Accepts URIs of the form ``mqtt[s]://[user[:pass]@]host[:port]``. Username
    and password may be percent-encoded. Raises ``ValueError`` if the URI is
    not a valid MQTT URI.
    """

    raw = (uri or "").strip()
    if not raw:
        raise ValueError("MQTT URI is empty")

    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme not in ("mqtt", "mqtts"):
        raise ValueError(
            f"Unsupported MQTT URI scheme '{parsed.scheme}'; expected 'mqtt' or 'mqtts'"
        )

    host = parsed.hostname
    if not host:
        raise ValueError(f"MQTT URI is missing a host: {uri!r}")

    if parsed.path not in ("", "/"):
        raise ValueError(f"MQTT URI must not contain a path: {uri!r}")
    if parsed.params or parsed.query or parsed.fragment:
        raise ValueError(
            f"MQTT URI must not contain params, query, or fragment: {uri!r}"
        )

    tls = scheme == "mqtts"
    port = parsed.port or (DEFAULT_MQTTS_PORT if tls else DEFAULT_MQTT_PORT)
    username = unquote(parsed.username) if parsed.username is not None else None
    password = unquote(parsed.password) if parsed.password is not None else None

    return MqttUriParts(
        host=host,
        port=port,
        username=username,
        password=password,
        tls=tls,
    )


def read_mqtt_connection(
    section: str, config: configparser.ConfigParser
) -> MqttUriParts:
    """The broker a section points at: its ``URI``, else the separate keys.

    A blank key counts as unset, so ``PORT =`` gets the default port rather
    than a parse error.
    """
    uri = config.get(section, "URI", fallback="").strip()
    if uri:
        return parse_mqtt_uri(uri)
    port = config.get(section, "PORT", fallback="").strip()
    tls = config.get(section, "TLS", fallback="").strip()
    return MqttUriParts(
        host=config.get(section, "BROKER", fallback=""),
        port=int(port) if port else DEFAULT_MQTT_PORT,
        username=config.get(section, "USERNAME", fallback=None) or None,
        password=config.get(section, "PASSWORD", fallback=None) or None,
        tls=config.getboolean(section, "TLS") if tls else False,
    )


def parse_float_list(value: str, key_name: str, section: str) -> list[float]:
    tokens = [t.strip() for t in value.split(",")]
    result = []
    for token in tokens:
        if not token:
            continue
        try:
            result.append(float(token))
        except ValueError as err:
            raise ValueError(
                f"Invalid {key_name} value '{token}' in section [{section}]"
            ) from err
    return result if result else [0.0]


def read_signal_settings(
    section: str,
    config: configparser.ConfigParser,
    defaults: SignalSettings,
    *,
    with_transform: bool = True,
) -> SignalSettings:
    """Read one section's signal conditioning, falling back to *defaults*.

    Called with the ``[GENERAL]`` section to derive the defaults themselves —
    ``with_transform`` is off there, since the offset/multiplier pair applies
    to the source that declares it, not to every source.
    """
    offsets: list[float] | None = None
    multipliers: list[float] | None = None
    if with_transform and (
        config.has_option(section, "POWER_OFFSET")
        or config.has_option(section, "POWER_MULTIPLIER")
    ):
        offsets = parse_float_list(
            config.get(section, "POWER_OFFSET", fallback="0"), "POWER_OFFSET", section
        )
        multipliers = parse_float_list(
            config.get(section, "POWER_MULTIPLIER", fallback="1"),
            "POWER_MULTIPLIER",
            section,
        )
    values = read_fields(
        config,
        section,
        SignalSettings,
        SIGNAL_KEY_OVERRIDES,
        skip=("offsets", "multipliers"),
        defaults=defaults,
    )
    values["pid_mode"] = values["pid_mode"].strip().lower()
    return SignalSettings(offsets=offsets, multipliers=multipliers, **values)


def apply_signal_wrappers(
    powermeter: Powermeter, name: str, signal: SignalSettings
) -> Powermeter:
    """Wrap *powermeter* in the conditioning stages *signal* asks for.

    Shared by every config backend, so a power source behaves the same however
    it was configured. *name* labels it in logs and MQTT Insights.
    """
    if signal.offsets is not None or signal.multipliers is not None:
        offsets = signal.offsets if signal.offsets is not None else [0.0]
        multipliers = signal.multipliers if signal.multipliers is not None else [1.0]
        logger.info(
            f"Applying power transform (multiplier={multipliers}, offset={offsets}) to {name}"
        )
        powermeter = TransformedPowermeter(powermeter, offsets, multipliers)

    if signal.throttle_interval > 0:
        logger.info("Applying throttling (%.1fs) to %s", signal.throttle_interval, name)
        powermeter = ThrottledPowermeter(powermeter, signal.throttle_interval)

    if signal.hampel_window > 0:
        logger.info(
            "Applying Hampel outlier filter (window=%d, n_sigma=%.2f, min_threshold=%.0fW) to %s",
            signal.hampel_window,
            signal.hampel_n_sigma,
            signal.hampel_min_threshold,
            name,
        )
        powermeter = HampelPowermeter(
            powermeter,
            window=signal.hampel_window,
            n_sigma=signal.hampel_n_sigma,
            min_threshold=signal.hampel_min_threshold,
        )

    if signal.smooth_alpha > 0:
        alpha = max(0.01, min(1.0, signal.smooth_alpha))
        logger.info(
            "Applying EMA smoothing (alpha=%.2f, max_step=%.0f) to %s",
            alpha,
            signal.max_smooth_step,
            name,
        )
        powermeter = SmoothedPowermeter(
            powermeter, alpha=alpha, max_step=signal.max_smooth_step
        )

    if signal.deadband > 0:
        logger.info("Applying deadband (%.0fW) to %s", signal.deadband, name)
        powermeter = DeadbandPowermeter(powermeter, deadband=signal.deadband)

    if signal.pid_kp > 0:
        logger.info(
            "Applying PID controller (Kp=%s, Ki=%s, Kd=%s, max=%sW, mode=%s) to %s",
            signal.pid_kp,
            signal.pid_ki,
            signal.pid_kd,
            signal.pid_output_max,
            signal.pid_mode,
            name,
        )
        powermeter = PidPowermeter(
            powermeter,
            kp=signal.pid_kp,
            ki=signal.pid_ki,
            kd=signal.pid_kd,
            output_max=signal.pid_output_max,
            mode=signal.pid_mode,
        )

    # Wrap outermost so health tracking sees the final processed read and
    # labels the powermeter's MQTT Insights device.
    return HealthTrackingPowermeter(powermeter, name=name)


def read_all_powermeter_configs(
    config: configparser.ConfigParser,
    global_signal: SignalSettings | None = None,
) -> list[ConfiguredPowermeter]:
    """Build every power source the config file declares."""
    if global_signal is None:
        global_signal = read_signal_settings(
            "GENERAL", config, SignalSettings(), with_transform=False
        )

    powermeters: list[ConfiguredPowermeter] = []
    for section in config.sections():
        powermeter = create_powermeter(section, config)
        if powermeter is None:
            continue
        signal = read_signal_settings(section, config, global_signal)
        powermeters.append(
            ConfiguredPowermeter(
                apply_signal_wrappers(powermeter, section, signal),
                create_client_filter(section, config),
                signal.wait_for_next_message,
            )
        )
    return powermeters


def create_client_filter(
    section: str, config: configparser.ConfigParser
) -> ClientFilter:
    netmask_raw = config.get(section, "NETMASK", fallback="0.0.0.0/0")
    netmasks = [IPv4Network(n.strip()) for n in netmask_raw.split(",")]
    return ClientFilter(netmasks)


def create_shelly_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    shelly_type = config.get(section, "TYPE", fallback="")
    shelly_ip = config.get(section, "IP", fallback="")
    shelly_user = config.get(section, "USER", fallback="")
    shelly_pass = config.get(section, "PASS", fallback="")
    meter_index = config.get(section, "METER_INDEX", fallback="")
    if shelly_type == "1PM":
        return Shelly1PM(shelly_ip, shelly_user, shelly_pass, meter_index)
    elif shelly_type == "PLUS1PM":
        return ShellyPlus1PM(shelly_ip, shelly_user, shelly_pass, meter_index)
    elif shelly_type == "EM" or shelly_type == "3EM":
        return ShellyEM(shelly_ip, shelly_user, shelly_pass, meter_index)
    elif shelly_type == "3EMPro":
        return Shelly3EMPro(shelly_ip, shelly_user, shelly_pass, meter_index)
    else:
        raise Exception(f"Error: unknown Shelly type '{shelly_type}'")


def create_amisreader_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return AmisReader(config.get(section, "IP", fallback=""))


def create_script_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return Script(config.get(section, "COMMAND", fallback=""))


def create_sml_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    oc, o1, o2, o3 = parse_sml_obis_config(section, config)
    kwargs = dict(
        obis_power_current=oc,
        obis_power_l1=o1,
        obis_power_l2=o2,
        obis_power_l3=o3,
    )
    raw = config.get(section, "SERIAL", fallback="").strip()
    if not raw:
        raise ValueError(
            f"Section [{section}] requires SERIAL (device path, e.g. /dev/ttyUSB0)."
        )
    return Sml(raw, **kwargs)


def create_mqtt_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    # Multi-topic: TOPICS takes precedence over TOPIC
    topics_raw = config.get(section, "TOPICS", fallback=None)
    if topics_raw:
        topic: str | list[str] = split_csv(topics_raw)
    else:
        topic = config.get(section, "TOPIC", fallback="")

    # Multi-path: JSON_PATHS takes precedence over JSON_PATH
    json_paths_raw = config.get(section, "JSON_PATHS", fallback=None)
    if json_paths_raw:
        json_path: str | list[str] | None = split_csv(json_paths_raw)
    else:
        json_path = config.get(section, "JSON_PATH", fallback=None)

    broker = read_mqtt_connection(section, config)
    return MqttPowermeter(
        broker.host,
        broker.port,
        topic,
        json_path,
        broker.username,
        broker.password,
        tls=broker.tls,
    )


def create_json_http_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    json_path_value = one_or_many(config.get(section, "JSON_PATHS", fallback=""))
    headers_raw = config.get(section, "HEADERS", fallback="")
    headers = (
        {
            name.strip(): value.strip()
            for name, value in (
                item.split(":", 1) for item in headers_raw.split(";") if ":" in item
            )
        }
        if headers_raw
        else None
    )
    return JsonHttpPowermeter(
        config.get(section, "URL", fallback=""),
        json_path_value,
        headers=headers,
        **_declared(
            config, section, username=("USERNAME", str), password=("PASSWORD", str)
        ),
    )


def create_modbus_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return ModbusPowermeter(
        config.get(section, "HOST", fallback=""),
        config.getint(section, "PORT", fallback=502),
        config.getint(section, "UNIT_ID", fallback=1),
        config.getint(section, "ADDRESS", fallback=0),
        config.getint(section, "COUNT", fallback=1),
        **_declared(
            config,
            section,
            data_type=("DATA_TYPE", str),
            byte_order=("BYTE_ORDER", str),
            word_order=("WORD_ORDER", str),
            register_type=("REGISTER_TYPE", str),
            transport=("TRANSPORT", str),
        ),
    )


def create_esphomenative_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return ESPHomeNative(
        address=config.get(section, "ADDRESS", fallback=""),
        port=config.get(section, "PORT", fallback="6053"),
        api_key=config.get(section, "API_KEY", fallback=""),
        object_id=config.get(section, "OBJECT_ID", fallback=""),
        client_info=config.get(section, "CLIENT_INFO", fallback="AstraMeter"),
    )


def create_esphome_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return ESPHome(
        config.get(section, "IP", fallback=""),
        config.get(section, "PORT", fallback=""),
        config.get(section, "DOMAIN", fallback=""),
        config.get(section, "ID", fallback=""),
    )


def create_vzlogger_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return VZLogger(
        config.get(section, "IP", fallback=""),
        config.get(section, "PORT", fallback=""),
        _split_labels(config.get(section, "UUID", fallback="")),
    )


def create_homeassistant_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    # Split entity strings on commas and strip whitespace
    def parse_entities(value: str) -> str | list[str]:
        if not value:
            return ""
        entities = [entity.strip() for entity in value.split(",")]
        # Return single string if only one entity, otherwise return list
        return entities[0] if len(entities) == 1 else entities

    current_power_entity = parse_entities(
        config.get(section, "CURRENT_POWER_ENTITY", fallback="")
    )
    power_input_alias = parse_entities(
        config.get(section, "POWER_INPUT_ALIAS", fallback="")
    )
    power_output_alias = parse_entities(
        config.get(section, "POWER_OUTPUT_ALIAS", fallback="")
    )

    ip = config.get(section, "IP", fallback="")
    if ip == "supervisor":

        def token_getter() -> str:
            return os.environ.get("SUPERVISOR_TOKEN", "")

    else:
        _static_token = config.get(section, "ACCESSTOKEN", fallback="")

        def token_getter() -> str:  # type: ignore[no-redef]
            return _static_token

    return HomeAssistant(
        ip,
        config.get(section, "PORT", fallback=""),
        config.getboolean(section, "HTTPS", fallback=False),
        token_getter,
        current_power_entity,
        config.getboolean(section, "POWER_CALCULATE", fallback=False),
        power_input_alias,
        power_output_alias,
        config.get(section, "API_PATH_PREFIX", fallback=None),
    )


def create_iobroker_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return IoBroker(
        config.get(section, "IP", fallback=""),
        config.get(section, "PORT", fallback=""),
        config.get(section, "CURRENT_POWER_ALIAS", fallback=""),
        config.getboolean(section, "POWER_CALCULATE", fallback=False),
        config.get(section, "POWER_INPUT_ALIAS", fallback=""),
        config.get(section, "POWER_OUTPUT_ALIAS", fallback=""),
    )


def create_emlog_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return Emlog(
        config.get(section, "IP", fallback=""),
        config.get(section, "METER_INDEX", fallback=""),
        config.getboolean(section, "JSON_POWER_CALCULATE", fallback=False),
    )


def create_shrdzm_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return Shrdzm(
        config.get(section, "IP", fallback=""),
        config.get(section, "USER", fallback=""),
        config.get(section, "PASS", fallback=""),
    )


def _split_labels(raw: str) -> str | list[str]:
    """Like :func:`one_or_many`, but no label at all is "" rather than []."""
    return one_or_many(raw) or ""


def create_tasmota_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return Tasmota(
        config.get(section, "IP", fallback=""),
        config.get(section, "USER", fallback=""),
        config.get(section, "PASS", fallback=""),
        config.get(section, "JSON_STATUS", fallback=""),
        config.get(section, "JSON_PAYLOAD_MQTT_PREFIX", fallback=""),
        _split_labels(config.get(section, "JSON_POWER_MQTT_LABEL", fallback="")),
        _split_labels(config.get(section, "JSON_POWER_INPUT_MQTT_LABEL", fallback="")),
        _split_labels(config.get(section, "JSON_POWER_OUTPUT_MQTT_LABEL", fallback="")),
        config.getboolean(section, "JSON_POWER_CALCULATE", fallback=False),
    )


def create_tq_em_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return TQEnergyManager(
        config.get(section, "IP", fallback=""),
        **_declared(
            config, section, password=("PASSWORD", str), timeout=("TIMEOUT", float)
        ),
    )


def create_homewizard_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return HomeWizardPowermeter(
        config.get(section, "IP", fallback=""),
        config.get(section, "TOKEN", fallback=""),
        config.get(section, "SERIAL", fallback=""),
        **_declared(config, section, verify_ssl=("VERIFY_SSL", bool)),
    )


def create_envoy_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return Envoy(
        host=config.get(section, "HOST", fallback=""),
        **_declared(
            config,
            section,
            token=("TOKEN", str),
            username=("USERNAME", str),
            password=("PASSWORD", str),
            serial=("SERIAL", str),
            verify_ssl=("VERIFY_SSL", bool),
        ),
    )


def create_sma_energy_meter_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return SmaEnergyMeter(
        **_declared(
            config,
            section,
            multicast_group=("MULTICAST_GROUP", str),
            port=("PORT", int),
            serial_number=("SERIAL_NUMBER", int),
            interface=("INTERFACE", str),
        )
    )


def create_fritz_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return FritzSmartEnergy(
        config.get(section, "HOST", fallback="fritz.box"),
        config.get(section, "USER", fallback=""),
        config.get(section, "PASSWORD", fallback=""),
        config.get(section, "AIN", fallback=""),
        **_declared(
            config,
            section,
            use_tls=("HTTPS", bool),
            verify_ssl=("VERIFY_SSL", bool),
            timeout=("TIMEOUT", float),
        ),
    )


def create_fronius_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return Fronius(
        config.get(section, "IP", fallback=""),
        **_declared(
            config, section, device_id=("DEVICE_ID", str), per_phase=("PER_PHASE", bool)
        ),
    )


def create_refoss_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    """Build a Refoss/Meross powermeter from a ``[REFOSS]`` / ``[MEROSS]`` section."""
    from astrameter.powermeter.refoss import parse_channels

    return Refoss(
        config.get(section, "IP", fallback=""),
        parse_channels(config.get(section, "CHANNELS", fallback="1")),
    )


def create_tibber_pulse_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    oc, o1, o2, o3 = parse_sml_obis_config(section, config)
    return TibberPulse(
        config.get(section, "IP", fallback=""),
        config.get(section, "PASSWORD", fallback=""),
        obis_power_current=oc,
        obis_power_l1=o1,
        obis_power_l2=o2,
        obis_power_l3=o3,
        **_declared(
            config,
            section,
            node_id=("NODE_ID", str),
            user=("USER", str),
            timeout=("TIMEOUT", float),
        ),
    )


PowermeterFactory = Callable[[str, configparser.ConfigParser], Powermeter]

# Matched longest prefix first, so [ESPHOMENATIVE] is never read as [ESPHOME]
# and [MQTT_INSIGHTS] — not a power source — is refused before [MQTT] matches.
_FACTORIES: list[tuple[str, PowermeterFactory | None]] = sorted(
    [
        (SHELLY_SECTION, create_shelly_powermeter),
        (TASMOTA_SECTION, create_tasmota_powermeter),
        (SHRDZM_SECTION, create_shrdzm_powermeter),
        (EMLOG_SECTION, create_emlog_powermeter),
        (IOBROKER_SECTION, create_iobroker_powermeter),
        (HOMEASSISTANT_SECTION, create_homeassistant_powermeter),
        (VZLOGGER_SECTION, create_vzlogger_powermeter),
        (SCRIPT_SECTION, create_script_powermeter),
        (SML_SECTION, create_sml_powermeter),
        (ESPHOME_SECTION, create_esphome_powermeter),
        (ESPHOMENATIVE_SECTION, create_esphomenative_powermeter),
        (AMIS_READER_SECTION, create_amisreader_powermeter),
        (MODBUS_SECTION, create_modbus_powermeter),
        (TQ_EM_SECTION, create_tq_em_powermeter),
        (JSON_HTTP_SECTION, create_json_http_powermeter),
        (HOMEWIZARD_SECTION, create_homewizard_powermeter),
        (ENVOY_SECTION, create_envoy_powermeter),
        (SMA_ENERGY_METER_SECTION, create_sma_energy_meter_powermeter),
        (FRITZ_SECTION, create_fritz_powermeter),
        (FRONIUS_SECTION, create_fronius_powermeter),
        (REFOSS_SECTION, create_refoss_powermeter),
        (MEROSS_SECTION, create_refoss_powermeter),
        (TIBBER_PULSE_SECTION, create_tibber_pulse_powermeter),
        (MQTT_SECTION, create_mqtt_powermeter),
        (MQTT_INSIGHTS_SECTION, None),
    ],
    key=lambda entry: len(entry[0]),
    reverse=True,
)


def create_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter | None:
    for prefix, factory in _FACTORIES:
        if section.startswith(prefix):
            return factory(section, config) if factory else None
    return None


def read_mqtt_insights_config(
    config: configparser.ConfigParser,
) -> MqttInsightsConfig | None:
    """Read [MQTT_INSIGHTS] section; return None if absent."""
    from astrameter.mqtt_insights import MqttInsightsConfig

    for section in config.sections():
        if section.startswith(MQTT_INSIGHTS_SECTION):
            broker = read_mqtt_connection(section, config)
            return MqttInsightsConfig(
                broker=broker.host or "localhost",
                port=broker.port,
                username=broker.username,
                password=broker.password,
                tls=broker.tls,
                # Everything else keeps whatever MqttInsightsConfig declares
                # unless this section really sets it — a key left blank means
                # "default", not "off" or "zero".
                **_declared(
                    config,
                    section,
                    blank_is_unset=True,
                    base_topic=("BASE_TOPIC", str),
                    ha_discovery=("HA_DISCOVERY", bool),
                    ha_discovery_prefix=("HA_DISCOVERY_PREFIX", str),
                    addon_slug=("ADDON_SLUG", str),
                    marstek_mqtt_enabled=("MARSTEK_MQTT_ENABLED", bool),
                    marstek_mqtt_interval=("MARSTEK_MQTT_INTERVAL", float),
                    powermeter_health_interval=("POWERMETER_HEALTH_INTERVAL", float),
                ),
            )
    return None
