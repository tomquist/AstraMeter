import configparser
from ipaddress import IPv4Network

import pytest

from b2500_meter.config.config_loader import (
    ClientFilter,
    create_amisreader_powermeter,
    create_client_filter,
    create_emlog_powermeter,
    create_esphome_powermeter,
    create_homeassistant_powermeter,
    create_homewizard_powermeter,
    create_iobroker_powermeter,
    create_json_http_powermeter,
    create_modbus_powermeter,
    create_mqtt_powermeter,
    create_powermeter,
    create_script_powermeter,
    create_shelly_powermeter,
    create_shrdzm_powermeter,
    create_sml_powermeter,
    create_tasmota_powermeter,
    create_tq_em_powermeter,
    create_vzlogger_powermeter,
    parse_float_list,
    read_all_powermeter_configs,
)
from b2500_meter.powermeter import TransformedPowermeter


def test_client_filter():
    """Basic test for ClientFilter."""
    filter = ClientFilter([IPv4Network("192.168.1.0/24")])
    # Just verify it runs without raising exceptions
    filter.matches("192.168.1.100")  # Should match
    filter.matches("10.0.0.1")  # Should not match
    filter.matches("invalid_ip")  # Should handle invalid


def test_create_client_filter():
    """Test create_client_filter with various inputs."""
    config = configparser.ConfigParser()
    config["TEST1"] = {"NETMASK": "192.168.1.0/24,10.0.0.0/8"}
    config["TEST2"] = {}  # No NETMASK specified, tests default

    # Just verify these run without exceptions
    create_client_filter("TEST1", config)
    create_client_filter("TEST2", config)


def test_create_shelly_powermeter():
    """Test Shelly powermeter creation."""
    config = configparser.ConfigParser()

    # Note: These won't connect to real devices, but test that
    # the creation logic works

    # Test all Shelly types with minimal config
    config["SHELLY1"] = {"TYPE": "1PM", "IP": "127.0.0.1"}
    config["SHELLY2"] = {"TYPE": "PLUS1PM", "IP": "127.0.0.1"}
    config["SHELLY3"] = {"TYPE": "EM", "IP": "127.0.0.1"}
    config["SHELLY4"] = {"TYPE": "3EM", "IP": "127.0.0.1"}
    config["SHELLY5"] = {"TYPE": "3EMPro", "IP": "127.0.0.1"}

    # Just verify these run without exceptions
    try:
        create_shelly_powermeter("SHELLY1", config)
        create_shelly_powermeter("SHELLY2", config)
        create_shelly_powermeter("SHELLY3", config)
        create_shelly_powermeter("SHELLY4", config)
        create_shelly_powermeter("SHELLY5", config)
    except Exception as e:
        if "Connection" not in str(e):  # Ignore expected connection errors
            raise

    # Test invalid type - should raise an exception
    config["SHELLY_INVALID"] = {"TYPE": "INVALID", "IP": "127.0.0.1"}
    with pytest.raises(Exception, match="unknown Shelly"):
        create_shelly_powermeter("SHELLY_INVALID", config)


def test_create_tasmota_powermeter():
    """Test Tasmota powermeter creation."""
    config = configparser.ConfigParser()
    config["TASMOTA"] = {"IP": "127.0.0.1"}

    try:
        create_tasmota_powermeter("TASMOTA", config)
    except Exception as e:
        if "Connection" not in str(e):  # Ignore expected connection errors
            raise


def test_create_shrdzm_powermeter():
    """Test Shrdzm powermeter creation."""
    config = configparser.ConfigParser()
    config["SHRDZM"] = {"IP": "127.0.0.1"}

    try:
        create_shrdzm_powermeter("SHRDZM", config)
    except Exception as e:
        if "Connection" not in str(e):  # Ignore expected connection errors
            raise


def test_create_emlog_powermeter():
    """Test Emlog powermeter creation."""
    config = configparser.ConfigParser()
    config["EMLOG"] = {"IP": "127.0.0.1"}

    try:
        create_emlog_powermeter("EMLOG", config)
    except Exception as e:
        if "Connection" not in str(e):  # Ignore expected connection errors
            raise


def test_create_iobroker_powermeter():
    """Test IoBroker powermeter creation."""
    config = configparser.ConfigParser()
    config["IOBROKER"] = {"IP": "127.0.0.1"}

    try:
        create_iobroker_powermeter("IOBROKER", config)
    except Exception as e:
        if "Connection" not in str(e):  # Ignore expected connection errors
            raise


