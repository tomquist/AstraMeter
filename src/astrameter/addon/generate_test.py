"""What the add-on options turn into.

This is the test that could not exist while the generator was shell inside a
``{ ... } > config.ini`` block: issue #510 shipped a config file that was
silently truncated to nothing for every user who had not set an optional
cloud-reporting key — that is, almost all of them — and the add-on simply
stopped starting.
"""

from __future__ import annotations

import configparser

from .generate import ct_sections, generate_config
from .options_test import addon_defaults


def parse(text: str) -> configparser.ConfigParser:
    cfg = configparser.ConfigParser(interpolation=None)
    cfg.read_string(text)
    return cfg


def test_fresh_install_is_complete():
    """#510: the defaults alone must produce a usable, startable config."""
    cfg = parse(generate_config(addon_defaults()))

    assert cfg.sections() == ["GENERAL", "CT002", "HOMEASSISTANT"]
    assert cfg["GENERAL"]["DEVICE_TYPE"] == "shellypro3em"
    assert cfg["GENERAL"]["THROTTLE_INTERVAL"] == "0"
    assert cfg["GENERAL"]["ENABLE_WEB_SERVER"] == "true"
    assert cfg["HOMEASSISTANT"]["IP"] == "supervisor"
    assert cfg["HOMEASSISTANT"]["PORT"] == "80"
    assert cfg["HOMEASSISTANT"]["API_PATH_PREFIX"] == "/core"
    assert cfg["HOMEASSISTANT"]["CURRENT_POWER_ENTITY"] == "sensor.current_power_in"


def test_unset_optional_keys_are_omitted_not_emptied():
    """An unset option keeps the loader's default instead of parsing "" ."""
    cfg = parse(generate_config(addon_defaults()))
    for absent in ("CLOUD_REPORTING_HOST", "BALANCE_GAIN", "IMPORT_TRIM_W"):
        assert absent not in cfg["CT002"]
    for absent in ("PID_KP", "DEADBAND", "POWER_OFFSET"):
        assert absent not in cfg["HOMEASSISTANT"]
    assert "DEDUPE_TIME_WINDOW" not in cfg["GENERAL"]


def test_zero_and_false_are_written():
    """They are choices, not absences — bashio::config.has_value agreed."""
    cfg = parse(generate_config({**addon_defaults(), "import_trim_w": 0}))
    assert cfg["CT002"]["MIN_DC_OUTPUT"] == "0"
    assert cfg["CT002"]["IMPORT_TRIM_W"] == "0"
    assert cfg["CT002"]["CLOUD_REPORTING"] == "false"
    assert cfg["CT002"]["ACTIVE_CONTROL"] == "true"


def test_ct_mac_is_written_even_when_blank():
    """A blank CT_MAC means "accept any", which is the intended default."""
    cfg = parse(generate_config(addon_defaults()))
    assert cfg["CT002"]["CT_MAC"] == ""


def test_ct_section_selection():
    assert ct_sections("ct002") == ["CT002"]
    assert ct_sections("CT003") == ["CT003"]
    assert ct_sections("ct002 ct003") == ["CT002", "CT003"]
    # Shelly-only installs still get the (inert) CT002 section, as before.
    assert ct_sections("shellypro3em") == ["CT002"]


def test_both_ct_types_get_identical_settings():
    options = {
        **addon_defaults(),
        "device_types": "ct002 ct003",
        "ct_mac": "aa:bb:cc:dd:ee:ff",
        "balance_gain": 0.4,
    }
    cfg = parse(generate_config(options))
    assert cfg.sections() == ["GENERAL", "CT002", "CT003", "HOMEASSISTANT"]
    assert dict(cfg["CT002"]) == dict(cfg["CT003"])
    assert cfg["CT002"]["CT_MAC"] == "aa:bb:cc:dd:ee:ff"
    assert cfg["CT002"]["BALANCE_GAIN"] == "0.4"


