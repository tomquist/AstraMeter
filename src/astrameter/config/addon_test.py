from __future__ import annotations

import json
from typing import Any

import pytest

from astrameter.config import addon
from astrameter.config.ini_config import IniAppConfig
from astrameter.config.settings import CtSettings, GeneralSettings
from astrameter.powermeter import HomeAssistant


class FakeSupervisor:
    """Stand-in for :class:`addon.SupervisorClient`."""

    base_url = addon.SUPERVISOR_BASE_URL

    def __init__(
        self,
        mqtt: dict[str, Any] | None = None,
        slug: str = "a0ef98c5_b2500_meter",
        ready: bool = True,
    ) -> None:
        self._mqtt = mqtt
        self._slug = slug
        self._ready = ready
        self.ready_calls = 0

    def mqtt_service(self) -> dict[str, Any] | None:
        return self._mqtt

    def addon_slug(self) -> str:
        return self._slug

    def home_assistant_ready(self) -> bool:
        self.ready_calls += 1
        return self._ready


BASE_OPTIONS: dict[str, Any] = {
    "power_input_alias": "sensor.current_power_in",
    "power_output_alias": "",
    "device_types": "ct002",
    "throttle_interval": 0,
    "wait_for_next_message": True,
    "log_level": "info",
}


def config(options: dict[str, Any], supervisor: Any = None) -> addon.AddonAppConfig:
    client: Any = FakeSupervisor() if supervisor is None else supervisor
    return addon.AddonAppConfig(options, client)


def test_get_option_treats_missing_null_and_empty_as_unset():
    options = {"set": "value", "null": None, "empty": "", "blank": "  ", "zero": 0}
    assert addon.get_option(options, "set") == "value"
    assert addon.get_option(options, "missing") is None
    assert addon.get_option(options, "null", "fallback") == "fallback"
    assert addon.get_option(options, "empty", "fallback") == "fallback"
    assert addon.get_option(options, "blank", "fallback") == "fallback"
    # A numeric 0 / boolean False is a real value the user chose.
    assert addon.get_option(options, "zero", "fallback") == 0


def test_general_settings_come_from_the_options():
    general = config(
        {**BASE_OPTIONS, "device_types": "ct002,ct003", "throttle_interval": 2}
    ).general()
    assert general.device_types == ["ct002", "ct003"]
    assert general.signal.throttle_interval == 2.0
    assert general.signal.wait_for_next_message is True
    # The add-on panel links to the web UI; its editor is for config files.
    assert general.enable_web_server is True
    assert general.web_config_enabled is False


def test_untouched_options_keep_the_settings_defaults():
    general = config(BASE_OPTIONS).general()
    ct = config(BASE_OPTIONS).ct("ct002")
    assert general.dedupe_time_window == GeneralSettings().dedupe_time_window
    assert ct == CtSettings()


def test_ct_settings_are_typed_values_not_strings():
    ct = config(
        {
            **BASE_OPTIONS,
            "ct_mac": "AA:BB:CC:DD:EE:FF",
            "active_control": False,
            "min_efficient_power": 100,
            "grid_predict_trust": 0.25,
            "fair_distribution": True,
            "import_trim_w": 12.5,
            "pace_base_step": 40,
            "cloud_reporting": True,
            "cloud_reporting_host": "eu.hamedata.com",
            "cloud_reporting_interval": 30,
        }
    ).ct("ct002")
    assert ct.ct_mac == "AA:BB:CC:DD:EE:FF"
    assert ct.active_control is False
    assert ct.min_efficient_power == 100
    assert ct.grid_predict_trust == 0.25
    assert ct.fair_distribution is True
    assert ct.import_trim_w == 12.5
    assert ct.pace_base_step == 40
    assert ct.cloud_reporting is True
    assert ct.cloud_reporting_host == "eu.hamedata.com"
    assert ct.cloud_reporting_interval == 30.0


def test_both_ct_emulators_share_the_add_on_settings():
    """The add-on has one set of CT options, whichever emulators run."""
    cfg = config({**BASE_OPTIONS, "device_types": "ct002,ct003", "ct_mac": "AA:BB"})
    assert cfg.ct("ct002") == cfg.ct("ct003")
    assert cfg.ct("ct003").ct_mac == "AA:BB"


def test_global_dedupe_window_reaches_the_ct_emulator():
    cfg = config({**BASE_OPTIONS, "dedupe_time_window": 1.5})
    assert cfg.general().dedupe_time_window == 1.5
    assert cfg.ct("ct002").dedupe_time_window == 1.5


