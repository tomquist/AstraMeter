import dataclasses
from io import StringIO

from astrameter.config.config_loader import new_config_parser
from astrameter.config.ini_config import IniAppConfig, render_ini
from astrameter.config.settings import CtSettings, GeneralSettings


def config(text: str) -> IniAppConfig:
    parser = new_config_parser()
    parser.read_file(StringIO(text))
    return IniAppConfig(parser)


def test_empty_config_yields_the_defaults():
    cfg = config("")
    assert cfg.general() == GeneralSettings()
    assert cfg.ct("ct002") == CtSettings()
    assert cfg.marstek().enable is False


def test_the_dashboard_is_served_and_writable_unless_the_file_says_otherwise():
    """Both on by default; each takes its own key to switch off."""
    assert config("").general().dashboard is True
    assert config("").general().dashboard_allow_write is True
    assert config("[GENERAL]\nDASHBOARD_ENABLED = False\n").general().dashboard is False
    assert (
        config("[GENERAL]\nDASHBOARD_ALLOW_WRITE = False\n")
        .general()
        .dashboard_allow_write
        is False
    )


def test_general_section_is_read_into_settings():
    general = config(
        """
[GENERAL]
DEVICE_TYPE = ct002, ct003
DEVICE_IDS = one, two
SKIP_POWERMETER_TEST = true
DEDUPE_TIME_WINDOW = 1.5
ENABLE_WEB_SERVER = false
WEB_CONFIG_ENABLED = true
WEB_SERVER_PORT = 8080
THROTTLE_INTERVAL = 2
"""
    ).general()
    assert general.device_types == ["ct002", "ct003"]
    assert general.device_ids == ["one", "two"]
    assert general.skip_powermeter_test is True
    assert general.dedupe_time_window == 1.5
    assert general.enable_web_server is False
    assert general.web_config_enabled is True
    assert general.web_server_port == 8080
    # The global conditioning every power source starts from.
    assert general.signal.throttle_interval == 2.0


def test_ct_section_is_read_into_settings():
    ct = config(
        """
[CT002]
CT_MAC = AA:BB:CC:DD:EE:FF
UDP_PORT = 12346
ACTIVE_CONTROL = false
BALANCE_GAIN = 0.35
PACE_MAX_STEP = 150
MIN_DC_OUTPUT = 50
CLOUD_REPORTING = true
"""
    ).ct("ct002")
    assert ct.ct_mac == "AA:BB:CC:DD:EE:FF"
    assert ct.udp_port == 12346
    assert ct.active_control is False
    assert ct.balance_gain == 0.35
    assert ct.pace_max_step == 150
    assert ct.min_dc_output == 50.0
    assert ct.cloud_reporting is True
    assert ct.cloud_reporting_host == "eu.hamedata.com"
    # Untouched keys keep their defaults.
    assert ct.fair_distribution is CtSettings().fair_distribution


def test_ct003_reads_its_own_section_only_when_it_exists():
    both = config("[CT002]\nCT_MAC = aa\n\n[CT003]\nCT_MAC = bb\n")
    assert both.ct("ct002").ct_mac == "aa"
    assert both.ct("ct003").ct_mac == "bb"

    shared = config("[CT002]\nCT_MAC = aa\n")
    assert shared.ct("ct003").ct_mac == "aa"


def test_ct_dedupe_window_falls_back_to_the_global_one():
    inherited = config("[GENERAL]\nDEDUPE_TIME_WINDOW = 2\n\n[CT002]\n")
    assert inherited.ct("ct002").dedupe_time_window == 2.0

    overridden = config(
        "[GENERAL]\nDEDUPE_TIME_WINDOW = 2\n\n[CT002]\nDEDUPE_TIME_WINDOW = 0.5\n"
    )
    assert overridden.ct("ct002").dedupe_time_window == 0.5


def test_marstek_section_is_read_into_settings():
    marstek = config(
        """
[MARSTEK]
ENABLE = True
MAILBOX = user@example.com
PASSWORD = secret
BASE_URL = https://us.hamedata.com
TIMEZONE = Europe/Madrid
"""
    ).marstek()
    assert marstek.enable is True
    assert marstek.mailbox == "user@example.com"
    assert marstek.password == "secret"
    assert marstek.base_url == "https://us.hamedata.com"
    assert marstek.timezone == "Europe/Madrid"


def test_powermeters_inherit_the_global_conditioning():
    cfg = config(
        """
[GENERAL]
THROTTLE_INTERVAL = 3

[SHELLY]
TYPE = 3EMPro
IP = 192.168.1.10
"""
    )
    meter, _, _ = cfg.powermeters(cfg.general())[0]
    assert type(meter.wrapped_powermeter).__name__ == "ThrottledPowermeter"


