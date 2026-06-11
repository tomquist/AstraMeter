"""Backend-agnostic powermeter filter pipeline.

`apply_wrappers` is the single source of truth for the order and conditional
application of the powermeter processing wrappers. Both the CLI config loader
(`config_loader.read_all_powermeter_configs`) and the native Home Assistant
integration build a `FilterOptions` and call this, so the filter chain can't
drift between them.

The six processing wrappers are applied **conditionally** (only when their knob
is set / non-zero), exactly as the config loader did historically.
`HealthTrackingPowermeter` is applied **unconditionally** as the outermost
wrapper whenever ``health_name`` is set.
"""

from __future__ import annotations

from dataclasses import dataclass

from astrameter.powermeter.base import Powermeter

from .hampel import HampelPowermeter
from .health import HealthTrackingPowermeter
from .pid import PidPowermeter
from .smoothing import DeadbandPowermeter, SmoothedPowermeter
from .throttling import ThrottledPowermeter
from .transform import TransformedPowermeter

__all__ = ["FilterOptions", "apply_wrappers"]


@dataclass
class FilterOptions:
    """Resolved filter-pipeline knobs with skip-when-unset semantics.

    A wrapper is applied only when its knob is "set":
      * transform   — both ``offsets`` and ``multipliers`` are not ``None``
      * throttle    — ``throttle_interval > 0``
      * hampel      — ``hampel_window > 0``
      * smoothing   — ``smooth_alpha > 0`` (clamped to ``[0.01, 1.0]``)
      * deadband    — ``deadband > 0``
      * pid         — ``pid_kp > 0``
      * health      — ``health_name is not None`` (outermost, always-on hook)
    """

    offsets: list[float] | None = None
    multipliers: list[float] | None = None
    throttle_interval: float = 0.0
    hampel_window: int = 0
    hampel_n_sigma: float = 3.0
    hampel_min_threshold: float = 0.0
    smooth_alpha: float = 0.0
    max_smooth_step: float = 0.0
    deadband: float = 0.0
    pid_kp: float = 0.0
    pid_ki: float = 0.0
    pid_kd: float = 0.0
    pid_output_max: float = 800.0
    pid_mode: str = "bias"
    health_name: str | None = None


def apply_wrappers(powermeter: Powermeter, opts: FilterOptions) -> Powermeter:
    """Apply the canonical filter chain to ``powermeter`` per ``opts``.

    Order (innermost → outermost): transform, throttle, hampel, smoothing,
    deadband, pid, health.
    """
    if opts.offsets is not None and opts.multipliers is not None:
        powermeter = TransformedPowermeter(powermeter, opts.offsets, opts.multipliers)

    if opts.throttle_interval > 0:
        powermeter = ThrottledPowermeter(powermeter, opts.throttle_interval)

    if opts.hampel_window > 0:
        powermeter = HampelPowermeter(
            powermeter,
            window=opts.hampel_window,
            n_sigma=opts.hampel_n_sigma,
            min_threshold=opts.hampel_min_threshold,
        )

    if opts.smooth_alpha > 0:
        alpha = max(0.01, min(1.0, opts.smooth_alpha))
        powermeter = SmoothedPowermeter(
            powermeter,
            alpha=alpha,
            max_step=opts.max_smooth_step,
        )

    if opts.deadband > 0:
        powermeter = DeadbandPowermeter(powermeter, deadband=opts.deadband)

    if opts.pid_kp > 0:
        powermeter = PidPowermeter(
            powermeter,
            kp=opts.pid_kp,
            ki=opts.pid_ki,
            kd=opts.pid_kd,
            output_max=opts.pid_output_max,
            mode=opts.pid_mode,
        )

    if opts.health_name is not None:
        powermeter = HealthTrackingPowermeter(powermeter, name=opts.health_name)

    return powermeter
