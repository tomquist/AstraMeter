from __future__ import annotations

import json
from configparser import NoOptionError, NoSectionError
from typing import Any

import pytest

from astrameter.config import addon
from astrameter.config.config_loader import (
    read_all_powermeter_configs,
    read_mqtt_insights_config,
)
from astrameter.powermeter import HomeAssistant


class FakeSupervisor:
    """Stand-in for :class:`addon.SupervisorClient` in config-building tests."""

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


def build(options: dict[str, Any], supervisor: Any = None) -> addon.AddonConfig:
    client: Any = FakeSupervisor() if supervisor is None else supervisor
    return addon.AddonConfig(options, client)


def test_get_option_treats_missing_null_and_empty_as_unset():
    options = {"set": "value", "null": None, "empty": "", "blank": "  ", "zero": 0}
    assert addon.get_option(options, "set") == "value"
    assert addon.get_option(options, "missing") is None
    assert addon.get_option(options, "null", "fallback") == "fallback"
    assert addon.get_option(options, "empty", "fallback") == "fallback"
    assert addon.get_option(options, "blank", "fallback") == "fallback"
    # A numeric 0 / boolean False is a real value the user chose.
    assert addon.get_option(options, "zero", "fallback") == 0


def test_general_section_maps_device_types_and_enables_web_server():
    cfg = build({**BASE_OPTIONS, "device_types": "ct002,ct003", "throttle_interval": 2})
    assert cfg.get("GENERAL", "DEVICE_TYPE") == "ct002,ct003"
    assert cfg.get("GENERAL", "THROTTLE_INTERVAL") == "2"
    assert cfg.getboolean("GENERAL", "ENABLE_WEB_SERVER") is True
    assert not cfg.has_option("GENERAL", "DEDUPE_TIME_WINDOW")


def test_unset_options_are_left_out_so_app_defaults_apply():
    cfg = build(BASE_OPTIONS)
    for key in ("ACTIVE_CONTROL", "MIN_DC_OUTPUT", "PACE_BASE_STEP", "IMPORT_TRIM_W"):
        assert not cfg.has_option("CT002", key)
    assert not cfg.has_option("HOMEASSISTANT", "PID_KP")


@pytest.mark.parametrize(
    ("device_types", "expected"),
    [
        ("ct002", ["CT002"]),
        ("ct003", ["CT003"]),
        ("CT003", ["CT003"]),
        ("ct002,ct003", ["CT002", "CT003"]),
        ("shellypro3em", ["CT002"]),
    ],
)
def test_ct_sections_follow_configured_device_types(device_types, expected):
    cfg = build({**BASE_OPTIONS, "device_types": device_types, "ct_mac": "AA:BB:CC"})
    assert [s for s in cfg.sections() if s.startswith("CT00")] == expected
    for section in expected:
        assert cfg.get(section, "CT_MAC") == "AA:BB:CC"


def test_ct_tuning_options_land_in_every_ct_section():
    cfg = build(
        {
            **BASE_OPTIONS,
            "device_types": "ct002,ct003",
            "active_control": False,
            "min_efficient_power": 100,
            "grid_predict_trust": 0.25,
            "fair_distribution": True,
            "import_trim_w": 12.5,
            "cloud_reporting": True,
            "cloud_reporting_host": "eu.hamedata.com",
            "cloud_reporting_interval": 30,
        }
    )
    for section in ("CT002", "CT003"):
        assert cfg.getboolean(section, "ACTIVE_CONTROL") is False
        assert cfg.getint(section, "MIN_EFFICIENT_POWER") == 100
        assert cfg.getfloat(section, "GRID_PREDICT_TRUST") == 0.25
        assert cfg.getboolean(section, "FAIR_DISTRIBUTION") is True
        assert cfg.getfloat(section, "IMPORT_TRIM_W") == 12.5
        assert cfg.getboolean(section, "CLOUD_REPORTING") is True
        assert cfg.get(section, "CLOUD_REPORTING_HOST") == "eu.hamedata.com"
        assert cfg.getfloat(section, "CLOUD_REPORTING_INTERVAL") == 30


def test_single_power_entity_is_read_directly():
    cfg = build(BASE_OPTIONS)
    assert cfg.get("HOMEASSISTANT", "IP") == "supervisor"
    assert cfg.get("HOMEASSISTANT", "PORT") == "80"
    assert cfg.get("HOMEASSISTANT", "API_PATH_PREFIX") == "/core"
    assert cfg.getboolean("HOMEASSISTANT", "POWER_CALCULATE") is False
    assert cfg.get("HOMEASSISTANT", "CURRENT_POWER_ENTITY") == "sensor.current_power_in"
    assert cfg.getboolean("HOMEASSISTANT", "WAIT_FOR_NEXT_MESSAGE") is True


