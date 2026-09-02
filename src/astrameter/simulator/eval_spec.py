"""What an evaluation scenario is made of: battery specs, timed events and the
world handle those events mutate, plus the per-poll sample a run records."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field

from astrameter.ct002.balancer import device_capabilities

from .battery import BatterySimulator
from .load_model import Load, LoadModel


@dataclass(frozen=True)
class BatterySpec:
    """Static description of one simulated battery in a scenario."""

    device_type: str = "HMG-50"
    phase: str = "A"
    max_charge_power: int = 2500
    max_discharge_power: int = 2500
    capacity_wh: float = 5120.0
    initial_soc: float = 0.6
    # How fast measured output follows the commanded setpoint (W/s). Unlike the
    # steering laws this is not firmware-derived: the Venus control MCU applies
    # no rate limit, the slewing lives in the inverter sub-processor whose
    # images carry nothing to read it from. It shapes transients only.
    ramp_rate: float = 400.0
    poll_interval: float = 1.0
    startup_delay: float = 2.0
    min_power_threshold: float = 5.0
    max_dc_input: int = 0
    # Net output (W) already flowing at t=0 (positive = discharge, negative =
    # charge). Non-zero models a system already in motion, which the firmware
    # input deadband treats differently from rest.
    initial_power: float = 0.0

    @property
    def ac_chargeable(self) -> bool:
        return device_capabilities(self.device_type).has_ac_input


@dataclass(frozen=True)
class Event:
    """A scheduled world mutation.

    A non-empty *label* marks a step disturbance whose settling/overshoot is
    measured; unlabeled events (e.g. the per-minute solar curve) only mutate
    the world.
    """

    at: float
    apply: Callable[[EvalWorld], None]
    label: str = ""


@dataclass
class Scenario:
    name: str
    description: str
    batteries: list[BatterySpec]
    duration_s: float
    build_events: Callable[[random.Random], list[Event]]
    base_load: list[float] = field(default_factory=lambda: [300.0, 0.0, 0.0])
    base_noise: float = 10.0
    loads: list[Load] = field(default_factory=list)
    ct_kwargs: dict[str, float] = field(default_factory=dict)
    # The controller acts on a meter reading refreshed at this cadence (a
    # typical ~1 s powermeter poll; some HTTP/MQTT meters only emit every
    # ~10 s) while the metrics see the true instantaneous grid.
    meter_interval_s: float = 1.0
    # Transport/measurement delay on top of the refresh: the controller reads
    # the grid as it was this many seconds ago. Acting on a stale reading is a
    # classic driver of sustained oscillation, so this is what reproduces a
    # loop that hunts instead of settling. No real meter is delay-free; set 0.0
    # only to isolate controller behaviour from meter latency.
    meter_latency_s: float = 0.5


@dataclass
class EvalWorld:
    """Mutable world handle passed to scenario events."""

    load_model: LoadModel
    batteries: list[BatterySimulator]
    # Solar is curve x factor so labeled transients (cloud dips) compose with
    # the unlabeled day curve instead of being overwritten by its next tick.
    solar_curve_w: float = 0.0
    solar_factor: float = 1.0

    def set_load(self, name: str, active: bool) -> None:
        for ld in self.load_model.loads:
            if ld.name == name:
                ld.active = active
                return
        raise KeyError(f"no load named {name!r}")

    def set_house_load(self, watts: float) -> None:
        """Replay one recorded whole-house reading as the phase-A base load."""
        self.load_model.base_load[0] = watts

    def set_net_load(self, load_w: float, pv_w: float) -> None:
        """House load and PV from one recorded net-load sample, so the two stay
        correlated (same site, same instant)."""
        self.set_house_load(load_w)
        self.set_solar_curve(pv_w)

    def set_solar_curve(self, watts: float) -> None:
        self.solar_curve_w = watts
        self._apply_solar()

    def set_solar_factor(self, factor: float) -> None:
        self.solar_factor = factor
        self._apply_solar()

    def _apply_solar(self) -> None:
        self.load_model.set_solar(self.solar_curve_w * self.solar_factor)

    def set_dc_input(self, battery_index: int, watts: float) -> None:
        self.batteries[battery_index].dc_input_power = watts


@dataclass(frozen=True)
class _Sample:
    """What the harness records at every battery poll."""

    t: float  # seconds since scenario start
    # Instantaneous *true* grid total (W): the physical ground truth, not the
    # delayed value the controller reads from the meter cache.
    grid: float
    # Raw whole-house consumption (load + noise - solar) straight from the load
    # model, so a plotted consumption can never carry control-loop oscillation.
    consumption: float
    powers: tuple[float, ...]
    socs: tuple[float, ...]
    # PV wired straight into a battery's DC side (B2500, hybrid Venus): energy
    # that never crosses the house meter, so it is not in ``consumption``.
    dc_input: float = 0.0
