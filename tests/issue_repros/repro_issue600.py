#!/usr/bin/env python3
"""Reproduction for issue #600 — a once-saturated battery never gets back in.

The report: a battery that was briefly unable to produce (empty, or in an app
mode that ignores the CT) is marked saturated, and from then on it is only ever
handed a token target of a few tens of watts — 16-30 W in the reporters' logs —
which is *below what the device can physically execute*.  It therefore produces
nothing, which is exactly what keeps it marked saturated.  The house imports
kilowatts next to an idle, healthy battery, for hours.

The mechanism is a feedback loop between two parts of the balancer that are each
reasonable alone:

  * fair share scales every consumer's slice by ``1 - saturation``
    (``_compute_auto_target``), so a fully saturated battery is handed ~1% of the
    grid error, and
  * :class:`~astrameter.ct002.balancer.SaturationTracker` counts a poll as
    "failed to follow" whenever the command was at least
    ``MIN_TARGET_FOR_SATURATION`` (20 W) and the battery produced less than that.

Together they have a *stable fixed point at a command of ~MIN_TARGET_FOR_SATURATION*:
if the score decays, the share grows, the command crosses 20 W, and the rise term
(``saturation_alpha`` = 0.15/s) slams the score back up ~30x faster than the decay
(``saturation_decay_factor`` = 0.995/s) can bleed it off.  So the command parks
just around 20-30 W for any shortfall the rest of the pool cannot cover, however
large that shortfall is — see PART A.

For a battery whose minimum actionable command is higher than that the state is
permanent: a B2500's firmware clamps its own command to at least ``pmin``
(80 W with the common inverter, 40 W with the other class), and a stopped unit
only restarts once the grid import reaches that figure, so it is asked for 22 W
forever and answers 0 W forever.  Nothing rescues it:
``MIN_EFFICIENT_POWER = 0`` (the default) disables rotation and probing entirely,
and a restart only re-runs the same trap.

Run: uv run python tests/issue_repros/repro_issue600.py
"""

from __future__ import annotations

import time

from astrameter.ct002.balancer import BalancerConfig, ConsumerMode, LoadBalancer
from astrameter.simulator.b2500_steering import (
    MIN_OUTPUT_W,
    B2500SteeringController,
)
from astrameter.simulator.venus_integer_steering import (
    VenusIntegerSteeringController,
)

# The reporter's add-on settings (issue #600 and the follow-up on the Venus):
# active control, fair distribution, no efficiency limiting, no DC floor.
CFG = dict(
    fair_distribution=True,
    balance_gain=0.2,
    balance_deadband=25,
    max_correction_per_step=80,
    min_efficient_power=0,  # -> no rotation, no probe, no saturation swap
    efficiency_rotation_interval=900,
    min_dc_output=0,
    pace_base_step=0,
    grid_predict_trust=0.5,
)
SAT_KW = dict(
    saturation_alpha=0.15,
    saturation_decay_factor=0.995,
    saturation_grace_seconds=90.0,
    saturation_stall_timeout_seconds=60.0,
    saturation_enabled=True,
)
MAX_POWER = 800
DT = 1.0


class _Clock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


class _Battery:
    """A battery plant running each device's firmware-derived steering law.

    Both laws were read out of the shipped firmware images: the B2500's
    integrator and its ``pmin`` clamp from HMJ-2 V118, the Venus integrator
    from the VNSA-0 / VNSD-0 / VNSE3-0 images (and validated by executing the
    routine under emulation).

    ``healthy`` is the "mode"/SoC switch from the report: while it is ``False``
    the unit answers every command with 0 W (empty, or in an app mode that does
    not follow the CT), which is how the saturation score reaches 1.0.
    """

    def __init__(self, mac: str, device_type: str, *, healthy: bool = True) -> None:
        self.mac = mac
        self.device_type = device_type
        self.phase = "A"
        self.healthy = healthy
        self.power = 0.0
        self.ramp = 400.0
        self._dc = device_type.upper().startswith(("HMA", "HMJ", "HMK"))
        # B2500: the firmware's device-wide integrator, whose command is
        # clamped to [pmin, p] and so can never be formed below pmin.
        self._b2500 = B2500SteeringController(pmin=MIN_OUTPUT_W)
        self._venus = VenusIntegerSteeringController()

    def step(self, reading: float, dt: float) -> None:
        if not self.healthy:
            self.power = 0.0
            return
        grid = round(reading)
        if self._dc:
            # Mirrors BatterySimulator._steer_b2500_output.
            desired = float(
                self._b2500.step(
                    grid, max(0, round(self.power)), dt, max_power=MAX_POWER
                )
            )
        else:
            desired = float(
                self._venus.step(
                    grid,
                    float(MAX_POWER),
                    -float(MAX_POWER),
                    out=self.power,
                )
            )
        diff = max(-self.ramp * dt, min(self.ramp * dt, desired - self.power))
        self.power += diff


