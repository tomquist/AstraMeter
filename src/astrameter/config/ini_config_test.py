from io import StringIO

from astrameter.config.config_loader import new_config_parser
from astrameter.config.ini_config import IniAppConfig
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