def test_separate_export_sensor_switches_to_calculate_mode():
    options = {
        **addon_defaults(),
        "power_input_alias": "sensor.import",
        "power_output_alias": "sensor.export",
    }
    ha = parse(generate_config(options))["HOMEASSISTANT"]
    assert ha["POWER_CALCULATE"] == "True"
    assert ha["POWER_INPUT_ALIAS"] == "sensor.import"
    assert ha["POWER_OUTPUT_ALIAS"] == "sensor.export"
    assert "CURRENT_POWER_ENTITY" not in ha


def test_single_signed_sensor_is_read_directly():
    ha = parse(generate_config(addon_defaults()))["HOMEASSISTANT"]
    assert ha["POWER_CALCULATE"] == "False"
    assert ha["CURRENT_POWER_ENTITY"] == "sensor.current_power_in"
    assert "POWER_OUTPUT_ALIAS" not in ha


def test_marstek_needs_the_flag_and_both_credentials():
    base = addon_defaults()
    creds = {"marstek_mailbox": "user@example.com", "marstek_password": "pw"}

    assert "MARSTEK" not in parse(generate_config({**base, **creds})).sections()
    assert (
        "MARSTEK"
        not in parse(
            generate_config({**base, "marstek_auto_register_ct_device": True})
        ).sections()
    )
    assert (
        "MARSTEK"
        not in parse(
            generate_config(
                {
                    **base,
                    "marstek_auto_register_ct_device": True,
                    "marstek_mailbox": "user@example.com",
                }
            )
        ).sections()
    )

    cfg = parse(
        generate_config({**base, "marstek_auto_register_ct_device": True, **creds})
    )
    assert cfg["MARSTEK"]["ENABLE"] == "True"
    assert cfg["MARSTEK"]["MAILBOX"] == "user@example.com"
    assert cfg["MARSTEK"]["PASSWORD"] == "pw"
    assert cfg["MARSTEK"]["BASE_URL"] == "https://eu.hamedata.com"
    assert cfg["MARSTEK"]["TIMEZONE"] == "Europe/Berlin"


def test_marstek_flag_accepts_the_string_form():
    """bashio handed booleans over as "true"; options.json uses real ones."""
    options = {
        **addon_defaults(),
        "marstek_auto_register_ct_device": "true",
        "marstek_mailbox": "user@example.com",
        "marstek_password": "pw",
    }
    assert "MARSTEK" in parse(generate_config(options)).sections()


def test_no_mqtt_section_without_a_broker():
    assert "MQTT_INSIGHTS" not in parse(generate_config(addon_defaults())).sections()


def test_explicit_mqtt_uri_wins_over_the_supervisor_broker():
    cfg = parse(
        generate_config(
            {**addon_defaults(), "mqtt_uri": "mqtt://a:b@broker:1883"},
            mqtt_service={"host": "core-mosquitto", "port": 1883},
            addon_slug="b2500_meter",
        )
    )
    assert cfg["MQTT_INSIGHTS"]["URI"] == "mqtt://a:b@broker:1883"
    assert "BROKER" not in cfg["MQTT_INSIGHTS"]
    assert cfg["MQTT_INSIGHTS"]["HA_DISCOVERY"] == "True"
    assert cfg["MQTT_INSIGHTS"]["ADDON_SLUG"] == "b2500_meter"


def test_supervisor_broker_is_used_when_offered():
    cfg = parse(
        generate_config(
            addon_defaults(),
            mqtt_service={
                "host": "core-mosquitto",
                "port": 1883,
                "username": "addons",
                "password": "s3cret",
                "ssl": False,
            },
            addon_slug="b2500_meter",
        )
    )
    mqtt = cfg["MQTT_INSIGHTS"]
    assert mqtt["BROKER"] == "core-mosquitto"
    assert mqtt["PORT"] == "1883"
    assert mqtt["USERNAME"] == "addons"
    assert mqtt["PASSWORD"] == "s3cret"
    assert mqtt["TLS"] == "false"
    assert mqtt["HA_DISCOVERY"] == "True"


def test_missing_addon_slug_just_omits_the_link():
    cfg = parse(
        generate_config(addon_defaults(), mqtt_service={"host": "b"}, addon_slug=None)
    )
    assert "ADDON_SLUG" not in cfg["MQTT_INSIGHTS"]


