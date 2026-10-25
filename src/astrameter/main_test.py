import argparse
from dataclasses import replace
from ipaddress import IPv4Network
from types import SimpleNamespace
from typing import cast

from astrameter.config.config_loader import (
    ClientFilter,
    apply_signal_wrappers,
    new_config_parser,
)
from astrameter.config.ini_config import IniAppConfig
from astrameter.config.settings import ConfiguredPowermeter, SignalSettings
from astrameter.ct002.ct002 import CT002, PhaseBucket
from astrameter.main import (
    _ct_measurement,
    _passive_grid_phases,
    _resolve_device_config,
    read_ct_powermeter,
)
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

    async def get_passive_watts(self) -> list[float]:
        return list(self._values)

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        self._wait_calls.append(timeout)
        if self._wait_raises is not None:
            raise self._wait_raises


def test_cloud_measurement_maps_buckets_like_a_real_ct() -> None:
    """A real CT fills the cloud's ``c*`` fields from the buckets it sends as
    ``*_dchrg_power`` over UDP and ``d*`` from ``*_chrg_power``."""
    buckets = {
        name: PhaseBucket(chrg_power=-10 * (i + 1), dchrg_power=10 * (i + 1))
        for i, name in enumerate(("x", "A", "B", "C", "ABC"))
    }
    device = SimpleNamespace(
        reporting_phase_buckets=lambda: buckets,
        reporting_consumer_count=lambda: 2,
        wifi_rssi=-60,
    )
    m = _ct_measurement(cast(CT002, device), [100.4, -50.0, 0.0], True)
    assert (m.ap, m.bp, m.cp, m.dp) == (100, -50, 0, 50)
    assert (m.cz, m.ca, m.cb, m.cc, m.cd) == (10, 20, 30, 40, 50)
    assert (m.dz, m.da, m.db, m.dc, m.dd) == (-10, -20, -30, -40, -50)
    assert (m.rssi, m.slv, m.udp, m.mqtt) == (-60, 2, 1, 1)


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


async def test_stub_powermeter_passive_matches_watts() -> None:
    pm = _StubPowermeter([3.0, 4.0, 5.0])
    assert (
        await pm.get_passive_watts()
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


class _PolledMeter(Powermeter):
    """A pull source that counts how often it is actually read."""

    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.reads = 0

    async def get_powermeter_watts(self) -> list[float]:
        self.reads += 1
        return list(self.values)


_EVERYONE = ClientFilter([IPv4Network("0.0.0.0/0")])


def _configured(source: Powermeter) -> list[ConfiguredPowermeter]:
    """*source* behind the wrappers the config loader builds, smoothing on."""
    signal = replace(SignalSettings(), smooth_alpha=0.5)
    return [
        ConfiguredPowermeter(
            apply_signal_wrappers(source, "SCRIPT_1", signal), _EVERYONE, False
        )
    ]


async def test_grid_phases_reuse_the_control_loops_read() -> None:
    """The Marstek responder and cloud reporting only report the reading, so
    while a battery polls the meter they must not read it again."""
    source = _PolledMeter([100.0, 200.0, 300.0])
    powermeters = _configured(source)

    await read_ct_powermeter(("10.0.0.5", 0), powermeters)
    source.values = [0.0, 0.0, 0.0]

    assert await _passive_grid_phases(powermeters) == [100.0, 200.0, 300.0]
    assert source.reads == 1


async def test_grid_phases_read_an_idle_meter_themselves() -> None:
    source = _PolledMeter([100.0, 200.0, 300.0])
    powermeters = _configured(source)

    assert await _passive_grid_phases(powermeters) == [100.0, 200.0, 300.0]
    assert source.reads == 1


def _resolve(device_type: str) -> tuple[list[str], list[str]]:
    cfg = new_config_parser()
    cfg.add_section("GENERAL")
    cfg.set("GENERAL", "DEVICE_TYPE", device_type)
    config = IniAppConfig(cfg)
    args = argparse.Namespace(
        device_types=None, skip_powermeter_test=None, device_ids=None
    )
    device_types, device_ids, _ = _resolve_device_config(config, config.general(), args)
    return device_types, device_ids


def test_resolve_device_config_shellypro3em_new_gets_shelly_id() -> None:
    """Issue #389: explicit shellypro3em_new must use a shellypro3em-* id."""
    device_types, device_ids = _resolve("shellypro3em_new")
    assert device_types == ["shellypro3em_new"]
    assert device_ids == ["shellypro3em-ec4609c439c1"]


def test_resolve_device_config_shellypro3em_old_gets_shelly_id() -> None:
    device_types, device_ids = _resolve("shellypro3em_old")
    assert device_types == ["shellypro3em_old"]
    assert device_ids == ["shellypro3em-ec4609c439c1"]


def test_resolve_device_config_shellypro3em_is_one_device() -> None:
    """Issue #734: both ports are one emulator, so one id, one dashboard card."""
    device_types, device_ids = _resolve("shellypro3em")
    assert device_types == ["shellypro3em"]
    assert device_ids == ["shellypro3em-ec4609c439c1"]
