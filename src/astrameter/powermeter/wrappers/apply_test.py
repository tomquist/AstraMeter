from __future__ import annotations

from astrameter.powermeter.base import Powermeter
from astrameter.powermeter.wrappers.apply import FilterOptions, apply_wrappers
from astrameter.powermeter.wrappers.base import PowermeterWrapper
from astrameter.powermeter.wrappers.hampel import HampelPowermeter
from astrameter.powermeter.wrappers.health import HealthTrackingPowermeter
from astrameter.powermeter.wrappers.pid import PidPowermeter
from astrameter.powermeter.wrappers.smoothing import (
    DeadbandPowermeter,
    SmoothedPowermeter,
)
from astrameter.powermeter.wrappers.throttling import ThrottledPowermeter
from astrameter.powermeter.wrappers.transform import TransformedPowermeter


class _StubPowermeter(Powermeter):
    async def get_powermeter_watts(self) -> list[float]:
        return [0.0, 0.0, 0.0]


def _chain(pm: Powermeter) -> list[type]:
    """Return wrapper classes from outermost to the base, then the base type."""
    out: list[type] = []
    while isinstance(pm, PowermeterWrapper):
        out.append(type(pm))
        pm = pm.wrapped_powermeter
    out.append(type(pm))
    return out


def test_no_knobs_returns_bare_powermeter() -> None:
    base = _StubPowermeter()
    assert apply_wrappers(base, FilterOptions()) is base


def test_only_health_wraps_just_health() -> None:
    base = _StubPowermeter()
    result = apply_wrappers(base, FilterOptions(health_name="SECTION"))
    assert _chain(result) == [HealthTrackingPowermeter, _StubPowermeter]
    assert isinstance(result, HealthTrackingPowermeter)
    assert result.name == "SECTION"


def test_only_pid_skips_the_rest() -> None:
    base = _StubPowermeter()
    result = apply_wrappers(base, FilterOptions(pid_kp=0.5))
    assert _chain(result) == [PidPowermeter, _StubPowermeter]


def test_full_chain_order_outermost_to_base() -> None:
    base = _StubPowermeter()
    opts = FilterOptions(
        offsets=[0.0],
        multipliers=[1.0],
        throttle_interval=1.0,
        hampel_window=5,
        smooth_alpha=0.2,
        deadband=10.0,
        pid_kp=0.5,
        health_name="SECTION",
    )
    result = apply_wrappers(base, opts)
    # Outermost -> base: health, pid, deadband, smoothing, hampel, throttle, transform
    assert _chain(result) == [
        HealthTrackingPowermeter,
        PidPowermeter,
        DeadbandPowermeter,
        SmoothedPowermeter,
        HampelPowermeter,
        ThrottledPowermeter,
        TransformedPowermeter,
        _StubPowermeter,
    ]


def test_smooth_alpha_is_clamped() -> None:
    base = _StubPowermeter()
    result = apply_wrappers(base, FilterOptions(smooth_alpha=5.0))
    smoothed = result
    assert isinstance(smoothed, SmoothedPowermeter)
    assert smoothed._alpha == 1.0

    result2 = apply_wrappers(_StubPowermeter(), FilterOptions(smooth_alpha=0.0001))
    # 0.0001 is not > 0? it is > 0, so applied and clamped up to 0.01
    assert isinstance(result2, SmoothedPowermeter)
    assert result2._alpha == 0.01
