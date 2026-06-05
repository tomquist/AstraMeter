"""DC anti-sleep minimum-output floor in the LoadBalancer (issue #425).

Mirrors the C++ host test ``LoadBalancer.AutoFloor*``. The floor keeps a
DC-coupled battery's inverter awake under PV surplus by commanding a small
charge-direction output (``-min_dc_output``) instead of letting it sit at 0 W.
It only ever affects non-AC-chargeable batteries and respects weight-0 parking.
"""

from astrameter.ct002.balancer import (
    BalancerConfig,
    ConsumerMode,
    LoadBalancer,
)

DC = "HMA-2"  # B2500 family — DC-coupled (not AC-chargeable)
AC = "HMG-50"  # Venus family — AC-chargeable


def _make_balancer(*, min_dc_output: float = 0.0) -> LoadBalancer:
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            balance_gain=0.2,
            balance_deadband=15,
            max_correction_per_step=80,
            min_efficient_power=0,
            min_dc_output=min_dc_output,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=True,
    )


def _report(
    power: float,
    *,
    device_type: str = DC,
    weight: float = 1.0,
    min_dc_output: float | None = None,
    phase: str = "A",
) -> dict:
    return {
        "phase": phase,
        "power": power,
        "device_type": device_type,
        "weight": weight,
        "min_dc_output": min_dc_output,
    }


def _target(
    lb: LoadBalancer, cid: str, reports: dict, grid_total: float
) -> list[float]:
    return lb.compute_target(
        cid, ConsumerMode("auto"), reports, grid_total, frozenset(), frozenset()
    )


def test_lone_dc_near_balanced_grid_is_floored():
    """Grid hovering slightly negative: the DC battery is held at -floor."""
    lb = _make_balancer(min_dc_output=25)
    reports = {"a": _report(0.0)}
    out = _target(lb, "a", reports, -5.0)
    assert out[0] == -25.0


def test_lone_dc_large_surplus_still_floored():
    """A B2500 can't AC-charge (reported≈0), so even a big negative target
    must be replaced by the small -floor nudge, not left un-actionable."""
    lb = _make_balancer(min_dc_output=25)
    reports = {"a": _report(0.0)}
    out = _target(lb, "a", reports, -800.0)
    assert out[0] == -25.0


def test_disabled_when_zero():
    """min_dc_output=0 leaves the unfloored charge target untouched."""
    lb = _make_balancer(min_dc_output=0)
    reports = {"a": _report(0.0)}
    out = _target(lb, "a", reports, -5.0)
    assert out[0] == -5.0


def test_ac_battery_never_floored():
    """An AC-chargeable battery absorbs surplus by charging; never floored."""
    lb = _make_balancer(min_dc_output=25)
    reports = {"a": _report(0.0, device_type=AC)}
    out = _target(lb, "a", reports, -5.0)
    assert out[0] == -5.0


def test_zero_weight_dc_stays_parked():
    """A weight-0 DC battery is intentionally parked at 0 W — not floored."""
    lb = _make_balancer(min_dc_output=25)
    reports = {"a": _report(0.0, weight=0.0), "b": _report(0.0, weight=1.0)}
    a_out = _target(lb, "a", reports, -10.0)
    b_out = _target(lb, "b", reports, -10.0)
    assert a_out[0] == 0.0  # parked stays parked
    assert b_out[0] == -25.0  # the other DC battery is floored


def test_per_consumer_override_beats_global_and_none_falls_back():
    lb = _make_balancer(min_dc_output=25)
    reports = {
        "a": _report(0.0, min_dc_output=50.0),  # explicit override
        "b": _report(0.0, min_dc_output=None),  # fall back to global 25
    }
    a_out = _target(lb, "a", reports, -10.0)
    b_out = _target(lb, "b", reports, -10.0)
    assert a_out[0] == -50.0
    assert b_out[0] == -25.0


def test_already_charging_above_floor_is_left_alone():
    """If the battery is genuinely already charging ≥ floor, don't disturb it."""
    lb = _make_balancer(min_dc_output=25)
    reports = {"a": _report(-30.0)}
    out = _target(lb, "a", reports, -5.0)
    assert out[0] == -5.0  # normal fair-share reply, not bumped to -25


def test_no_floor_under_grid_import():
    """Under import the battery is discharging (awake); the floor never fires."""
    lb = _make_balancer(min_dc_output=25)
    reports = {"a": _report(0.0)}
    out = _target(lb, "a", reports, 200.0)
    assert out[0] == 200.0  # positive (discharge) target, untouched


def test_absent_consumer_is_not_floored():
    """A queried consumer that isn't in the report snapshot is never floored —
    we have no device_type/phase for it. Matches the C++ stack, which
    early-returns on a missing consumer."""
    reports = {"a": _report(0.0)}
    on = _target(_make_balancer(min_dc_output=25), "z", reports, -50.0)
    off = _target(_make_balancer(min_dc_output=0), "z", reports, -50.0)
    assert on == off


def test_mixed_fleet_dc_floored_ac_unperturbed():
    """In a mixed fleet under surplus, the DC battery is floored while the AC
    battery's reply is byte-identical with the floor enabled vs disabled."""
    reports = {
        "ac": _report(-100.0, device_type=AC),
        "dc": _report(0.0, device_type=DC),
    }
    lb_on = _make_balancer(min_dc_output=25)
    lb_off = _make_balancer(min_dc_output=0)

    dc_out = _target(lb_on, "dc", reports, -50.0)
    assert dc_out[0] == -25.0  # DC kept awake

    ac_on = _target(lb_on, "ac", reports, -50.0)
    ac_off = _target(lb_off, "ac", reports, -50.0)
    assert ac_on == ac_off  # the floor never perturbs the AC allocation