# -- rendering ---------------------------------------------------------------
#
# The reader is the oracle: whatever render_ini writes has to come back as the
# settings it was given. That is what stops the dashboard's "switch to a config
# file" from handing someone a file that silently drops a setting.


def _round_trip(cfg: IniAppConfig, device_types: list[str] | None = None):
    return config(render_ini(cfg, device_types))


def _sample(field: dataclasses.Field) -> str:
    """A value for *field* that is not its default, so writing it is visible."""
    default = field.default
    if isinstance(default, bool):
        return "false" if default else "true"
    if isinstance(default, int):
        return str(default + 7)
    if isinstance(default, float):
        return str(round(default + 0.25, 4))
    return f"{default}-changed" if default else "changed"


def test_rendering_the_defaults_writes_nothing_to_carry():
    """Only what the user changed is worth putting in their file."""
    cfg = config("")
    assert render_ini(cfg).strip() == ""
    assert _round_trip(cfg).general() == GeneralSettings()


def test_general_settings_survive_the_round_trip():
    cfg = config(
        """
[GENERAL]
DEVICE_TYPE = ct002, ct003
DEVICE_IDS = one, two
SKIP_POWERMETER_TEST = true
DEDUPE_TIME_WINDOW = 1.5
ENABLE_WEB_SERVER = false
WEB_CONFIG_ENABLED = true
WEB_SERVER_PORT = 8080
DASHBOARD_ENABLED = true
DASHBOARD_ALLOW_WRITE = true
DASHBOARD_DIRECT_ACCESS = true
THROTTLE_INTERVAL = 2.5
WAIT_FOR_NEXT_MESSAGE = false
SMOOTH_TARGET_ALPHA = 0.4
MAX_SMOOTH_STEP = 250
DEADBAND = 8
HAMPEL_WINDOW = 5
HAMPEL_N_SIGMA = 2.5
HAMPEL_MIN_THRESHOLD = 30
PID_KP = 0.8
PID_KI = 0.1
PID_KD = 0.2
PID_OUTPUT_MAX = 900
PID_MODE = replace
"""
    )
    assert _round_trip(cfg).general() == cfg.general()


def test_ct_settings_survive_the_round_trip():
    """Every CT field, so a key written under the wrong name is caught."""
    body = "\n".join(
        f"{f.name.upper()} = {_sample(f)}"
        for f in dataclasses.fields(CtSettings)
        if f.name != "consumer_ttl"
    )
    cfg = config(f"[GENERAL]\nDEVICE_TYPE = ct002\n\n[CT002]\n{body}\n")
    assert _round_trip(cfg).ct("ct002") == cfg.ct("ct002")


def test_both_ct_sections_are_rendered():
    cfg = config(
        """
[GENERAL]
DEVICE_TYPE = ct002, ct003

[CT002]
BALANCE_GAIN = 0.4

[CT003]
BALANCE_GAIN = 0.6
"""
    )
    written = _round_trip(cfg)
    assert written.ct("ct002").balance_gain == 0.4
    assert written.ct("ct003").balance_gain == 0.6


def test_marstek_credentials_survive_the_round_trip():
    cfg = config(
        """
[MARSTEK]
ENABLE = true
MAILBOX = user@example.com
PASSWORD = secret
BASE_URL = https://us.hamedata.com
TIMEZONE = Europe/Madrid
"""
    )
    assert _round_trip(cfg).marstek() == cfg.marstek()


def test_a_disabled_marstek_section_is_not_written():
    """Credentials that are not in use do not get copied into a new file."""
    cfg = config("[MARSTEK]\nENABLE = false\nPASSWORD = secret\n")
    assert "secret" not in render_ini(cfg)


def test_the_device_types_to_render_can_be_given():
    """The add-on backend answers ct() for a type its own list may not name."""
    cfg = config("")
    assert "[CT003]" in render_ini(cfg, ["ct003"])


def test_an_all_default_ct003_does_not_inherit_ct002s_settings():
    """`ct_section` falls back to [CT002], so [CT003] has to be written."""
    cfg = config(
        """
[GENERAL]
DEVICE_TYPE = ct002, ct003

[CT002]
BALANCE_GAIN = 0.44

[CT003]
"""
    )
    assert cfg.ct("ct003") == CtSettings()
    assert _round_trip(cfg).ct("ct003") == CtSettings()


def test_declared_general_keys_reports_only_what_the_file_sets():
    """`dashboard` is not `DASHBOARD`, so the lookup goes through the map."""
    cfg = config("[GENERAL]\nDASHBOARD_ENABLED = False\n")

    assert cfg.declared_general_keys("dashboard") == ["DASHBOARD_ENABLED"]
    assert cfg.declared_general_keys("enable_web_server") == []
    assert cfg.declared_general_keys("enable_web_server", "dashboard") == [
        "DASHBOARD_ENABLED"
    ]
