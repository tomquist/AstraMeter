import argparse
from io import StringIO

from astrameter import main as main_module
from astrameter.config.config_loader import new_config_parser


def test_main_config_parser_allows_percent_in_marstek_password():
    cfg = new_config_parser()
    cfg.read_file(
        StringIO(
            """
[MARSTEK]
ENABLE = True
MAILBOX = user@example.com
PASSWORD = abc%def/123
""".strip()
        )
    )

    assert cfg.get("MARSTEK", "PASSWORD") == "abc%def/123"


def test_load_config_reads_the_config_file_by_default(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[GENERAL]\nDEVICE_TYPE = ct002\n", encoding="utf-8")

    args = argparse.Namespace(addon=False, config=str(path))
    cfg = main_module._load_config(args)

    assert cfg.get("GENERAL", "DEVICE_TYPE") == "ct002"
    assert args.config == str(path)


def test_load_config_builds_from_addon_options(monkeypatch):
    """--addon skips the file entirely: the add-on options are the config."""

    class NoSupervisor:
        def mqtt_service(self):
            return None

        def addon_slug(self):
            return ""

    monkeypatch.setattr(
        main_module.addon, "SupervisorClient", lambda *a, **k: NoSupervisor()
    )

    args = argparse.Namespace(addon=True, config="config.ini")
    cfg = main_module._load_config(
        args,
        {"device_types": "ct002", "power_input_alias": "sensor.grid_power"},
    )

    assert cfg.get("GENERAL", "DEVICE_TYPE") == "ct002"
    assert cfg.get("HOMEASSISTANT", "CURRENT_POWER_ENTITY") == "sensor.grid_power"
    # Nothing on disk backs a generated config, so the web UI gets no path.
    assert args.config is None


def test_load_config_rereads_the_addon_options_on_restart(monkeypatch):
    """A restart re-reads the options, picking up changes made meanwhile."""

    class NoSupervisor:
        def mqtt_service(self):
            return None

        def addon_slug(self):
            return ""

    monkeypatch.setattr(
        main_module.addon, "SupervisorClient", lambda *a, **k: NoSupervisor()
    )
    monkeypatch.setattr(
        main_module.addon, "load_options", lambda *a: {"device_types": "ct003"}
    )

    args = argparse.Namespace(addon=True, config="config.ini")
    cfg = main_module._load_config(args)

    assert cfg.get("GENERAL", "DEVICE_TYPE") == "ct003"
    assert cfg.has_section("CT003")