def test_marstek_needs_the_opt_in_and_credentials():
    assert (
        not config(
            {
                **BASE_OPTIONS,
                "marstek_mailbox": "user@example.com",
                "marstek_password": "secret",
            }
        )
        .marstek()
        .enable
    )
    assert (
        not config({**BASE_OPTIONS, "marstek_auto_register_ct_device": True})
        .marstek()
        .enable
    )

    marstek = config(
        {
            **BASE_OPTIONS,
            "marstek_auto_register_ct_device": True,
            "marstek_mailbox": "user@example.com",
            "marstek_password": "pass%word",
        }
    ).marstek()
    assert marstek.enable is True
    assert marstek.mailbox == "user@example.com"
    assert marstek.password == "pass%word"
    assert marstek.base_url == "https://eu.hamedata.com"
    assert marstek.timezone == "Europe/Berlin"


def test_single_power_entity_is_read_directly():
    cfg = config(BASE_OPTIONS)
    meter = cfg.powermeters(cfg.general())[0][0]
    source = meter.wrapped_powermeter
    assert isinstance(source, HomeAssistant)
    assert source.power_calculate is False
    assert source.current_power_entity == ["sensor.current_power_in"]
    # Reached through the Supervisor proxy with the add-on's own token.
    assert (source.ip, source.port, source.path_prefix) == ("supervisor", "80", "/core")


def test_three_phase_entities_are_split_per_phase():
    cfg = config({**BASE_OPTIONS, "power_input_alias": "sensor.a, sensor.b ,sensor.c"})
    source = cfg.powermeters(cfg.general())[0][0].wrapped_powermeter
    assert source.current_power_entity == ["sensor.a", "sensor.b", "sensor.c"]


def test_input_and_output_entities_switch_to_calculated_power():
    cfg = config(
        {
            **BASE_OPTIONS,
            "power_input_alias": "sensor.import",
            "power_output_alias": "sensor.export",
        }
    )
    source = cfg.powermeters(cfg.general())[0][0].wrapped_powermeter
    assert source.power_calculate is True
    assert source.power_input_alias == ["sensor.import"]
    assert source.power_output_alias == ["sensor.export"]


def test_power_source_is_conditioned_by_the_options():
    cfg = config(
        {
            **BASE_OPTIONS,
            "throttle_interval": 5,
            "power_offset": "10,20,30",
            "power_multiplier": " 1.5 \n",
            "smooth_target_alpha": 0.3,
            "deadband": 5,
            "hampel_window": 7,
            "pid_kp": 0.4,
            "wait_for_next_message": False,
        }
    )
    meter, client_filter, wait_for_next_message = cfg.powermeters(cfg.general())[0]
    assert wait_for_next_message is False
    assert client_filter.matches("192.168.1.50")

    # Unwrap the conditioning stack down to the source.
    stack = []
    current = meter
    while hasattr(current, "wrapped_powermeter"):
        stack.append(type(current).__name__)
        current = current.wrapped_powermeter
    assert isinstance(current, HomeAssistant)
    assert stack == [
        "HealthTrackingPowermeter",
        "PidPowermeter",
        "DeadbandPowermeter",
        "SmoothedPowermeter",
        "HampelPowermeter",
        "ThrottledPowermeter",
        "TransformedPowermeter",
    ]


def test_command_line_throttle_override_reaches_the_power_source():
    from dataclasses import replace

    cfg = config({**BASE_OPTIONS, "throttle_interval": 1})
    general = cfg.general()
    general = replace(general, signal=replace(general.signal, throttle_interval=9))
    meter = cfg.powermeters(general)[0][0]
    throttled = meter.wrapped_powermeter
    assert type(throttled).__name__ == "ThrottledPowermeter"
    assert throttled.throttle_interval == 9


def test_mqtt_uses_home_assistant_broker_when_offered():
    supervisor = FakeSupervisor(
        mqtt={
            "host": "core-mosquitto",
            "port": 1883,
            "username": "addons",
            "password": "secret",
            "ssl": False,
        }
    )
    insights = config(BASE_OPTIONS, supervisor).mqtt_insights()
    assert insights is not None
    assert insights.broker == "core-mosquitto"
    assert insights.port == 1883
    assert insights.username == "addons"
    assert insights.password == "secret"
    assert insights.tls is False
    assert insights.ha_discovery is True
    assert insights.addon_slug == "a0ef98c5_b2500_meter"


