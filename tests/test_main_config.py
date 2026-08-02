import argparse

from astrameter import main as main_module
from astrameter.config.addon import AddonAppConfig
from astrameter.config.ini_config import IniAppConfig


class NoSupervisor:
    """Supervisor that offers nothing, so no network is touched."""

    base_url = "http://supervisor"

    def __init__(self):
        self.lookups = 0

    def mqtt_service(self):
        self.lookups += 1
        return None

    def addon_slug(self):
        self.lookups += 1
        return ""


def test_marstek_password_with_percent_is_read_verbatim(tmp_path):
    """No interpolation: a literal '%' in a credential survives."""
    path = tmp_path / "config.ini"
    path.write_text(
        "[MARSTEK]\nENABLE = True\nMAILBOX = user@example.com\n"
        "PASSWORD = abc%def/123\n",
        encoding="utf-8",
    )

    marstek = IniAppConfig.from_file(str(path)).marstek()

    assert marstek.enable is True
    assert marstek.password == "abc%def/123"


def test_load_config_reads_the_config_file_by_default(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[GENERAL]\nDEVICE_TYPE = ct002\n", encoding="utf-8")

    args = argparse.Namespace(addon=False, config=str(path))
    config = main_module._load_config(args)

    assert isinstance(config, IniAppConfig)
    assert config.general().device_types == ["ct002"]
    # The web UI offers its config editor for a real file.
    assert config.path == str(path)


def test_load_config_takes_the_addon_options(monkeypatch):
    """--addon skips the file entirely: the add-on options are the config."""
    monkeypatch.setattr(
        main_module.addon, "SupervisorClient", lambda *a, **k: NoSupervisor()
    )

    args = argparse.Namespace(addon=True, config="config.ini")
    config = main_module._load_config(
        args,
        {"device_types": "ct002", "power_input_alias": "sensor.grid_power"},
    )

    assert isinstance(config, AddonAppConfig)
    assert config.general().device_types == ["ct002"]
    # No file backs the options, so the web UI gets no config editor.
    assert config.path is None


def test_load_config_rereads_the_addon_options_on_restart(monkeypatch):
    """A restart re-reads the options, picking up changes made meanwhile."""
    monkeypatch.setattr(
        main_module.addon, "SupervisorClient", lambda *a, **k: NoSupervisor()
    )
    monkeypatch.setattr(
        main_module.addon, "load_options", lambda *a: {"device_types": "ct003"}
    )

    args = argparse.Namespace(addon=True, config="config.ini")
    config = main_module._load_config(args)

    assert config.general().device_types == ["ct003"]


def test_load_config_resolves_the_supervisor_lookups_up_front(monkeypatch):
    """Both startup and the restart branch load through here, so the blocking
    Supervisor lookups can never be left to the running event loop."""
    supervisor = NoSupervisor()
    monkeypatch.setattr(
        main_module.addon, "SupervisorClient", lambda *a, **k: supervisor
    )

    args = argparse.Namespace(addon=True, config="config.ini")
    config = main_module._load_config(args, {"device_types": "ct002"})

    assert supervisor.lookups == 2  # the MQTT service and the add-on slug

    # Asking again is answered from what was already fetched.
    config.mqtt_insights()
    assert supervisor.lookups == 2


def test_cli_throttle_override_reaches_the_power_sources(tmp_path):
    path = tmp_path / "config.ini"
    path.write_text("[GENERAL]\nTHROTTLE_INTERVAL = 1\n", encoding="utf-8")
    config = IniAppConfig.from_file(str(path))
    args = argparse.Namespace(throttle_interval=7.5)

    general = main_module._apply_cli_overrides(config.general(), args)

    assert general.signal.throttle_interval == 7.5
