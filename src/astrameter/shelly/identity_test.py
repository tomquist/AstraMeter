"""The emulated device's identity has to survive a restart.

A battery that paired with one identity will not follow it to another, so a
value that changes on every start is a device that keeps disappearing. Every
test here drives the derivation off a fixture tree rather than the real host,
so none of them touch the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrameter.config.settings import ShellySettings
from astrameter.shelly import identity as identity_module
from astrameter.shelly.identity import (
    ShellyIdentity,
    normalize_mac,
    resolve_identity,
    state_dir,
)

LEGACY_DEVICE_ID = "shellypro3em-ec4609c439c1"


@pytest.fixture(autouse=True)
def _no_host_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the host-derived step deterministic and obviously distinguishable."""
    monkeypatch.setattr(
        identity_module, "default_route_interface", lambda *a, **k: "eth0"
    )
    monkeypatch.setattr(
        identity_module, "interface_mac", lambda *a, **k: "aa:bb:cc:00:11:22"
    )


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Keep the last-resort state directory inside the test's own tree.

    ``ASTRAMETER_STATE_DIR`` is deliberately *unset* here: it outranks the
    config file's directory, so leaving it set would mask the branch most
    installs actually take.
    """
    monkeypatch.delenv("ASTRAMETER_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("B8:27:EB:36:42:42", "B827EB364242"),
        ("b8-27-eb-36-42-42", "B827EB364242"),
        ("b827eb364242", "B827EB364242"),
        (" B827EB364242 ", "B827EB364242"),
        ("", ""),
        ("nope", ""),
        ("B827EB3642", ""),
        ("B827EB36424Z", ""),
    ],
)
def test_normalize_mac(raw: str, expected: str) -> None:
    """A user may paste any of the usual spellings."""
    assert normalize_mac(raw) == expected


def test_an_explicit_mac_wins() -> None:
    result = resolve_identity(
        ShellySettings(mac="B8:27:EB:36:42:42"),
        None,
        addon=False,
        config_path=None,
    )
    assert result.mac == "B827EB364242"
    assert result.instance == "ShellyPro3EM-B827EB364242"
    assert result.hostname == "ShellyPro3EM-B827EB364242"
    assert result.shelly_id == "shellypro3em-b827eb364242"


def test_a_user_written_device_id_seeds_the_identity() -> None:
    result = resolve_identity(
        ShellySettings(),
        "shellypro3em-b827eb364242",
        addon=False,
        config_path=None,
    )
    assert result.mac == "B827EB364242"


def test_the_generated_default_does_not_seed_the_identity() -> None:
    """The critical case: the legacy default must not become everyone's MAC.

    It ends in 12 hex characters exactly as a MAC-derived id does, so a rule
    that trusted the shape alone would hand every install on earth the same
    emulated MAC — and identical mDNS names on the same network.
    """
    result = resolve_identity(ShellySettings(), None, addon=False, config_path=None)
    assert result.mac == "AABBCC001122"
    assert "ec4609c439c1" not in result.shelly_id


def test_the_derived_identity_is_persisted_and_reused(tmp_path: Path) -> None:
    """The file is what makes the identity survive a changed interface."""
    config_path = tmp_path / "config.ini"
    config_path.write_text("")
    first = resolve_identity(
        ShellySettings(), None, addon=False, config_path=str(config_path)
    )
    state = tmp_path / "astrameter-identity.json"
    assert state.exists()
    assert json.loads(state.read_text())["mac"] == first.mac

    # The interface is gone on the next start; the identity is not.
    import astrameter.shelly.identity as module

    original = module.interface_mac
    module.interface_mac = lambda *a, **k: None
    try:
        second = resolve_identity(
            ShellySettings(), None, addon=False, config_path=str(config_path)
        )
    finally:
        module.interface_mac = original
    assert second.mac == first.mac


def test_an_unreadable_state_file_is_ignored_not_fatal(tmp_path: Path) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text("")
    (tmp_path / "astrameter-identity.json").write_text("{not json")
    result = resolve_identity(
        ShellySettings(), None, addon=False, config_path=str(config_path)
    )
    assert result.mac == "AABBCC001122"


def test_a_state_file_from_a_future_version_is_ignored(tmp_path: Path) -> None:
    config_path = tmp_path / "config.ini"
    config_path.write_text("")
    (tmp_path / "astrameter-identity.json").write_text(
        json.dumps({"version": 99, "mac": "010203040506"})
    )
    result = resolve_identity(
        ShellySettings(), None, addon=False, config_path=str(config_path)
    )
    assert result.mac == "AABBCC001122"


def test_a_random_identity_is_locally_administered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no interface to read, the fallback must not claim real hardware."""
    monkeypatch.setattr(identity_module, "default_route_interface", lambda *a: None)
    result = resolve_identity(ShellySettings(), None, addon=False, config_path=None)
    first_octet = int(result.mac[:2], 16)
    assert first_octet & 0x02, "the local-administration bit must be set"
    assert not first_octet & 0x01, "a unicast address must not be multicast"


def test_an_unwritable_state_directory_is_survivable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Nothing writable still yields a working, stable identity.

    The derivation is deterministic per host, so the identity survives a
    restart anyway; only the guarantee against a changed interface is lost.
    """
    unwritable = tmp_path / "nope"
    unwritable.write_text("not a directory")
    monkeypatch.setenv("ASTRAMETER_STATE_DIR", str(unwritable / "sub"))
    result = resolve_identity(ShellySettings(), None, addon=False, config_path=None)
    assert result.mac == "AABBCC001122"


def test_the_two_dns_names_are_independently_overridable() -> None:
    """Pinning one name must not move the other, or the HTTP ``id``."""
    result = resolve_identity(
        ShellySettings(
            mac="B827EB364242",
            hostname="MyHost-1",
            mdns_instance="MyInstance-2",
        ),
        None,
        addon=False,
        config_path=None,
    )
    assert result.hostname == "MyHost-1"
    assert result.instance == "MyInstance-2"
    # Always derived from the MAC: it is what a consumer matches the device on
    # across mDNS and HTTP, so a DNS-name override must not touch it.
    assert result.shelly_id == "shellypro3em-b827eb364242"

    only_host = resolve_identity(
        ShellySettings(mac="B827EB364242", hostname="MyHost-1"),
        None,
        addon=False,
        config_path=None,
    )
    assert only_host.hostname == "MyHost-1"
    assert only_host.instance == "ShellyPro3EM-B827EB364242"


def test_the_addon_keeps_its_identity_in_the_data_volume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``/data`` is the add-on's persistent volume, so the identity belongs there."""
    monkeypatch.setattr(
        identity_module,
        "ADDON_STATE_PATH",
        tmp_path / "data" / "identity.json",
    )
    assert state_dir(None, addon=True) == tmp_path / "data"
    result = resolve_identity(ShellySettings(), None, addon=True, config_path=None)
    assert (tmp_path / "data" / "identity.json").exists()
    assert result.mac == "AABBCC001122"


def test_the_identity_carries_no_device_id() -> None:
    """Deliberately absent, and the reason is a migration hazard.

    ``device_id`` is the MQTT-Insights entity identity; deriving it here would
    rename every Home Assistant entity an existing install has and orphan its
    retained discovery topics.
    """
    assert not hasattr(ShellyIdentity("m", "i", "h", "s"), "device_id")
    assert {f for f in ShellyIdentity.__dataclass_fields__} == {
        "mac",
        "instance",
        "hostname",
        "shelly_id",
    }