def test_custom_mqtt_uri_wins_over_the_home_assistant_broker():
    supervisor = FakeSupervisor(mqtt={"host": "core-mosquitto", "port": 1883})
    insights = config(
        {**BASE_OPTIONS, "mqtt_uri": "mqtts://user:pw@broker:8883"}, supervisor
    ).mqtt_insights()
    assert insights is not None
    assert (insights.broker, insights.port, insights.tls) == ("broker", 8883, True)
    assert insights.username == "user"
    assert insights.password == "pw"


def test_no_mqtt_without_a_broker():
    assert config(BASE_OPTIONS, FakeSupervisor(mqtt=None)).mqtt_insights() is None


def test_missing_addon_slug_is_simply_omitted():
    supervisor = FakeSupervisor(mqtt={"host": "core-mosquitto"}, slug="")
    insights = config(BASE_OPTIONS, supervisor).mqtt_insights()
    assert insights is not None
    assert insights.addon_slug is None


def test_options_are_read_on_every_call():
    """No snapshot is taken: the options stay the single source of truth."""
    options = dict(BASE_OPTIONS)
    cfg = config(options)
    assert cfg.ct("ct002").active_control is True

    options["active_control"] = False
    assert cfg.ct("ct002").active_control is False


def test_load_options_reads_the_supervisor_file(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"device_types": "ct002"}), encoding="utf-8")
    assert addon.load_options(str(path)) == {"device_types": "ct002"}


@pytest.mark.parametrize("content", ["not json", '["a", "b"]'])
def test_load_options_survives_a_broken_file(tmp_path, content):
    path = tmp_path / "options.json"
    path.write_text(content, encoding="utf-8")
    assert addon.load_options(str(path)) == {}


def test_load_options_survives_a_missing_file(tmp_path):
    assert addon.load_options(str(tmp_path / "nope.json")) == {}


def test_custom_config_file_replaces_the_add_on_options(tmp_path):
    (tmp_path / "my.ini").write_text(
        "[GENERAL]\nDEVICE_TYPE = ct003\n", encoding="utf-8"
    )
    cfg = addon.load_config(
        {**BASE_OPTIONS, "custom_config": "my.ini"},
        FakeSupervisor(),
        config_dir=str(tmp_path),
    )
    assert isinstance(cfg, IniAppConfig)
    assert cfg.path == str(tmp_path / "my.ini")
    assert cfg.general().device_types == ["ct003"]


