import configparser
from ipaddress import IPv4Address, IPv4Network

from b2500_meter.config.logger import logger
from b2500_meter.powermeter import (
    AmisReader,
    Emlog,
    ESPHome,
    HomeAssistant,
    HomeWizardPowermeter,
    IoBroker,
    JsonHttpPowermeter,
    ModbusPowermeter,
    MqttPowermeter,
    Powermeter,
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
    TQEnergyManager,
    TransformedPowermeter,
    VZLogger,
    parse_sml_obis_config,
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
AMIS_READER_SECTION = "AMIS_READER"
MODBUS_SECTION = "MODBUS"
JSON_HTTP_SECTION = "JSON_HTTP"
TQ_EM_SECTION = "TQ_EM"
HOMEWIZARD_SECTION = "HOMEWIZARD"
SMA_ENERGY_METER_SECTION = "SMA_ENERGY_METER"


class ClientFilter:
    def __init__(self, netmasks: list[IPv4Network]):
        self.netmasks = netmasks

    def matches(self, client_ip) -> bool:
        try:
            client_ip_addr = IPv4Address(client_ip)
            for netmask in self.netmasks:
                if client_ip_addr in netmask:
                    return True
        except ValueError as e:
            logger.error(f"Error: {e}")
            return False
        return False


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


def read_all_powermeter_configs(
    config: configparser.ConfigParser,
) -> list[tuple[Powermeter, ClientFilter]]:
    powermeters = []
    global_throttle_interval = config.getfloat(
        "GENERAL", "THROTTLE_INTERVAL", fallback=0.0
    )

    for section in config.sections():
        powermeter = create_powermeter(section, config)
        if powermeter is not None:
            # Apply power transform if configured
            has_offset = config.has_option(section, "POWER_OFFSET")
            has_multiplier = config.has_option(section, "POWER_MULTIPLIER")
            if has_offset or has_multiplier:
                offsets = parse_float_list(
                    config.get(section, "POWER_OFFSET", fallback="0"),
                    "POWER_OFFSET",
                    section,
                )
                multipliers = parse_float_list(
                    config.get(section, "POWER_MULTIPLIER", fallback="1"),
                    "POWER_MULTIPLIER",
                    section,
                )
                logger.info(
                    f"Applying power transform (multiplier={multipliers}, offset={offsets}) to {section}"
                )
                powermeter = TransformedPowermeter(powermeter, offsets, multipliers)

            section_throttle_interval = config.getfloat(
                section, "THROTTLE_INTERVAL", fallback=global_throttle_interval
            )

            if section_throttle_interval > 0:
                throttle_source = (
                    "section-specific"
                    if config.has_option(section, "THROTTLE_INTERVAL")
                    else "global"
                )
                logger.info(
                    "Applying %s throttling (%.1fs) to %s",
                    throttle_source,
                    section_throttle_interval,
                    section,
                )
                powermeter = ThrottledPowermeter(powermeter, section_throttle_interval)

            client_filter = create_client_filter(section, config)
            powermeters.append((powermeter, client_filter))
    return powermeters


def create_client_filter(
    section: str, config: configparser.ConfigParser
) -> ClientFilter:
    netmask_raw = config.get(section, "NETMASK", fallback="0.0.0.0/0")
    netmasks = [IPv4Network(n.strip()) for n in netmask_raw.split(",")]
    return ClientFilter(netmasks)


# Helper function to create a powermeter instance
def create_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter | None:
    if section.startswith(SHELLY_SECTION):
        return create_shelly_powermeter(section, config)
    elif section.startswith(TASMOTA_SECTION):
        return create_tasmota_powermeter(section, config)
    elif section.startswith(SHRDZM_SECTION):
        return create_shrdzm_powermeter(section, config)
    elif section.startswith(EMLOG_SECTION):
        return create_emlog_powermeter(section, config)
    elif section.startswith(IOBROKER_SECTION):
        return create_iobroker_powermeter(section, config)
    elif section.startswith(HOMEASSISTANT_SECTION):
        return create_homeassistant_powermeter(section, config)
    elif section.startswith(VZLOGGER_SECTION):
        return create_vzlogger_powermeter(section, config)
    elif section.startswith(SCRIPT_SECTION):
        return create_script_powermeter(section, config)
    elif section.startswith(SML_SECTION):
        return create_sml_powermeter(section, config)
    elif section.startswith(ESPHOME_SECTION):
        return create_esphome_powermeter(section, config)
    elif section.startswith(AMIS_READER_SECTION):
        return create_amisreader_powermeter(section, config)
    elif section.startswith(MODBUS_SECTION):
        return create_modbus_powermeter(section, config)
    elif section.startswith(TQ_EM_SECTION):
        return create_tq_em_powermeter(section, config)
    elif section.startswith(JSON_HTTP_SECTION):
        return create_json_http_powermeter(section, config)
    elif section.startswith(HOMEWIZARD_SECTION):
        return create_homewizard_powermeter(section, config)
    elif section.startswith(SMA_ENERGY_METER_SECTION):
        return create_sma_energy_meter_powermeter(section, config)
    elif section.startswith("MQTT"):
        return create_mqtt_powermeter(section, config)
    else:
        return None


def create_shelly_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    shelly_type = config.get(section, "TYPE", fallback="")
    shelly_ip = config.get(section, "IP", fallback="")
    shelly_user = config.get(section, "USER", fallback="")
    shelly_pass = config.get(section, "PASS", fallback="")
    shelly_meterindex = config.get(section, "METER_INDEX", fallback=None)
    meter_index = shelly_meterindex if shelly_meterindex is not None else ""
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
        topic: str | list[str] = [t.strip() for t in topics_raw.split(",") if t.strip()]
    else:
        topic = config.get(section, "TOPIC", fallback="")

    # Multi-path: JSON_PATHS takes precedence over JSON_PATH
    json_paths_raw = config.get(section, "JSON_PATHS", fallback=None)
    if json_paths_raw:
        json_path: str | list[str] | None = [
            p.strip() for p in json_paths_raw.split(",") if p.strip()
        ]
    else:
        json_path = config.get(section, "JSON_PATH", fallback=None)

    return MqttPowermeter(
        config.get(section, "BROKER", fallback=""),
        config.getint(section, "PORT", fallback=1883),
        topic,
        json_path,
        config.get(section, "USERNAME", fallback=None),
        config.get(section, "PASSWORD", fallback=None),
    )


def create_json_http_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    json_paths = config.get(section, "JSON_PATHS", fallback="").split(",")
    json_paths = [p.strip() for p in json_paths if p.strip()]
    json_path_value = json_paths[0] if len(json_paths) == 1 else json_paths
    return JsonHttpPowermeter(
        config.get(section, "URL", fallback=""),
        json_path_value,
        config.get(section, "USERNAME", fallback=None),
        config.get(section, "PASSWORD", fallback=None),
        (
            {
                k.strip(): v.strip()
                for k, v in (
                    [
                        item.split(":", 1)
                        for item in config.get(section, "HEADERS", fallback="").split(
                            ";"
                        )
                        if ":" in item
                    ]
                )
            }
            if config.get(section, "HEADERS", fallback="")
            else None
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
        config.get(section, "DATA_TYPE", fallback="UINT16"),
        config.get(section, "BYTE_ORDER", fallback="BIG"),
        config.get(section, "WORD_ORDER", fallback="BIG"),
        config.get(section, "REGISTER_TYPE", fallback="HOLDING"),
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
        config.get(section, "UUID", fallback=""),
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

    return HomeAssistant(
        config.get(section, "IP", fallback=""),
        config.get(section, "PORT", fallback=""),
        config.getboolean(section, "HTTPS", fallback=False),
        config.get(section, "ACCESSTOKEN", fallback=""),
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
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if len(parts) <= 1:
        return parts[0] if parts else ""
    return parts


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
        config.get(section, "PASSWORD", fallback=""),
        timeout=config.getfloat(section, "TIMEOUT", fallback=5.0),
    )


def create_homewizard_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return HomeWizardPowermeter(
        config.get(section, "IP", fallback=""),
        config.get(section, "TOKEN", fallback=""),
        config.get(section, "SERIAL", fallback=""),
        verify_ssl=config.getboolean(section, "VERIFY_SSL", fallback=True),
    )


def create_sma_energy_meter_powermeter(
    section: str, config: configparser.ConfigParser
) -> Powermeter:
    return SmaEnergyMeter(
        config.get(section, "MULTICAST_GROUP", fallback="239.12.255.254"),
        config.getint(section, "PORT", fallback=9522),
        config.getint(section, "SERIAL_NUMBER", fallback=0),
        config.get(section, "INTERFACE", fallback=""),
    )