def test_create_homeassistant_powermeter():
    """Test HomeAssistant powermeter creation."""
    config = configparser.ConfigParser()

    # Test single entity
    config["HA1"] = {"IP": "127.0.0.1", "CURRENT_POWER_ENTITY": "sensor.power"}

    # Test multiple entities
    config["HA2"] = {
        "IP": "127.0.0.1",
        "CURRENT_POWER_ENTITY": "sensor.power1, sensor.power2",
        "POWER_INPUT_ALIAS": "sensor.input1, sensor.input2",
        "POWER_OUTPUT_ALIAS": "sensor.output1, sensor.output2",
    }

    try:
        create_homeassistant_powermeter("HA1", config)
        create_homeassistant_powermeter("HA2", config)
    except Exception as e:
        if "Connection" not in str(e):  # Ignore expected connection errors
            raise


def test_create_vzlogger_powermeter():
    """Test VZLogger powermeter creation."""
    config = configparser.ConfigParser()
    config["VZLOGGER"] = {"IP": "127.0.0.1"}

    try:
        create_vzlogger_powermeter("VZLOGGER", config)
    except Exception as e:
        if "Connection" not in str(e):  # Ignore expected connection errors
            raise


def test_create_script_powermeter():
    """Test Script powermeter creation."""
    config = configparser.ConfigParser()
    config["SCRIPT"] = {"COMMAND": 'echo "test"'}

    try:
        create_script_powermeter("SCRIPT", config)
    except Exception as e:
        if "not found" not in str(e):  # Ignore script not found errors
            raise


def test_create_esphome_powermeter():
    """Test ESPHome powermeter creation."""
    config = configparser.ConfigParser()
    config["ESPHOME"] = {"IP": "127.0.0.1"}

    try:
        create_esphome_powermeter("ESPHOME", config)
    except Exception as e:
        if "Connection" not in str(e):  # Ignore expected connection errors
            raise


def test_create_amisreader_powermeter():
    """Test AMIS Reader powermeter creation."""
    config = configparser.ConfigParser()
    config["AMIS_READER"] = {"IP": "127.0.0.1"}

    try:
        create_amisreader_powermeter("AMIS_READER", config)
    except Exception as e:
        if "Connection" not in str(e):  # Ignore expected connection errors
            raise


def test_create_modbus_powermeter():
    """Test Modbus powermeter creation."""
    config = configparser.ConfigParser()
    config["MODBUS"] = {"HOST": "127.0.0.1"}

    try:
        create_modbus_powermeter("MODBUS", config)
    except Exception as e:
        if "Connection" not in str(e):  # Ignore expected connection errors
            raise


def test_create_mqtt_powermeter():
    """Test MQTT powermeter creation."""
    config = configparser.ConfigParser()
    config["MQTT"] = {"BROKER": "127.0.0.1"}

    try:
        create_mqtt_powermeter("MQTT", config)
    except Exception as e:
        if "Connection" not in str(e) and "timed out" not in str(e):
            # Ignore connection errors
            raise


def test_create_mqtt_powermeter_with_topics():
    """Test MQTT powermeter creation with multi-phase TOPICS."""
    config = configparser.ConfigParser()
    config["MQTT"] = {
        "BROKER": "127.0.0.1",
        "TOPICS": "home/l1, home/l2, home/l3",
    }
    pm = create_mqtt_powermeter("MQTT", config)
    assert len(pm._subscriptions) == 3
    assert pm._subscriptions == [
        ("home/l1", None),
        ("home/l2", None),
        ("home/l3", None),
    ]


def test_create_mqtt_powermeter_with_json_paths():
    """Test MQTT powermeter creation with single TOPIC and multiple JSON_PATHS."""
    config = configparser.ConfigParser()
    config["MQTT"] = {
        "BROKER": "127.0.0.1",
        "TOPIC": "home/power",
        "JSON_PATHS": "$.l1.power, $.l2.power, $.l3.power",
    }
    pm = create_mqtt_powermeter("MQTT", config)
    assert len(pm._subscriptions) == 3
    assert pm._subscriptions[0] == ("home/power", "$.l1.power")
    assert pm._subscriptions[1] == ("home/power", "$.l2.power")
    assert pm._subscriptions[2] == ("home/power", "$.l3.power")


def test_create_mqtt_powermeter_topics_takes_precedence_over_topic():
    """Test that TOPICS takes precedence over TOPIC when both are set."""
    config = configparser.ConfigParser()
    config["MQTT"] = {
        "BROKER": "127.0.0.1",
        "TOPIC": "ignored",
        "TOPICS": "home/l1, home/l2",
    }
    pm = create_mqtt_powermeter("MQTT", config)
    assert len(pm._subscriptions) == 2
    assert pm._subscriptions[0][0] == "home/l1"
    assert pm._subscriptions[1][0] == "home/l2"