def test_unknown_custom_config_falls_back_to_the_options(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        cfg = addon.load_config(
            {**BASE_OPTIONS, "custom_config": "missing.ini"},
            FakeSupervisor(),
            config_dir=str(tmp_path),
        )
    assert isinstance(cfg, addon.AddonAppConfig)
    # Nothing on disk backs the options, so the web UI gets no config editor.
    assert cfg.path is None
    assert "missing.ini" in caplog.text


@pytest.mark.parametrize(
    "name",
    ["/etc/passwd", "../outside.ini", "nested/../../outside.ini"],
)
def test_custom_config_cannot_escape_the_addon_config_mount(tmp_path, caplog, name):
    """The option names a file in the add-on's config mount, nothing else."""
    (tmp_path.parent / "outside.ini").write_text("[GENERAL]\n", encoding="utf-8")
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    with caplog.at_level("WARNING"):
        cfg = addon.load_config(
            {**BASE_OPTIONS, "custom_config": name},
            FakeSupervisor(),
            config_dir=str(config_dir),
        )

    # Refused, so the add-on options still apply.
    assert isinstance(cfg, addon.AddonAppConfig)
    assert cfg.path is None
    assert "outside" in caplog.text or "not found" in caplog.text


def test_custom_config_warns_about_ignored_ui_options(tmp_path, caplog):
    (tmp_path / "my.ini").write_text("[GENERAL]\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        addon.load_config(
            {
                **BASE_OPTIONS,
                "custom_config": "my.ini",
                "marstek_mailbox": "user@example.com",
                "mqtt_uri": "mqtt://broker",
            },
            FakeSupervisor(),
            config_dir=str(tmp_path),
        )
    assert "Marstek settings are ignored" in caplog.text
    assert "mqtt_uri is ignored" in caplog.text


def _custom(tmp_path, caplog, body, level="WARNING"):
    """Load *body* as the add-on's custom config file and return its general()."""
    (tmp_path / "my.ini").write_text(body, encoding="utf-8")
    with caplog.at_level(level):
        cfg = addon.load_config(
            {**BASE_OPTIONS, "custom_config": "my.ini"},
            FakeSupervisor(),
            config_dir=str(tmp_path),
        )
        return cfg.general()


def test_custom_config_still_serves_the_dashboard(tmp_path, caplog):
    """A file predating the dashboard must not leave the panel on a 404.

    ``DASHBOARD_ENABLED`` is off by default for a bare Docker run, where the
    web port is unauthenticated. Under the add-on the panel and the watchdog
    both depend on it, so the file's default cannot be allowed to decide.
    """
    general = _custom(tmp_path, caplog, "[GENERAL]\nDEVICE_TYPE = ct003\n")

    assert general.dashboard is True
    assert general.enable_web_server is True
    # The file never mentioned either key, so there is nothing to warn about.
    assert "Ignoring" not in caplog.text


def test_custom_config_cannot_turn_the_dashboard_or_web_server_off(tmp_path, caplog):
    general = _custom(
        tmp_path,
        caplog,
        "[GENERAL]\nDASHBOARD_ENABLED = False\nENABLE_WEB_SERVER = False\n",
    )

    assert general.dashboard is True
    assert general.enable_web_server is True
    # Named so the user can find them, and told what to reach for instead.
    assert "ENABLE_WEB_SERVER and DASHBOARD_ENABLED" in caplog.text
    assert "DASHBOARD_ALLOW_WRITE" in caplog.text


def test_custom_config_keeps_the_rest_of_its_dashboard_settings(tmp_path, caplog):
    """Only *whether* it runs is forced — what it may do stays the file's."""
    general = _custom(
        tmp_path,
        caplog,
        "[GENERAL]\nDASHBOARD_ALLOW_WRITE = True\nDASHBOARD_DIRECT_ACCESS = True\n",
    )

    assert general.dashboard_allow_write is True
    assert general.dashboard_direct_access is True


def test_wait_for_home_assistant_returns_once_the_api_answers():
    class LateSupervisor(FakeSupervisor):
        def home_assistant_ready(self) -> bool:
            self.ready_calls += 1
            return self.ready_calls >= 3

    supervisor = LateSupervisor()
    slept: list[float] = []
    assert addon.wait_for_home_assistant(
        supervisor, attempts=10, delay=5.0, sleep=slept.append
    )
    assert supervisor.ready_calls == 3
    assert slept == [5.0, 5.0]


def test_wait_for_home_assistant_gives_up_but_lets_the_app_start(caplog):
    supervisor = FakeSupervisor(ready=False)
    with caplog.at_level("WARNING"):
        assert not addon.wait_for_home_assistant(
            supervisor, attempts=3, delay=1.0, sleep=lambda _: None
        )
    assert supervisor.ready_calls == 3
    assert "continuing anyway" in caplog.text


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def patch_requests(monkeypatch, responses: dict[str, Any]):
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers or {}))
        response = responses.get(url)
        if isinstance(response, Exception):
            raise response
        assert response is not None, f"unexpected request to {url}"
        return response

    monkeypatch.setattr(addon.requests, "get", fake_get)
    return calls


def test_supervisor_client_reads_the_mqtt_service(monkeypatch):
    service = {"host": "core-mosquitto", "port": 1883, "ssl": False}
    calls = patch_requests(
        monkeypatch,
        {
            "http://supervisor/services/mqtt": FakeResponse(
                200, {"result": "ok", "data": service}
            )
        },
    )
    client = addon.SupervisorClient(token="tok")
    assert client.mqtt_service() == service
    assert calls[0][1]["Authorization"] == "Bearer tok"


def test_supervisor_client_reports_no_mqtt_service_when_unavailable(monkeypatch):
    patch_requests(
        monkeypatch,
        {"http://supervisor/services/mqtt": FakeResponse(400, {"result": "error"})},
    )
    assert addon.SupervisorClient(token="tok").mqtt_service() is None


def test_supervisor_client_survives_a_network_error(monkeypatch):
    patch_requests(
        monkeypatch,
        {"http://supervisor/addons/self/info": addon.requests.ConnectionError("boom")},
    )
    assert addon.SupervisorClient(token="tok").addon_slug() == ""


def test_supervisor_client_reads_the_addon_slug(monkeypatch):
    patch_requests(
        monkeypatch,
        {
            "http://supervisor/addons/self/info": FakeResponse(
                200, {"result": "ok", "data": {"slug": "a0ef98c5_b2500_meter"}}
            )
        },
    )
    assert addon.SupervisorClient(token="tok").addon_slug() == "a0ef98c5_b2500_meter"


def test_home_assistant_ready_checks_the_core_api(monkeypatch):
    patch_requests(
        monkeypatch,
        {"http://supervisor/core/api/": FakeResponse(200, {"message": "ok"})},
    )
    assert addon.SupervisorClient(token="tok").home_assistant_ready() is True


def test_supervisor_token_defaults_to_the_environment(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "from-env")
    assert addon.SupervisorClient().token == "from-env"
