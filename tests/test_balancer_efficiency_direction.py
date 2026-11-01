"""Separate efficiency thresholds for charging and discharging (issue #642).

``MIN_EFFICIENT_CHARGE_POWER`` / ``MIN_EFFICIENT_DISCHARGE_POWER`` replace
``MIN_EFFICIENT_POWER`` for one direction of the pool's demand: unset falls back
to it, ``0`` turns efficiency rotation off that way only.  A Marstek charges up
to 2500 W but discharges only 800 W, so the reporter wants discharging spread
across two batteries from ~750 W but charging kept on one up to 1200 W.

These drive :meth:`LoadBalancer.compute_target` with a scripted demand, like
``tests/test_balancer_slot_hysteresis.py``.  The direction is the pool's
demand: battery output plus grid, positive to discharge.
"""

from __future__ import annotations

import pytest

from astrameter.ct002.balancer import (
    EFFICIENCY_DIRECTION_DEADBAND_W,
    BalancerConfig,
    ConsumerMode,
    ConsumerReport,
    LoadBalancer,
)

MACS = ["aabb00000001", "aabb00000002"]


class _FakeClock:
    def __init__(self) -> None:
        self._t = 1_000_000.0

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _make_balancer(clock: _FakeClock, **cfg: float | None) -> LoadBalancer:
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            # Keep rotation and saturation swaps out of the picture so the
            # tests isolate the slot count.
            efficiency_rotation_interval=1_000_000,
            efficiency_saturation_threshold=0.0,
            **cfg,  # type: ignore[arg-type]
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=False,
        clock=clock,
    )


class _Run:
    """Feeds the balancer a demand, the pool reporting *carried* W of it."""

    def __init__(self, **cfg: float | None) -> None:
        self.clock = _FakeClock()
        self.lb = _make_balancer(self.clock, **cfg)
        self.tick = 0

    def __call__(self, demand: float, ticks: int = 1, carried: float = 0.0) -> None:
        for _ in range(ticks):
            reports = {
                MACS[0]: ConsumerReport(phase="A", power=round(carried)),
                MACS[1]: ConsumerReport(phase="A", power=0),
            }
            for mac in MACS:
                self.lb.compute_target(
                    consumer_id=mac,
                    consumer_mode=ConsumerMode("auto"),
                    all_reports=reports,
                    grid_total=demand - carried,
                    inactive=frozenset(),
                    manual=frozenset(),
                    sample_id=(self.tick,),
                )
            self.tick += 1
            self.clock.advance(1.0)

    @property
    def active(self) -> int:
        return len(MACS) - len(self.lb._deprioritized)


# --- configuration ------------------------------------------------------------


def test_unset_falls_back_to_min_efficient_power() -> None:
    cfg = BalancerConfig(min_efficient_power=375)
    assert cfg.efficient_power(1) == 375
    assert cfg.efficient_power(-1) == 375
    assert cfg.efficiency_enabled


def test_each_direction_takes_its_own_value() -> None:
    cfg = BalancerConfig(
        min_efficient_power=375,
        min_efficient_charge_power=1200,
        min_efficient_discharge_power=300,
    )
    assert cfg.efficient_power(-1) == 1200
    assert cfg.efficient_power(1) == 300


@pytest.mark.parametrize(
    ("global_w", "charge", "discharge", "enabled"),
    [
        (0, None, None, False),
        (375, None, None, True),
        (0, 1200, None, True),
        (0, None, 375, True),
        (375, 0, 0, False),
        (375, 0, None, True),
    ],
)
def test_enabled_means_enabled_in_either_direction(
    global_w: float, charge: float | None, discharge: float | None, enabled: bool
) -> None:
    cfg = BalancerConfig(
        min_efficient_power=global_w,
        min_efficient_charge_power=charge,
        min_efficient_discharge_power=discharge,
    )
    assert cfg.efficiency_enabled is enabled
    clock = _FakeClock()
    lb = _make_balancer(
        clock,
        min_efficient_power=global_w,
        min_efficient_charge_power=charge,
        min_efficient_discharge_power=discharge,
    )
    # Gates the Force Rotation button and the window-weight entity, on the
    # dashboard and in Home Assistant discovery.
    assert lb.efficiency_rotation_enabled is enabled


def test_negative_overrides_are_clamped_to_zero() -> None:
    cfg = BalancerConfig(
        min_efficient_charge_power=-5, min_efficient_discharge_power=-1
    )
    assert cfg.min_efficient_charge_power == 0
    assert cfg.min_efficient_discharge_power == 0


# --- the #642 case ------------------------------------------------------------


