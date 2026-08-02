"""Tests for the status registry, the wire serializer and the secret sentinel."""

import dataclasses
import inspect
from pathlib import Path

import pytest

from astrameter.config.config_loader import new_config_parser
from astrameter.config.ini_config import IniAppConfig
from astrameter.ct002 import CT002
from astrameter.powermeter.wrappers.health import HealthTrackingPowermeter
from astrameter.status import StatusRegistry, detect_config_mode
from astrameter.status.config_mode import materialize_config, target_path
from astrameter.status.registry import _as_wire_dict
from astrameter.status.secrets import SENTINEL, redact_sections, restore_sections
from astrameter.status.serialize import compact, iso, round_or_none


def _registry(**kwargs) -> StatusRegistry:
    kwargs.setdefault("config_path", "config.ini")
    kwargs.setdefault("log_level", "info")
    kwargs.setdefault("version", "9.9.9")
    kwargs.setdefault("git_commit", "")
    return StatusRegistry(**kwargs)


def _ct(device_id: str = "ct-1") -> CT002:
    device = CT002(device_id=device_id)
    device._update_consumer_report(
        "02b250000001", "A", -120, "HMK-2", source_ip="10.0.0.5"
    )
    device._last_grid_values = [12.0, -30.0, 5.0]
    device._last_grid_at = 1_770_000_000.0
    device._last_smooth_target = -13.0
    return device


# -- the atomicity guarantee ------------------------------------------


@pytest.mark.parametrize(
    "func",
    [
        CT002.status_snapshot,
        CT002.snapshot_consumer,
        HealthTrackingPowermeter.status_snapshot,
        StatusRegistry.snapshot,
    ],
)
def test_snapshot_methods_are_synchronous_and_await_free(func):
    """A snapshot must be atomic against every in-flight UDP handler.

    They all share one asyncio loop, so an await-free builder cannot be
    interleaved — and a future ``await`` would silently start producing
    snapshots torn across two polls, with no test failing. This is that test.
    """
    assert not inspect.iscoroutinefunction(func), f"{func.__qualname__} is async"
    source = inspect.getsource(func)
    assert "await " not in source, f"{func.__qualname__} contains an await"


def test_snapshot_hands_out_copies_not_live_containers():
    registry = _registry()
    device = _ct()
    registry.register_device("ct-1", "ct002", device)

    snapshot = registry.snapshot(ingress=False)
    consumers = snapshot["devices"][0]["consumers"]
    assert len(consumers) == 1

    # A later mutation must not retroactively change a taken snapshot.
    device._update_consumer_report(
        "02b250000002", "B", 90, "HMK-2", source_ip="10.0.0.6"
    )
    assert len(consumers) == 1
    assert len(registry.snapshot(ingress=False)["devices"][0]["consumers"]) == 2


# -- absence is not zero ----------------------------------------------


def test_compact_drops_none_but_keeps_zero():
    assert compact({"a": None, "b": 0, "c": False}) == {"b": 0, "c": False}


def test_round_and_iso_preserve_absence():
    assert round_or_none(None) is None
    assert round_or_none(1.23456) == 1.2
    assert iso(None) is None
    assert iso(0) is None
    assert iso(1_770_000_000.0).startswith("2026-")


def test_absent_fields_are_omitted_not_zeroed():
    """A field the backend cannot produce must be missing, so the UI can omit
    the card instead of rendering a convincing zero."""
    snapshot = _registry(git_commit="").snapshot(ingress=False)
    assert "git_commit" not in snapshot["service"]
    assert "devices" not in snapshot
    assert "powermeters" not in snapshot
    # The three guaranteed keys are always there.
    assert snapshot["schema_version"] == 1
    assert snapshot["generated_at"]
    assert snapshot["capabilities"]


def test_snapshot_carries_no_credentials():
    registry = _registry()
    registry.register_device("ct-1", "ct002", _ct())
    text = repr(registry.snapshot(ingress=False)).lower()
    for banned in ("password", "token", "secret", "accesstoken"):
        assert banned not in text


# -- capabilities gate the UI -----------------------------------------


def test_capabilities_are_fail_closed_without_ingress():
    registry = _registry(allow_write=True, direct_access=False)
    assert registry.capabilities(ingress=False)["controls"] is False
    assert registry.capabilities(ingress=True)["controls"] is True


def test_simple_mode_is_never_config_writable():
    """The add-on regenerates config.ini on every start, so offering to edit
    it there would silently lose the user's work."""
    registry = _registry(config_mode="ha_simple", allow_write=True)
    caps = registry.capabilities(ingress=True)
    assert caps["config_writable"] is False
    assert caps["ha_options"] is True


def test_etag_changes_with_state_and_over_time():
    registry = _registry()
    first = registry.etag()
    assert registry.etag() == first
    registry.bump()
    assert registry.etag() != first


def test_revision_folds_in_device_local_mutations():
    """Serving a battery bumps only the device's own ``_rev`` (ct002.py), not
    any registry field — so the registry has to fold each device's revision
    into its own, or a polling client would sit on a 304 through every
    battery update."""
    registry = _registry()
    device = _ct()
    registry.register_device("ct-1", "ct002", device)
    before = registry.revision()
    device._rev += 1  # what the real request path does once a response is sent
    assert registry.revision() == before + 1
    assert registry.etag() != f'W/"{before}-0"'


def test_register_and_unregister_device():
    registry = _registry()
    registry.register_device("ct-1", "ct002", _ct())
    assert "ct-1" in registry.devices
    registry.unregister_device("ct-1")
    assert registry.devices == {}


def test_reset_cycle_clears_per_run_state():
    registry = _registry()
    registry.register_device("ct-1", "ct002", _ct())
    registry.powermeters = [object()]
    registry.insights = object()
    registry.reset_cycle()
    assert registry.devices == {}
    assert registry.powermeters == []
    assert registry.insights is None