def _run(
    *,
    device_type: str,
    house: float,
    saturation_episode: bool = True,
    min_target: float = 20.0,
    stuck_s: float = 300.0,
    duration_s: float = 1800.0,
) -> tuple[float, float, float, float]:
    """Run the loop; return (power, saturation, command, grid) of battery 0."""
    clock = _Clock()
    lb = LoadBalancer(
        config=BalancerConfig(**CFG),
        saturation_min_target=min_target,
        clock=clock,
        **SAT_KW,
    )
    stuck = _Battery("aa0000000001", device_type, healthy=not saturation_episode)
    peer = _Battery("bb0000000002", device_type)
    bats = [stuck, peer]

    t = 0.0
    tick = 0
    reading = 0.0
    while t < duration_s:
        if t >= stuck_s:
            stuck.healthy = True  # the mode is fixed / the battery is charged
        reports = {
            b.mac: {
                "phase": b.phase,
                "power": round(b.power),
                "device_type": b.device_type,
            }
            for b in bats
        }
        grid = house - sum(b.power for b in bats)
        readings = {
            b.mac: lb.compute_target(
                consumer_id=b.mac,
                consumer_mode=ConsumerMode("auto"),
                all_reports=reports,
                grid_total=grid,
                inactive=frozenset(),
                manual=frozenset(),
                sample_id=(tick,),
            )[0]
            for b in bats
        }
        for b in bats:
            b.step(readings[b.mac], DT)
        reading = readings[stuck.mac]
        clock.advance(DT)
        t += DT
        tick += 1
    return stuck.power, lb.get_saturation(stuck.mac), reading, grid


def part_a() -> None:
    print("=" * 78)
    print("PART A — B2500 (HMJ-2): the command parks at ~MIN_TARGET_FOR_SATURATION")
    print("         two units on one phase, unit 0 unable to produce for the")
    print("         first 5 min, then perfectly healthy for 25 min")
    print("=" * 78)
    print(
        f"  {'house':>7} | {'unit 0 cmd':>10} | {'unit 0 sat':>10} | "
        f"{'unit 0 pow':>10} | {'grid':>7} |"
    )
    for house in (1000.0, 1400.0, 2000.0, 3000.0, 4000.0):
        power, sat, cmd, grid = _run(device_type="HMJ-2", house=house)
        print(
            f"  {house:>7.0f} | {cmd:>10.1f} | {sat:>10.3f} | {power:>10.0f} | "
            f"{grid:>7.0f} |"
        )
    power, sat, cmd, grid = _run(
        device_type="HMJ-2", house=3000.0, saturation_episode=False
    )
    print(
        f"\n  counterfactual — same run, unit 0 healthy from the start:\n"
        f"    cmd={cmd:.0f} W  sat={sat:.3f}  power={power:.0f} W  grid={grid:.0f} W\n"
        f"  The only difference is the 5-minute saturation episode. From 1400 W up\n"
        f"  — every load the peer alone cannot cover — the locked unit is asked for\n"
        f"  ~20-30 W however big the shortfall is: below the ~80 W a B2500 needs to\n"
        f"  energize its two DC channels, so it answers 0 W, which is what keeps it\n"
        f"  saturated. At 1000 W the peer covers the house on its own, the trickle\n"
        f"  falls under the detector's 20 W threshold, the score decays and the unit\n"
        f"  comes back — which is why the symptom looks intermittent in the field."
    )


def part_b() -> None:
    print()
    print("=" * 78)
    print("PART B — the pin tracks MIN_TARGET_FOR_SATURATION (house = 3000 W)")
    print("=" * 78)
    print(
        f"  {'min_target':>10} | {'unit 0 cmd':>10} | {'unit 0 sat':>10} | "
        f"{'unit 0 pow':>10} |"
    )
    for min_target in (20.0, 40.0, 80.0, 100.0, 160.0):
        power, sat, cmd, _ = _run(
            device_type="HMJ-2", house=3000.0, min_target=min_target
        )
        note = "  <-- above the device's ~80 W floor: recovers" if power > 0 else ""
        print(
            f"  {min_target:>10.0f} | {cmd:>10.1f} | {sat:>10.3f} | "
            f"{power:>10.0f} |{note}"
        )
    print(
        "  The command the loop settles on is set by MIN_TARGET_FOR_SATURATION,\n"
        "  not by the house: the score rises 30x faster than it decays, so it\n"
        "  parks wherever the command is just under the detector's threshold.\n"
        "  The trap is the gap between that threshold and what the battery can\n"
        "  actually execute."
    )


def part_c() -> None:
    print()
    print("=" * 78)
    print("PART C — Venus D (VNSD-0): pinned the same way, but it integrates out")
    print("=" * 78)
    for house in (2000.0, 3000.0):
        power, sat, cmd, _grid = _run(
            device_type="VNSD-0", house=house, duration_s=420.0
        )
        print(
            f"  house={house:>6.0f} -> after 2 min healthy: cmd={cmd:>7.1f} "
            f"sat={sat:.3f} power={power:>4.0f} W"
        )
    print(
        "  A Venus gets the same ~1% trickle while saturated, but its steering law\n"
        "  integrates its *own* setpoint (setpoint += gain*g - 5 per poll), so a\n"
        "  repeated 22 W command accumulates until the unit starts and the score\n"
        "  decays. A B2500 cannot: its firmware clamps its own command to at least\n"
        "  pmin, and once stopped it restarts only when the grid import itself\n"
        "  reaches pmin — so a 22 W request is never executed, however often it\n"
        "  is repeated, and the unit stays locked."
    )


def main() -> None:
    part_a()
    part_b()
    part_c()


if __name__ == "__main__":
    main()
