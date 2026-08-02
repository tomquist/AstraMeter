"""Startup decisions: which config file runs, and what gets logged."""

from __future__ import annotations

import json

import pytest

from . import startup as mod
from .options_test import addon_defaults


@pytest.fixture(autouse=True)
def _no_supervisor(monkeypatch):
    """Keep every test offline; the Supervisor is exercised explicitly."""
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)


def test_read_options_returns_empty_outside_the_addon(tmp_path):
    assert mod.read_options(str(tmp_path / "nope.json")) == {}


def test_read_options_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "options.json"
    path.write_text("{not json", encoding="utf-8")
    assert mod.read_options(str(path)) == {}


def test_read_options_reads_the_supervisor_file(tmp_path):
    path = tmp_path / "options.json"
    path.write_text(json.dumps({"device_types": "ct002"}), encoding="utf-8")
    assert mod.read_options(str(path)) == {"device_types": "ct002"}


def test_generated_config_is_written(tmp_path):
    generated = tmp_path / "config.ini"
    resolved = mod.resolve_config(
        addon_defaults(),
        config_dir=str(tmp_path),
        generated_path=str(generated),
        supervisor=False,
    )
    assert resolved.source == "addon_options"
    assert resolved.path == str(generated)
    assert "[GENERAL]" in generated.read_text(encoding="utf-8")


def test_custom_config_is_read_in_place(tmp_path):
    """It must not be copied: a copy discards every later edit on restart."""
    custom = tmp_path / "mine.ini"
    custom.write_text("[GENERAL]\nDEVICE_TYPE=ct002\n", encoding="utf-8")
    generated = tmp_path / "config.ini"

    resolved = mod.resolve_config(
        {**addon_defaults(), "custom_config": "mine.ini"},
        config_dir=str(tmp_path),
        generated_path=str(generated),
        supervisor=False,
    )

    assert resolved == mod.AddonConfig(str(custom), "custom_config")
    assert not generated.exists()


def test_missing_custom_config_falls_back_and_says_so(tmp_path, caplog):
    generated = tmp_path / "config.ini"
    with caplog.at_level("ERROR"):
        resolved = mod.resolve_config(
            {**addon_defaults(), "custom_config": "typo.ini"},
            config_dir=str(tmp_path),
            generated_path=str(generated),
            supervisor=False,
        )
    assert resolved.source == "addon_options"
    assert "typo.ini" in caplog.text


def test_custom_config_warns_about_options_it_overrides(tmp_path, caplog):
    custom = tmp_path / "mine.ini"
    custom.write_text("[GENERAL]\n", encoding="utf-8")
    with caplog.at_level("WARNING"):
        mod.resolve_config(
            {
                **addon_defaults(),
                "custom_config": "mine.ini",
                "mqtt_uri": "mqtt://broker",
                "marstek_mailbox": "user@example.com",
            },
            config_dir=str(tmp_path),
            generated_path=str(tmp_path / "config.ini"),
            supervisor=False,
        )
    assert "mqtt_uri" in caplog.text
    assert "marstek_mailbox" in caplog.text
    # The default `false` is not something the user set; don't nag about it.
    assert "marstek_auto_register_ct_device" not in caplog.text


def test_bootstrap_logs_the_effective_config_with_secrets_removed(
    tmp_path, caplog, monkeypatch
):
    monkeypatch.setattr(mod, "ADDON_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "GENERATED_CONFIG_PATH", str(tmp_path / "config.ini"))
    options = {
        **addon_defaults(),
        "marstek_auto_register_ct_device": True,
        "marstek_mailbox": "user@example.com",
        "marstek_password": "hunter2",
    }
    with caplog.at_level("INFO"):
        resolved = mod.bootstrap(options)

    assert resolved.source == "addon_options"
    assert "hunter2" not in caplog.text
    assert "user@example.com" not in caplog.text
    assert "PASSWORD = REDACTED" in caplog.text
    # The password is still in the file the service reads.
    with open(resolved.path, encoding="utf-8") as handle:
        assert "hunter2" in handle.read()


def test_bootstrap_reports_an_unreadable_config(tmp_path, monkeypatch):
    unreadable = tmp_path / "gone.ini"
    monkeypatch.setattr(
        mod,
        "resolve_config",
        lambda *a, **k: mod.AddonConfig(str(unreadable), "custom_config"),
    )
    with pytest.raises(RuntimeError, match=r"gone\.ini"):
        mod.bootstrap({})


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("PASSWORD=hunter2", "PASSWORD = REDACTED"),
        ("MAILBOX = user@example.com", "MAILBOX = REDACTED"),
        ("password=hunter2", "password = REDACTED"),
        ("ACCESSTOKEN=abc", "ACCESSTOKEN = REDACTED"),
        ("URI=mqtt://user:pw@broker:1883", "URI=mqtt://***:***@broker:1883"),
        ("BROKER=core-mosquitto", "BROKER=core-mosquitto"),
        ("DEVICE_TYPE=ct002", "DEVICE_TYPE=ct002"),
    ],
)
def test_redact(line, expected):
    assert mod.redact(line) == expected


def test_redact_keeps_the_file_readable():
    text = "[MARSTEK]\nENABLE=True\nPASSWORD=hunter2\nTIMEZONE=Europe/Berlin\n"
    out = mod.redact(text)
    assert out.splitlines()[0] == "[MARSTEK]"
    assert "hunter2" not in out
    assert "TIMEZONE=Europe/Berlin" in out


def test_wait_for_home_assistant_is_a_no_op_without_a_token():
    assert mod.wait_for_home_assistant(timeout=0.0) is True


def test_supervisor_helpers_are_silent_without_a_token():
    assert mod.mqtt_service() is None
    assert mod.addon_slug() is None


def test_mqtt_service_ignores_a_broker_with_no_host(monkeypatch):
    monkeypatch.setattr(mod, "_supervisor_get", lambda path, **kw: {"port": 1883})
    assert mod.mqtt_service() is None


def test_supervisor_helpers_unwrap_the_data_envelope(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
    calls: list[str] = []

    class _Response:
        status = 200

        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _urlopen(request, timeout=None):
        calls.append(request.full_url)
        assert request.headers["Authorization"] == "Bearer t"
        body = {"result": "ok", "data": {"host": "core-mosquitto", "slug": "b2500"}}
        return _Response(json.dumps(body).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", _urlopen)
    assert mod.mqtt_service() == {"host": "core-mosquitto", "slug": "b2500"}
    assert mod.addon_slug() == "b2500"
    assert calls == [
        f"{mod.SUPERVISOR_URL}/services/mqtt",
        f"{mod.SUPERVISOR_URL}/addons/self/info",
    ]


def test_options_path_is_the_resolved_file_not_the_api():
    """`!secret` references are resolved in options.json, raw over the API."""
    assert mod.OPTIONS_PATH == "/data/options.json"