# -- config mode ------------------------------------------------------


def test_mode_comes_from_the_loaded_backend_never_from_the_filesystem():
    # Outside the add-on it makes no difference whether a file backs the
    # config — a Docker install is standalone either way.
    assert detect_config_mode(addon=False, config_path=None) == "standalone"
    assert detect_config_mode(addon=False, config_path="/app/x.ini") == "standalone"
    # In the add-on, a file behind the config means the user supplied one.
    assert detect_config_mode(addon=True, config_path=None) == "ha_simple"
    assert detect_config_mode(addon=True, config_path="/config/x.ini") == "ha_advanced"


def test_target_path_rejects_traversal(tmp_path):
    assert target_path("astrameter.ini", str(tmp_path)).endswith("/astrameter.ini")
    assert target_path("../../etc/passwd", str(tmp_path)) == str(tmp_path / "passwd")
    with pytest.raises(ValueError):
        target_path("  ", str(tmp_path))
    with pytest.raises(ValueError):
        target_path(".hidden", str(tmp_path))


def test_materialize_copies_a_config_that_already_has_a_file(tmp_path):
    """Verbatim, comments and all — there is nothing to gain by re-rendering."""
    source = tmp_path / "running.ini"
    source.write_text("[GENERAL]\n# keep me\nDEVICE_TYPE = ct002\n")
    written = materialize_config(
        IniAppConfig(new_config_parser(), str(source)),
        "astrameter.ini",
        str(tmp_path / "config"),
    )
    body = Path(written).read_text(encoding="utf-8")
    assert "DEVICE_TYPE = ct002" in body
    assert "# keep me" in body
    assert "Written by AstraMeter" in body


def test_materialize_renders_a_config_that_has_no_file(tmp_path):
    """The add-on options case: the settings are the only source there is."""
    parser = new_config_parser()
    parser.read_string(
        "[GENERAL]\nDEVICE_TYPE = ct002\n\n[CT002]\nBALANCE_GAIN = 0.4\n"
    )
    written = materialize_config(
        IniAppConfig(parser), "astrameter.ini", str(tmp_path / "config")
    )
    body = Path(written).read_text(encoding="utf-8")
    # Reading it back gives the settings that were running.
    restored = new_config_parser()
    restored.read_string(body)
    assert IniAppConfig(restored).ct("ct002").balance_gain == 0.4


def test_materialize_never_clobbers_an_existing_file(tmp_path):
    target_dir = tmp_path / "config"
    target_dir.mkdir()
    existing = target_dir / "astrameter.ini"
    existing.write_text("# precious\n")
    source = tmp_path / "running.ini"
    source.write_text("[GENERAL]\n")
    materialize_config(
        IniAppConfig(new_config_parser(), str(source)),
        "astrameter.ini",
        str(target_dir),
    )
    assert existing.read_text() == "# precious\n"


# -- the secret sentinel ----------------------------------------------


def test_secrets_are_redacted_on_the_way_out():
    sections = {
        "MQTT": {"PASSWORD": "hunter2", "BROKER": "10.0.0.2", "USERNAME": "bob"},
        "MARSTEK": {"MAILBOX": "a@b.c", "TIMEZONE": "Europe/Berlin"},
    }
    out = redact_sections(sections)
    assert out["MQTT"]["PASSWORD"] == SENTINEL
    assert out["MARSTEK"]["MAILBOX"] == SENTINEL
    assert out["MQTT"]["BROKER"] == "10.0.0.2"
    assert out["MARSTEK"]["TIMEZONE"] == "Europe/Berlin"
    assert "hunter2" not in repr(out)


def test_uri_userinfo_is_redacted():
    out = redact_sections({"MQTT_INSIGHTS": {"URI": "mqtt://bob:hunter2@broker:1883"}})
    assert "hunter2" not in out["MQTT_INSIGHTS"]["URI"]
    assert "broker:1883" in out["MQTT_INSIGHTS"]["URI"]


def test_sentinel_round_trip_keeps_the_stored_secret():
    """A client that never saw the real password cannot send it back, so an
    unrelated edit must not blank it."""
    current = {"MQTT": {"PASSWORD": "hunter2", "BROKER": "10.0.0.2"}}
    edited = {"MQTT": {"PASSWORD": SENTINEL, "BROKER": "10.0.0.9"}}
    restored = restore_sections(edited, current)
    assert restored["MQTT"]["PASSWORD"] == "hunter2"
    assert restored["MQTT"]["BROKER"] == "10.0.0.9"


def test_a_real_new_secret_is_written_through():
    current = {"MQTT": {"PASSWORD": "old"}}
    restored = restore_sections({"MQTT": {"PASSWORD": "brand-new"}}, current)
    assert restored["MQTT"]["PASSWORD"] == "brand-new"


def test_empty_secret_is_not_replaced_by_a_sentinel():
    """An unset password must render as empty, not as eight bullets the user
    would then have to clear."""
    out = redact_sections({"MQTT": {"PASSWORD": ""}})
    assert out["MQTT"]["PASSWORD"] == ""


# -- serializer helpers -----------------------------------------------


def test_as_wire_dict_expands_nested_dataclasses_and_drops_none():
    @dataclasses.dataclass
    class Inner:
        a: int = 1
        b: str | None = None

    @dataclasses.dataclass
    class Outer:
        inner: Inner = dataclasses.field(default_factory=Inner)
        items: tuple = ()
        missing: str | None = None

    out = _as_wire_dict(Outer(items=(Inner(a=2),)))
    assert out == {"inner": {"a": 1}, "items": [{"a": 2}]}