def test_pasted_values_cannot_break_the_ini():
    """A trailing newline in a free-text field used to split the line."""
    cfg = parse(
        generate_config(
            {
                **addon_defaults(),
                "power_offset": "-15\n",
                "power_multiplier": "1.02\r\n",
            }
        )
    )
    assert cfg["HOMEASSISTANT"]["POWER_OFFSET"] == "-15"
    assert cfg["HOMEASSISTANT"]["POWER_MULTIPLIER"] == "1.02"


def test_output_is_stable_and_newline_terminated():
    options = addon_defaults()
    text = generate_config(options)
    assert text == generate_config(options)
    assert text.endswith("\n")
    assert not text.endswith("\n\n")


def test_every_balancer_knob_reaches_the_ct_section():
    """The tuning group run.sh emitted from a loop, one key at a time."""
    knobs = {
        "fair_distribution": ("FAIR_DISTRIBUTION", True),
        "balance_gain": ("BALANCE_GAIN", 0.3),
        "balance_deadband": ("BALANCE_DEADBAND", 12),
        "max_correction_per_step": ("MAX_CORRECTION_PER_STEP", 150),
        "error_boost_threshold": ("ERROR_BOOST_THRESHOLD", 200),
        "error_boost_max": ("ERROR_BOOST_MAX", 2.5),
        "error_reduce_threshold": ("ERROR_REDUCE_THRESHOLD", 40),
        "max_target_step": ("MAX_TARGET_STEP", 300),
        "pace_base_step": ("PACE_BASE_STEP", 60),
        "pace_max_step": ("PACE_MAX_STEP", 400),
        "osc_damp_max": ("OSC_DAMP_MAX", 0.6),
        "osc_damp_alpha": ("OSC_DAMP_ALPHA", 0.2),
        "osc_damp_decay": ("OSC_DAMP_DECAY", 0.9),
        "osc_damp_threshold": ("OSC_DAMP_THRESHOLD", 25),
        "concentrate_deadband": ("CONCENTRATE_DEADBAND", 15),
        "import_trim_w": ("IMPORT_TRIM_W", 5),
        "cloud_reporting_host": ("CLOUD_REPORTING_HOST", "eu.hamedata.com"),
        "cloud_reporting_interval": ("CLOUD_REPORTING_INTERVAL", 60),
    }
    options = {
        **addon_defaults(),
        **{name: value for name, (_, value) in knobs.items()},
    }
    ct = parse(generate_config(options))["CT002"]
    for key, value in knobs.values():
        assert ct[key] == str(value).replace("True", "true")


def test_every_homeassistant_knob_reaches_its_section():
    knobs = {
        "power_offset": ("POWER_OFFSET", "-10"),
        "power_multiplier": ("POWER_MULTIPLIER", "1.01"),
        "smooth_target_alpha": ("SMOOTH_TARGET_ALPHA", 0.4),
        "max_smooth_step": ("MAX_SMOOTH_STEP", 250),
        "deadband": ("DEADBAND", 8),
        "hampel_window": ("HAMPEL_WINDOW", 5),
        "hampel_n_sigma": ("HAMPEL_N_SIGMA", 3),
        "hampel_min_threshold": ("HAMPEL_MIN_THRESHOLD", 30),
        "pid_kp": ("PID_KP", 0.8),
        "pid_ki": ("PID_KI", 0.1),
        "pid_kd": ("PID_KD", 0.0),
        "pid_output_max": ("PID_OUTPUT_MAX", 800),
        "pid_mode": ("PID_MODE", "bias"),
        "dedupe_time_window": ("DEDUPE_TIME_WINDOW", 0.2),
    }
    options = {
        **addon_defaults(),
        **{name: value for name, (_, value) in knobs.items()},
    }
    cfg = parse(generate_config(options))
    assert cfg["GENERAL"]["DEDUPE_TIME_WINDOW"] == "0.2"
    for key, value in knobs.values():
        if key == "DEDUPE_TIME_WINDOW":
            continue
        assert cfg["HOMEASSISTANT"][key] == str(value)