def test_charging_concentrates_up_to_the_charge_threshold() -> None:
    """Discharge 375 W, charge 1200 W, two batteries (the reporter's setup)."""
    run = _Run(
        min_efficient_power=375,
        min_efficient_charge_power=1200,
        efficiency_demand_alpha=1.0,
    )
    # A 1000 W surplus: 500 W each is below 1200, so one battery charges.
    run(-1000, ticks=3)
    assert run.active == 1
    # A small surplus is still absorbed by that one battery.
    run(-80, ticks=3)
    assert run.active == 1
    (active,) = [mac for mac in MACS if mac not in run.lb._deprioritized]
    intent = run.lb.get_last_intent(active)
    assert intent is not None and intent < 0, "the active battery charges"
    # Past 2 x 1200 x 1.2 both charge.
    run(-2900, ticks=3)
    assert run.active == 2
    # 1000 W of demand: 500 W each is above 375, so both discharge, where the
    # charge threshold would have kept one.
    run(1000, ticks=3)
    assert run.active == 2
    # 600 W of demand: 300 W each is below 375, so one discharges.
    run(600, ticks=3)
    assert run.active == 1


def test_unset_overrides_match_min_efficient_power() -> None:
    """With neither override set, the active set is exactly today's."""
    demands = [-1000, -80, -2900, 1000, 600, 30, -30, 900, -900, 0]
    legacy = _Run(min_efficient_power=375)
    fallback = _Run(
        min_efficient_power=375,
        min_efficient_charge_power=None,
        min_efficient_discharge_power=None,
    )
    explicit = _Run(
        min_efficient_power=375,
        min_efficient_charge_power=375,
        min_efficient_discharge_power=375,
    )
    for demand in demands:
        for run in (legacy, fallback, explicit):
            run(demand, ticks=4)
        assert legacy.lb._deprioritized == fallback.lb._deprioritized
        assert legacy.lb._deprioritized == explicit.lb._deprioritized


def test_zero_turns_rotation_off_in_that_direction_only() -> None:
    """Global 0 with charge 1200: rotation only while charging."""
    run = _Run(
        min_efficient_power=0,
        min_efficient_charge_power=1200,
        efficiency_demand_alpha=1.0,
    )
    run(-600, ticks=3)
    assert run.active == 1
    run(300, ticks=3)
    assert run.active == 2, "discharging has no efficiency threshold"
    run(-600, ticks=3)
    assert run.active == 1

    run = _Run(
        min_efficient_power=375,
        min_efficient_discharge_power=0,
        efficiency_demand_alpha=1.0,
    )
    run(200, ticks=3)
    assert run.active == 2
    run(-200, ticks=3)
    assert run.active == 1


# --- the zero crossing --------------------------------------------------------


@pytest.mark.parametrize("alpha", [0.1, 1.0])
def test_noise_around_zero_does_not_toggle_batteries(alpha: float) -> None:
    """A demand hovering at 0 W must not switch thresholds on meter noise.

    Global 0 with charge 1200 is the sharpest case: the discharge side keeps
    both batteries active and the charge side only one, so every switch of
    threshold would move a battery.  Noise within the direction deadband
    never switches, even with the demand smoothing off.
    """
    run = _Run(
        min_efficient_power=0,
        min_efficient_charge_power=1200,
        efficiency_demand_alpha=alpha,
    )
    run(300, ticks=60)
    assert run.active == 2
    noise = EFFICIENCY_DIRECTION_DEADBAND_W - 10
    for i in range(200):
        run(noise if i % 2 else -noise)
        assert run.active == 2, f"battery toggled on noise at tick {i}"
    # From the charge side, noise keeps the one charging battery.
    run(-600, ticks=60)
    assert run.active == 1
    for i in range(200):
        run(noise if i % 2 else -noise)
        assert run.active == 1, f"battery toggled on noise at tick {i}"
    # A sustained flip past the deadband does switch.
    run(300, ticks=60)
    assert run.active == 2


def test_direction_switch_keeps_the_slot_hysteresis() -> None:
    """After a flip, the new direction's threshold still carries the 20 %
    growth margin, so a demand at its boundary does not flap either."""
    run = _Run(
        min_efficient_power=375,
        min_efficient_charge_power=1200,
        efficiency_demand_alpha=1.0,
    )
    run(-1000, ticks=3)
    assert run.active == 1
    # Discharging at 2 x 375 = 750 W: limiting entered from the charge side,
    # so growing back to two needs 375 x 2 x 1.2 = 900 W.
    for _ in range(20):
        run(760)
        assert run.active == 1
        run(740)
        assert run.active == 1
    run(950, ticks=2)
    assert run.active == 2


def test_direction_needs_a_sustained_crossing_with_smoothing() -> None:
    """With the default smoothing a short spike the other way is not a flip."""
    run = _Run(min_efficient_power=0, min_efficient_charge_power=1200)
    run(300, ticks=80)
    assert run.lb._demand_dir == 1
    run(-400, ticks=2)
    assert run.lb._demand_dir == 1
    assert run.active == 2
    run(-400, ticks=40)
    assert run.lb._demand_dir == -1
    assert run.active == 1