def test_input_and_output_entities_switch_to_calculated_power():
    cfg = build(
        {
            **BASE_OPTIONS,
            "power_input_alias": "sensor.import",
            "power_output_alias": "sensor.export",
        }
    )
    assert cfg.getboolean("HOMEASSISTANT", "POWER_CALCULATE") is True
    assert cfg.get("HOMEASSISTANT", "POWER_INPUT_ALIAS") == "sensor.import"
    assert cfg.get("HOMEASSISTANT", "POWER_OUTPUT_ALIAS") == "sensor.export"
    assert not cfg.has_option("HOMEASSISTANT", "CURRENT_POWER_ENTITY")


def test_signal_conditioning_options_are_forwarded():
    cfg = build(
        {
            **BASE_OPTIONS,
            "power_offset": "10,20,30",
            "power_multiplier": "1.0",
            "smooth_target_alpha": 0.3,
            "max_smooth_step": 50,
            "deadband": 5,
            "hampel_window": 7,
            "hampel_n_sigma": 3.5,
            "hampel_min_threshold": 20,
            "pid_kp": 0.4,
            "pid_ki": 0.01,
            "pid_kd": 0,
            "pid_output_max": 900,
            "pid_mode": "bias",
        }
    )
    assert cfg.get("HOMEASSISTANT", "POWER_OFFSET") == "10,20,30"
    assert cfg.get("HOMEASSISTANT", "POWER_MULTIPLIER") == "1.0"
    assert cfg.getfloat("HOMEASSISTANT", "SMOOTH_TARGET_ALPHA") == 0.3
    assert cfg.getfloat("HOMEASSISTANT", "MAX_SMOOTH_STEP") == 50
    assert cfg.getfloat("HOMEASSISTANT", "DEADBAND") == 5
    assert cfg.getint("HOMEASSISTANT", "HAMPEL_WINDOW") == 7
    assert cfg.getfloat("HOMEASSISTANT", "HAMPEL_N_SIGMA") == 3.5
    assert cfg.getfloat("HOMEASSISTANT", "HAMPEL_MIN_THRESHOLD") == 20
    assert cfg.getfloat("HOMEASSISTANT", "PID_KP") == 0.4
    assert cfg.getfloat("HOMEASSISTANT", "PID_KI") == 0.01
    assert cfg.getfloat("HOMEASSISTANT", "PID_KD") == 0
    assert cfg.getfloat("HOMEASSISTANT", "PID_OUTPUT_MAX") == 900
    assert cfg.get("HOMEASSISTANT", "PID_MODE") == "bias"


def test_power_offset_strips_stray_newlines():
    cfg = build({**BASE_OPTIONS, "power_offset": " 10 \n"})
    assert cfg.get("HOMEASSISTANT", "POWER_OFFSET") == "10"


def test_marstek_section_needs_opt_in_and_credentials():
    without_opt_in = build(
        {
            **BASE_OPTIONS,
            "marstek_mailbox": "user@example.com",
            "marstek_password": "secret",
        }
    )
    assert not without_opt_in.has_section("MARSTEK")

    without_credentials = build(
        {**BASE_OPTIONS, "marstek_auto_register_ct_device": True}
    )
    assert not without_credentials.has_section("MARSTEK")

    cfg = build(
        {
            **BASE_OPTIONS,
            "marstek_auto_register_ct_device": True,
            "marstek_mailbox": "user@example.com",
            "marstek_password": "pass%word",
        }
    )
    assert cfg.getboolean("MARSTEK", "ENABLE") is True
    assert cfg.get("MARSTEK", "BASE_URL") == "https://eu.hamedata.com"
    assert cfg.get("MARSTEK", "MAILBOX") == "user@example.com"
    # No interpolation: a literal '%' survives.
    assert cfg.get("MARSTEK", "PASSWORD") == "pass%word"
    assert cfg.get("MARSTEK", "TIMEZONE") == "Europe/Berlin"


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
    cfg = build(BASE_OPTIONS, supervisor)
    insights = read_mqtt_insights_config(cfg)
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
    cfg = build({**BASE_OPTIONS, "mqtt_uri": "mqtts://user:pw@broker:8883"}, supervisor)
    assert not cfg.has_option("MQTT_INSIGHTS", "BROKER")
    insights = read_mqtt_insights_config(cfg)
    assert insights is not None
    assert (insights.broker, insights.port, insights.tls) == ("broker", 8883, True)
    assert insights.username == "user"


