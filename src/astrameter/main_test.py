import argparse
from ipaddress import IPv4Network

from astrameter.config.config_loader import ClientFilter, new_config_parser
from astrameter.config.ini_config import IniAppConfig
from astrameter.config.settings import ConfiguredPowermeter
from astrameter.main import _resolve_device_config, read_ct_powermeter
from astrameter.powermeter import Powermeter


class _StubPowermeter(Powermeter):
    """Minimal powermeter stub for testing ``read_ct_powermeter``."""

    def __init__(
        self,
        values: list[float],
        wait_raises: BaseException | None = None,
        wait_calls: list[float] | None = None,
    ) -> None:
        self._values = values
        self._wait_raises = wait_raises
        self._wait_calls = wait_calls if wait_calls is not None else []

    async def get_powermeter_watts(self) -> list[float]:
        return list(self._values)

    async def get_powermeter_watts_raw(self) -> list[float]:
        return list(self._values)

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        self._wait_calls.append(timeout)
        if self._wait_raises is not None:
            raise self._wait_raises


_LOCAL = ClientFilter([IPv4Network("127.0.0.1/32")])


async def test_read_ct_powermeter_returns_none_when_no_match() -> None:
    pm = _StubPowermeter([10.0])
    powermeters = [ConfiguredPowermeter(pm, _LOCAL, True)]
    assert await read_ct_powermeter(("10.0.0.1", 0), powermeters) is None


async def test_read_ct_powermeter_pads_to_three_phases() -> None:
    pm = _StubPowermeter([42.0])
    powermeters = [ConfiguredPowermeter(pm, _LOCAL, False)]
    assert await read_ct_powermeter(("127.0.0.1", 0), powermeters) == [42.0, 0, 0]


async def test_read_ct_powermeter_skips_wait_when_disabled() -> None:
    pm = _StubPowermeter([1.0, 2.0, 3.0])
    powermeters = [ConfiguredPowermeter(pm, _LOCAL, False)]
    result = await read_ct_powermeter(("127.0.0.1", 0), powermeters)
    assert result == [1.0, 2.0, 3.0]
    assert pm._wait_calls == []


async def test_read_ct_powermeter_calls_wait_with_2s_when_enabled() -> None:
    pm = _StubPowermeter([1.0, 2.0, 3.0])
    powermeters = [ConfiguredPowermeter(pm, _LOCAL, True)]
    await read_ct_powermeter(("127.0.0.1", 0), powermeters)
    assert pm._wait_calls == [2]


async def test_stub_powermeter_raw_matches_watts() -> None:
    pm = _StubPowermeter([3.0, 4.0, 5.0])
    assert (
        await pm.get_powermeter_watts_raw()
        == await pm.get_powermeter_watts()
        == [
            3.0,
            4.0,
            5.0,
        ]
    )


async def test_read_ct_powermeter_swallows_timeout_and_serves_cached() -> None:
    """Issue #327: a slow push meter must not break CT002 responses."""
    pm = _StubPowermeter(
        [11.0, 22.0, 33.0],
        wait_raises=TimeoutError("simulated slow meter"),
    )
    powermeters = [ConfiguredPowermeter(pm, _LOCAL, True)]
    result = await read_ct_powermeter(("127.0.0.1", 0), powermeters)
    assert result == [11.0, 22.0, 33.0]


def _resolve(
    device_type: str, device_ids: list[str] | None = None
) -> tuple[list[str], list[str]]:
    types, ids, _, _, _ = _resolve_full(device_type, device_ids)
    return types, ids


def _resolve_full(
    device_type: str, device_ids: list[str] | None = None
) -> tuple[list[str], list[str], bool, frozenset[int], int | None]:
    cfg = new_config_parser()
    cfg.add_section("GENERAL")
    cfg.set("GENERAL", "DEVICE_TYPE", device_type)
    if device_ids:
        cfg.set("GENERAL", "DEVICE_IDS", ", ".join(device_ids))
    config = IniAppConfig(cfg)
    args = argparse.Namespace(
        device_types=None, skip_powermeter_test=None, device_ids=None
    )
    return _resolve_device_config(config, config.general(), args)


def test_resolve_device_config_shellypro3em_new_gets_shelly_id() -> None:
    """Issue #389: explicit shellypro3em_new must use a shellypro3em-* id."""
    device_types, device_ids = _resolve("shellypro3em_new")
    assert device_types == ["shellypro3em_new"]
    assert device_ids == ["shellypro3em-ec4609c439c1"]


def test_resolve_device_config_shellypro3em_old_gets_shelly_id() -> None:
    device_types, device_ids = _resolve("shellypro3em_old")
    assert device_types == ["shellypro3em_old"]
    assert device_ids == ["shellypro3em-ec4609c439c1"]


def test_resolve_device_config_shellypro3em_expands_to_old_and_new() -> None:
    device_types, device_ids = _resolve("shellypro3em")
    assert device_types == ["shellypro3em_old", "shellypro3em_new"]
    assert device_ids == ["shellypro3em-ec4609c439c1", "shellypro3em-ec4609c439c1"]


def test_resolve_device_config_marks_the_newer_port_as_the_tcp_owner() -> None:
    """One device owns the HTTP surface, and it is the newer-firmware port.

    The pair shares an identity, so the choice is free — and the older port is
    the one some runtimes cannot bind, so owning it there would tie the HTTP
    surface to the bind most likely to fail.
    """
    types, _, _, _, owner = _resolve_full("shellypro3em")
    assert types == ["shellypro3em_old", "shellypro3em_new"]
    assert owner == 1


def test_resolve_device_config_owns_tcp_for_a_single_shelly() -> None:
    types, _, _, _, owner = _resolve_full("shellypro3em_old")
    assert types == ["shellypro3em_old"]
    assert owner == 0


def test_resolve_device_config_has_no_tcp_owner_without_a_pro3em() -> None:
    for device_type in ("ct002", "shellyemg3", "shellyproem50"):
        _, _, _, _, owner = _resolve_full(device_type)
        assert owner is None, device_type


def test_resolve_device_config_reports_no_user_set_ids_by_default() -> None:
    """The generated default must not read as a user-chosen id.

    It ends in 12 hex characters exactly as a MAC-derived id does, so an
    identity step that trusted the shape alone would adopt the shared default
    on every install and give every user the same emulated MAC.
    """
    _, ids, _, user_set, _ = _resolve_full("shellypro3em")
    assert ids == ["shellypro3em-ec4609c439c1", "shellypro3em-ec4609c439c1"]
    assert user_set == frozenset()


def test_resolve_device_config_marks_a_configured_id_and_its_twin() -> None:
    _, ids, _, user_set, _ = _resolve_full(
        "shellypro3em", ["shellypro3em-aabbccddeeff"]
    )
    assert ids == ["shellypro3em-aabbccddeeff", "shellypro3em-aabbccddeeff"]
    # The expansion's appended twin inherits the flag, or the two halves would
    # disagree about their own identity.
    assert user_set == frozenset({0, 1})
