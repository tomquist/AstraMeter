"""Per-battery efficiency-window weight in the LoadBalancer.

The efficiency rotation deprioritizes some batteries at low demand (so the
active ones stay above ``min_efficient_power``) and rotates which one is active
for fair wear. ``efficiency_window_weight`` (a report-dict field, ``[0, 1]``,
neutral ``1.0``) biases that rotation: ``0.0`` parks a battery while limiting,
``1.0`` is full participation, and the active head holds its slot for
``efficiency_rotation_interval`` scaled by its weight.

These poke the balancer internals (``_priority`` / ``_deprioritized`` /
``_last_rotation``) directly, matching the existing balancer unit tests. The
C++ mirror is covered by the differential parity suite.
"""

import time

from astrameter.ct002.balancer import (
    BalancerConfig,
    LoadBalancer,
)


class _FakeClock:
    def __init__(self) -> None:
        self._t = time.time()

    def __call__(self) -> float:
        return self._t

    def advance(self, dt: float) -> None:
        self._t += dt


def _make_balancer(clock, *, rotation_interval: float = 900.0) -> LoadBalancer:
    return LoadBalancer(
        config=BalancerConfig(
            fair_distribution=True,
            min_efficient_power=150,
            efficiency_rotation_interval=rotation_interval,
            # Follow demand instantly so the active-set decision is deterministic
            # (no EMA smoothing) for these unit assertions.
            efficiency_demand_alpha=1.0,
        ),
        saturation_alpha=0.15,
        saturation_min_target=20,
        saturation_decay_factor=0.995,
        saturation_grace_seconds=90.0,
        saturation_stall_timeout_seconds=60.0,
        saturation_enabled=False,
        clock=clock,
    )


def _report(power: float, eff_weight: float = 1.0) -> dict:
    return {
        "phase": "A",
        "power": power,
        "device_type": "HMG-50",
        "efficiency_window_weight": eff_weight,
    }


def test_zero_weight_battery_stays_deprioritized_while_limiting():
    """A 0-weight battery is parked while limiting (enough non-zero peers)."""
    clock = _FakeClock()
    lb = _make_balancer(clock)
    # abs demand 200 over two units => per-consumer 100 < 150 => limiting,
    # one active slot. "a" (full weight) stays active; "b" (0) is parked.
    reports = {"a": _report(0.0, 1.0), "b": _report(0.0, 0.0)}
    for i in range(3):
        clock.advance(1.0)
        lb._compute_efficiency_deprioritized(reports, (i,), 200.0)
    assert lb._deprioritized == {"b"}
    # Order: the zero-weight unit is sunk to the back of the priority list.
    assert lb._priority[0] == "a"
    assert lb._priority[-1] == "b"


def test_zero_weight_battery_runs_when_all_needed():
    """When demand needs every battery (slots == n), the 0-weight one runs too."""
    clock = _FakeClock()
    lb = _make_balancer(clock)
    # abs demand 600 over two units => per-consumer 300 >= 150 => no limiting.
    reports = {"a": _report(0.0, 1.0), "b": _report(0.0, 0.0)}
    for i in range(3):
        clock.advance(1.0)
        lb._compute_efficiency_deprioritized(reports, (i,), 600.0)
    assert lb._deprioritized == set()


def test_low_weight_battery_sinks_below_alphabetical_order():
    """The weight sort overrides the alphabetical fill order."""
    clock = _FakeClock()
    lb = _make_balancer(clock)
    # "a" sorts first alphabetically but has the lower weight, so it must end up
    # behind "b" once the descending-by-weight stable sort runs.
    reports = {"a": _report(0.0, 0.2), "b": _report(0.0, 1.0)}
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    assert lb._priority[0] == "b"
    assert lb._priority[-1] == "a"
    assert lb._deprioritized == {"a"}


def test_full_weight_head_rotates_after_full_interval():
    """A weight-1.0 head holds its slot for the whole rotation interval."""
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    reports = {"a": _report(0.0, 1.0), "b": _report(0.0, 1.0)}
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    rot0 = lb._last_rotation

    # Half the interval: no rotation yet.
    clock.advance(450.0)
    lb._compute_efficiency_deprioritized(reports, (1,), 200.0)
    assert lb._last_rotation == rot0

    # Past the full interval: the head rotates out.
    clock.advance(500.0)
    lb._compute_efficiency_deprioritized(reports, (2,), 200.0)
    assert lb._last_rotation > rot0


def test_half_weight_head_rotates_after_half_interval():
    """A weight-0.5 head gives up its slot after ~half the interval."""
    clock = _FakeClock()
    lb = _make_balancer(clock, rotation_interval=900.0)
    reports = {"a": _report(0.0, 0.5), "b": _report(0.0, 0.5)}
    clock.advance(1.0)
    lb._compute_efficiency_deprioritized(reports, (0,), 200.0)
    rot0 = lb._last_rotation

    # Just under half the interval: not yet.
    clock.advance(440.0)
    lb._compute_efficiency_deprioritized(reports, (1,), 200.0)
    assert lb._last_rotation == rot0

    # Past half the interval (900 * 0.5 = 450): the head rotates out early.
    clock.advance(20.0)
    lb._compute_efficiency_deprioritized(reports, (2,), 200.0)
    assert lb._last_rotation > rot0