def test_no_mqtt_section_without_a_broker():
    cfg = build(BASE_OPTIONS, FakeSupervisor(mqtt=None))
    assert not cfg.has_section("MQTT_INSIGHTS")
    assert read_mqtt_insights_config(cfg) is None


def test_missing_addon_slug_is_simply_omitted():
    supervisor = FakeSupervisor(mqtt={"host": "core-mosquitto"}, slug="")
    cfg = build(BASE_OPTIONS, supervisor)
    assert not cfg.has_option("MQTT_INSIGHTS", "ADDON_SLUG")


def test_addon_config_yields_a_home_assistant_powermeter():
    cfg = build(BASE_OPTIONS)
    powermeters = read_all_powermeter_configs(cfg)
    assert len(powermeters) == 1
    powermeter = powermeters[0][0]
    inner = getattr(powermeter, "wrapped_powermeter", powermeter)
    assert isinstance(inner, HomeAssistant)


def test_values_are_read_from_the_options_on_every_lookup():
    """No snapshot is taken: the options stay the single source of truth."""
    options = dict(BASE_OPTIONS)
    cfg = build(options)
    assert cfg.get("HOMEASSISTANT", "CURRENT_POWER_ENTITY") == "sensor.current_power_in"

    options["power_input_alias"] = "sensor.other"
    assert cfg.get("HOMEASSISTANT", "CURRENT_POWER_ENTITY") == "sensor.other"


def test_runtime_overrides_win_over_the_options():
    """CLI flags (--throttle-interval) assign into the same config object."""
    cfg = build({**BASE_OPTIONS, "throttle_interval": 1})
    cfg.set("GENERAL", "THROTTLE_INTERVAL", "5")
    assert cfg.getfloat("GENERAL", "THROTTLE_INTERVAL") == 5.0
    # Untouched keys still come from the add-on options.
    assert cfg.get("GENERAL", "DEVICE_TYPE") == "ct002"


def test_unknown_keys_raise_or_fall_back_like_a_config_file():
    cfg = build(BASE_OPTIONS)
    assert cfg.get("GENERAL", "NOPE", fallback="default") == "default"
    assert cfg.getint("CT002", "UDP_PORT", fallback=12345) == 12345
    with pytest.raises(NoOptionError):
        cfg.get("GENERAL", "NOPE")
    with pytest.raises(NoSectionError):
        cfg.get("NOPE", "NOPE")


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


def test_custom_config_file_replaces_the_generated_config(tmp_path):
    (tmp_path / "my.ini").write_text(
        "[GENERAL]\nDEVICE_TYPE = ct003\n", encoding="utf-8"
    )
    cfg, path = addon.load_config(
        {**BASE_OPTIONS, "custom_config": "my.ini"},
        FakeSupervisor(),
        config_dir=str(tmp_path),
    )
    assert path == str(tmp_path / "my.ini")
    assert cfg.get("GENERAL", "DEVICE_TYPE") == "ct003"
    assert not cfg.has_section("HOMEASSISTANT")


def test_unknown_custom_config_falls_back_to_the_options(tmp_path, caplog):
    with caplog.at_level("WARNING"):
        cfg, path = addon.load_config(
            {**BASE_OPTIONS, "custom_config": "missing.ini"},
            FakeSupervisor(),
            config_dir=str(tmp_path),
        )
    assert path is None
    assert cfg.get("HOMEASSISTANT", "IP") == "supervisor"
    assert "missing.ini" in caplog.text


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


def test_generated_config_is_logged_with_credentials_masked(caplog):
    cfg = build(
        {
            **BASE_OPTIONS,
            "marstek_auto_register_ct_device": True,
            "marstek_mailbox": "user@example.com",
            "marstek_password": "topsecret",
        }
    )
    with caplog.at_level("INFO"):
        addon.log_config(cfg)
    rendered = "\n".join(
        record.getMessage() for record in caplog.records if record.name == "astrameter"
    )
    assert "[MARSTEK]" in rendered
    from astrameter.config.logger import redact_secrets

    assert "topsecret" not in redact_secrets(rendered)
    assert "user@example.com" not in redact_secrets(rendered)


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
        {
            "http://supervisor/addons/self/info": addon.requests.ConnectionError(
                "boom"
            ),
        },
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