def test_create_json_http_powermeter():
    """Test JSON HTTP powermeter creation."""
    config = configparser.ConfigParser()
    config["JSON_HTTP"] = {"URL": "http://localhost", "JSON_PATHS": "$.power"}

    try:
        create_json_http_powermeter("JSON_HTTP", config)
    except Exception as e:
        if "Connection" not in str(e):
            raise


def test_create_tq_em_powermeter():
    """Test TQ Energy Manager powermeter creation."""
    config = configparser.ConfigParser()
    config["TQ_EM"] = {"IP": "127.0.0.1"}

    try:
        create_tq_em_powermeter("TQ_EM", config)
    except Exception as e:
        if "Connection" not in str(e):
            raise


def test_create_homewizard_powermeter():
    """Test HomeWizard powermeter creation."""
    config = configparser.ConfigParser()
    config["HOMEWIZARD"] = {
        "IP": "127.0.0.1",
        "TOKEN": "ABCDEF1234567890ABCDEF1234567890",
        "SERIAL": "aabbccddee",
    }

    try:
        create_homewizard_powermeter("HOMEWIZARD", config)
    except Exception as e:
        if "Connection" not in str(e) and "timed out" not in str(e):
            raise


def test_create_sml_powermeter():
    """Test SML powermeter creation: SERIAL required, OBIS overrides applied."""
    config = configparser.ConfigParser()
    config["SML"] = {"SERIAL": "/dev/ttyUSB0"}
    pm = create_sml_powermeter("SML", config)
    assert pm._serial_device == "/dev/ttyUSB0"

    config = configparser.ConfigParser()
    config["SML"] = {
        "SERIAL": "/dev/ttyUSB0",
        "OBIS_POWER_CURRENT": "0100100700ff",
    }
    pm = create_sml_powermeter("SML", config)
    assert pm._obis_current == "0100100700ff"


def test_create_sml_powermeter_requires_serial():
    """Section [SML] must define non-empty SERIAL."""
    config = configparser.ConfigParser()
    config["SML"] = {}
    with pytest.raises(ValueError, match="SERIAL"):
        create_sml_powermeter("SML", config)

    config = configparser.ConfigParser()
    config["SML"] = {"SERIAL": "  \t  "}
    with pytest.raises(ValueError, match="SERIAL"):
        create_sml_powermeter("SML", config)


def test_create_powermeter():
    """Test the main create_powermeter function."""
    config = configparser.ConfigParser()

    # Setup basic configurations for each type
    config["SHELLY_TEST"] = {"TYPE": "1PM", "IP": "127.0.0.1"}
    config["TASMOTA_TEST"] = {"IP": "127.0.0.1"}
    config["SHRDZM_TEST"] = {"IP": "127.0.0.1"}
    config["EMLOG_TEST"] = {"IP": "127.0.0.1"}
    config["IOBROKER_TEST"] = {"IP": "127.0.0.1"}
    config["HOMEASSISTANT_TEST"] = {"IP": "127.0.0.1"}
    config["VZLOGGER_TEST"] = {"IP": "127.0.0.1"}
    config["SCRIPT_TEST"] = {"COMMAND": 'echo "test"'}
    config["ESPHOME_TEST"] = {"IP": "127.0.0.1"}
    config["AMIS_READER_TEST"] = {"IP": "127.0.0.1"}
    config["MODBUS_TEST"] = {"HOST": "127.0.0.1"}
    config["MQTT_TEST"] = {"BROKER": "127.0.0.1"}
    config["JSON_HTTP_TEST"] = {"URL": "http://localhost", "JSON_PATHS": "$.power"}
    config["TQ_EM_TEST"] = {"IP": "127.0.0.1"}
    config["HOMEWIZARD_TEST"] = {
        "IP": "127.0.0.1",
        "TOKEN": "ABCDEF1234567890ABCDEF1234567890",
        "SERIAL": "aabbccddee",
    }
    config["SML_TEST"] = {"SERIAL": "/dev/ttyUSB0"}
    config["UNKNOWN_TEST"] = {"SOME_KEY": "some_value"}

    # Test each powermeter type
    for section in config.sections():
        try:
            create_powermeter(section, config)
        except Exception as e:
            # Ignore expected connection errors
            if (
                "Connection" not in str(e)
                and "timed out" not in str(e)
                and "not found" not in str(e)
                and section != "UNKNOWN_TEST"
            ):  # Unknown section is expected to return None
                raise


