"""``astrameter --addon`` end to end: options.json in, parsed config out.

The generator has its own tests; this one covers the wiring around it, which
is where the shell version kept breaking — the service starting against the
wrong file, or against nothing at all.
"""

from __future__ import annotations

import json
import sys

import pytest

from astrameter import main as main_module

from . import startup
from .options_test import addon_defaults


@pytest.fixture
def run_main(monkeypatch, tmp_path):
    """Run ``main()`` up to the event loop and hand back the parsed config."""
    monkeypatch.setattr(startup, "ADDON_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(startup, "GENERATED_CONFIG_PATH", str(tmp_path / "config.ini"))
    monkeypatch.setattr(startup, "wait_for_home_assistant", lambda **kw: True)
    monkeypatch.setattr(startup, "sync_ingress_panel", lambda wanted, info=None: None)

    captured: dict = {}

    def _run(coro):
        coro.close()

    monkeypatch.setattr(main_module.asyncio, "run", _run)

    def _supervise(cfg, args, registry):
        captured["cfg"] = cfg
        captured["args"] = args
        captured["registry"] = registry
        captured["device_types"] = main_module._resolve_device_config(cfg, args)[0]

        async def _noop():
            return None

        return _noop()

    monkeypatch.setattr(main_module, "_supervise", _supervise)

    def _run_cli(options: dict | None, *argv: str) -> dict:
        if options is not None:
            path = tmp_path / "options.json"
            path.write_text(json.dumps(options), encoding="utf-8")
            monkeypatch.setattr(startup, "OPTIONS_PATH", str(path))
        else:
            monkeypatch.setattr(startup, "OPTIONS_PATH", str(tmp_path / "absent.json"))
        monkeypatch.setattr(sys, "argv", ["astrameter", *argv])
        main_module.main()
        return captured

    return _run_cli


def test_addon_run_uses_the_generated_config(run_main, tmp_path):
    captured = run_main({**addon_defaults(), "device_types": "ct002"}, "--addon")

    assert captured["args"].config == str(tmp_path / "config.ini")
    assert captured["device_types"] == ["ct002"]
    cfg = captured["cfg"]
    assert cfg["GENERAL"]["ENABLE_WEB_SERVER"] == "true"
    assert cfg["HOMEASSISTANT"]["IP"] == "supervisor"
    assert cfg["CT002"]["ACTIVE_CONTROL"] == "true"


def test_addon_run_uses_a_custom_config_in_place(run_main, tmp_path):
    custom = tmp_path / "mine.ini"
    custom.write_text(
        "[GENERAL]\nDEVICE_TYPE=ct003\n[CT003]\nCT_MAC=aa:bb:cc:dd:ee:ff\n",
        encoding="utf-8",
    )
    captured = run_main(
        {**addon_defaults(), "custom_config": "mine.ini"},
        "--addon",
    )

    assert captured["args"].config == str(custom)
    assert captured["device_types"] == ["ct003"]
    assert captured["cfg"]["CT003"]["CT_MAC"] == "aa:bb:cc:dd:ee:ff"
    assert not (tmp_path / "config.ini").exists()


def test_addon_log_level_comes_from_the_options(run_main):
    captured = run_main({**addon_defaults(), "log_level": "debug"}, "--addon")
    assert captured["args"].loglevel == "debug"


def test_addon_without_options_falls_back_to_the_config_flag(run_main, tmp_path):
    """`--addon` outside a Supervisor must not clobber anything."""
    fallback = tmp_path / "standalone.ini"
    fallback.write_text("[GENERAL]\nDEVICE_TYPE=ct002\n", encoding="utf-8")

    captured = run_main(None, "--addon", "--config", str(fallback))

    assert captured["args"].config == str(fallback)
    assert not (tmp_path / "config.ini").exists()


def test_without_the_flag_nothing_addon_related_runs(run_main, tmp_path):
    """A standalone install must ignore an options.json that happens to exist."""
    fallback = tmp_path / "standalone.ini"
    fallback.write_text("[GENERAL]\nDEVICE_TYPE=ct003\n", encoding="utf-8")

    captured = run_main({**addon_defaults()}, "--config", str(fallback))

    assert captured["args"].config == str(fallback)
    assert captured["device_types"] == ["ct003"]
    assert not (tmp_path / "config.ini").exists()


def test_paths_can_be_overridden_for_out_of_supervisor_runs(monkeypatch):
    """The env hooks the fixture above relies on are the shipped ones."""
    monkeypatch.setenv("ASTRAMETER_ADDON_OPTIONS", "/x/options.json")
    monkeypatch.setenv("ASTRAMETER_ADDON_CONFIG_DIR", "/x/config")
    monkeypatch.setenv("ASTRAMETER_ADDON_GENERATED_CONFIG", "/x/config.ini")

    import importlib

    reloaded = importlib.reload(startup)
    try:
        assert reloaded.OPTIONS_PATH == "/x/options.json"
        assert reloaded.ADDON_CONFIG_DIR == "/x/config"
        assert reloaded.GENERATED_CONFIG_PATH == "/x/config.ini"
    finally:
        monkeypatch.undo()
        importlib.reload(startup)