def test_read_all_powermeter_configs():
    """Test reading all powermeter configs."""
    config = configparser.ConfigParser()
    config["SHELLY_1"] = {"TYPE": "1PM", "IP": "127.0.0.1"}
    config["TASMOTA_1"] = {"IP": "127.0.0.1"}
    config["UNKNOWN_SECTION"] = {"SOME_KEY": "some_value"}

    try:
        # Attempt to read all configs
        powermeters = read_all_powermeter_configs(config)

        # Just verify we got some results, don't validate details
        # Some powermeters might fail due to connection issues, but the function should run
        assert isinstance(powermeters, list)

    except Exception as e:
        if "Connection" not in str(e) and "timed out" not in str(e):
            raise


def test_parse_float_list():
    """Test parsing comma-separated float lists."""
    assert parse_float_list("10", "KEY", "SECTION") == [10.0]
    assert parse_float_list("1.5, 2.5, 3.5", "KEY", "SECTION") == [1.5, 2.5, 3.5]
    assert parse_float_list("-50", "KEY", "SECTION") == [-50.0]
    assert parse_float_list(" 10 , 20 ", "KEY", "SECTION") == [10.0, 20.0]
    assert parse_float_list("", "KEY", "SECTION") == [0.0]


def test_parse_float_list_invalid():
    """Test that invalid float values raise ValueError with clear message."""
    with pytest.raises(ValueError, match="Invalid POWER_OFFSET value 'abc'"):
        parse_float_list("abc", "POWER_OFFSET", "SHELLY_1")


def test_read_all_configs_with_power_transform():
    """Test that POWER_OFFSET and POWER_MULTIPLIER wrap the powermeter."""
    config = configparser.ConfigParser()
    config["SCRIPT_1"] = {
        "COMMAND": 'echo "100"',
        "POWER_OFFSET": "-50",
        "POWER_MULTIPLIER": "1.05",
    }

    powermeters = read_all_powermeter_configs(config)
    assert len(powermeters) == 1
    pm, _ = powermeters[0]
    assert isinstance(pm, TransformedPowermeter)
    assert pm.offsets == [-50.0]
    assert pm.multipliers == [1.05]


def test_read_all_configs_with_per_phase_transform():
    """Test per-phase offset and multiplier values."""
    config = configparser.ConfigParser()
    config["SCRIPT_1"] = {
        "COMMAND": 'echo "100"',
        "POWER_OFFSET": "-10,-20,-30",
        "POWER_MULTIPLIER": "1.05,1.02,1.03",
    }

    powermeters = read_all_powermeter_configs(config)
    assert len(powermeters) == 1
    pm, _ = powermeters[0]
    assert isinstance(pm, TransformedPowermeter)
    assert pm.offsets == [-10.0, -20.0, -30.0]
    assert pm.multipliers == [1.05, 1.02, 1.03]


def test_read_all_configs_offset_only():
    """Test that setting only POWER_OFFSET wraps with default multiplier."""
    config = configparser.ConfigParser()
    config["SCRIPT_1"] = {
        "COMMAND": 'echo "100"',
        "POWER_OFFSET": "10",
    }

    powermeters = read_all_powermeter_configs(config)
    assert len(powermeters) == 1
    pm, _ = powermeters[0]
    assert isinstance(pm, TransformedPowermeter)
    assert pm.offsets == [10.0]
    assert pm.multipliers == [1.0]


def test_read_all_configs_zero_multiplier_accepted():
    """Test that a multiplier of 0 is accepted (e.g. to null a phase)."""
    config = configparser.ConfigParser()
    config["SCRIPT_1"] = {
        "COMMAND": 'echo "100"',
        "POWER_MULTIPLIER": "0",
    }

    powermeters = read_all_powermeter_configs(config)
    assert len(powermeters) == 1
    pm, _ = powermeters[0]
    assert isinstance(pm, TransformedPowermeter)
    assert pm.multipliers == [0.0]


def test_read_all_configs_no_transform_when_not_configured():
    """Test that no transform wrapper is applied when keys are absent."""
    config = configparser.ConfigParser()
    config["SCRIPT_1"] = {
        "COMMAND": 'echo "100"',
    }

    powermeters = read_all_powermeter_configs(config)
    assert len(powermeters) == 1
    pm, _ = powermeters[0]
    assert not isinstance(pm, TransformedPowermeter)
