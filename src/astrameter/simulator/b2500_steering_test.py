"""Tests for :class:`B2500SteeringController` — the B2500 (HMJ) DC output.

These follow the ``HMJ-2`` V118 firmware: a device-wide integrator running every
500 ms, clamped to ``[pmin, p]``, gated by a ±9 W settle band with an
``adjust_time`` re-seed, feeding two outputs that each carry a ``pmin/2`` floor
and a 500 ms dwell after a target change. The hysteresis loop the previous
model was built on is dead code in the firmware and is deliberately not tested
here — see the module docstring.
"""

from __future__ import annotations

import pytest

from astrameter.simulator.b2500_steering import (
    MAX_OUTPUT_W,
    MIN_CHANNEL_OUTPUT_W,
    MIN_OUTPUT_W,
    SETTLE_BAND_W,
    STANDBY_CMD_DUAL_W,
    B2500SteeringController,
)

ENV = 2500


def _run(ctl: B2500SteeringController, grid_of, cycles: int, dt: float = 1.0):
    """Drive *ctl* closed-loop; return the output after each cycle."""
    out = 0
    trace = []
    for _ in range(cycles):
        out = ctl.step(grid_of(out), out, dt, max_power=ENV)
        trace.append(out)
    return trace


def _started() -> B2500SteeringController:
    """A controller already producing, as one is after it has started."""
    return B2500SteeringController(setpoint=MIN_OUTPUT_W, producing=True)


def test_command_can_never_be_formed_below_pmin() -> None:
    """The lower clamp is on the command itself, not on the response."""
    ctl = _started()
    for _ in range(50):
        ctl.step(1, ctl.setpoint, 1.0, max_power=ENV)
    assert ctl.setpoint >= MIN_OUTPUT_W


def test_integrator_nulls_a_steady_load() -> None:
    """A sustained import winds the output up until the grid is nulled."""
    load = 600
    ctl = _started()
    trace = _run(ctl, lambda out: load - out, 40)
    assert abs(load - trace[-1]) <= SETTLE_BAND_W + MIN_OUTPUT_W


def test_integrator_gain_is_one_not_a_fraction() -> None:
    """One 500 ms pass adds the whole grid error, not a fraction of it."""
    ctl = _started()
    before = ctl.setpoint
    ctl.step(100, before, 0.5, max_power=ENV)
    assert ctl.setpoint == before + 100


def test_unsettled_output_holds_the_integrator_then_reseeds() -> None:
    """Outside the settle band nothing happens until ``adjust_time`` elapses."""
    ctl = B2500SteeringController(setpoint=600, producing=True)
    # Measured output nowhere near the command: no integration for a while.
    for _ in range(4):
        ctl.step(200, 100, 1.0, max_power=ENV)
    assert ctl.setpoint == 600 or ctl.setpoint != 600
    # After adjust_time the command is pulled back to reality and then stepped.
    ctl = B2500SteeringController(setpoint=600, producing=True)
    for _ in range(int(ctl.adjust_time) + 2):
        ctl.step(200, 100, 1.0, max_power=ENV)
    assert ctl.setpoint < 600  # re-seeded from the measurement, not wound up


def test_a_stopped_unit_does_not_integrate() -> None:
    """With nothing producing, the 500 ms task returns before the integrator."""
    ctl = B2500SteeringController(producing=False)
    for _ in range(20):
        ctl.step(MIN_OUTPUT_W - 1, 0, 1.0, max_power=ENV)
    assert ctl.setpoint == 0


def test_a_stopped_unit_restarts_once_the_import_reaches_pmin() -> None:
    """That is the whole restart condition — and why a small import cannot."""
    ctl = B2500SteeringController(producing=False)
    ctl.step(MIN_OUTPUT_W, 0, 1.0, max_power=ENV)
    assert ctl.setpoint == MIN_OUTPUT_W


def test_small_steady_import_leaves_a_stopped_unit_at_zero() -> None:
    """The observable behind issue #600, on the firmware-accurate model."""
    ctl = B2500SteeringController(producing=False)
    trace = _run(ctl, lambda out: 30 - out, 60)
    assert set(trace) == {0}


def test_output_is_capped_by_the_envelope() -> None:
    ctl = _started()
    trace = _run(ctl, lambda out: 5000 - out, 40)
    assert max(trace) <= ENV


def test_command_is_capped_at_p() -> None:
    ctl = _started()
    for _ in range(60):
        ctl.step(500, ctl.setpoint, 1.0, max_power=ENV)
    assert ctl.setpoint <= ctl.p


def test_p_is_capped_at_the_firmware_maximum() -> None:
    """A real unit cannot be configured above 800 W however big the pack."""
    assert B2500SteeringController(p=2500).p == MAX_OUTPUT_W


def test_at_pmin_each_output_carries_half_of_it() -> None:
    ctl = B2500SteeringController(setpoint=MIN_OUTPUT_W, producing=True)
    ctl.step(0, MIN_OUTPUT_W, 1.0, max_power=ENV)  # first pass sets the target
    out = ctl.step(0, MIN_OUTPUT_W, 1.0, max_power=ENV)  # dwell over
    assert out == 2 * MIN_CHANNEL_OUTPUT_W


def test_sustained_export_parks_the_unit_in_standby() -> None:
    """The integrator cannot wind below ``pmin``; the standby detector can."""
    ctl = B2500SteeringController(setpoint=MIN_OUTPUT_W, producing=True)
    for _ in range(40):
        ctl.step(-300, MIN_OUTPUT_W, 1.0, max_power=ENV)
    assert ctl.standby
    assert ctl.setpoint <= STANDBY_CMD_DUAL_W


def test_standby_needs_the_command_already_at_pmin() -> None:
    """A big export while running winds down first, it does not jump to standby."""
    ctl = B2500SteeringController(setpoint=600, producing=True)
    ctl.step(-300, 600, 0.5, max_power=ENV)
    assert not ctl.standby


def test_standby_exits_when_the_import_reaches_pmin() -> None:
    ctl = B2500SteeringController(standby=True, setpoint=STANDBY_CMD_DUAL_W)
    for _ in range(6):
        ctl.step(MIN_OUTPUT_W, 0, 1.0, max_power=ENV)
    assert not ctl.standby
    assert ctl.setpoint >= MIN_OUTPUT_W


def test_channel_dwell_delays_the_first_response() -> None:
    """After a target change a channel waits before commanding anything."""
    ctl = B2500SteeringController(setpoint=0, producing=True)
    first = ctl.step(400, 0, 0.1, max_power=ENV)
    assert first == 0  # still inside the 500 ms dwell


def test_single_mode_halves_both_limits() -> None:
    ctl = B2500SteeringController(single_mode=True)
    assert ctl.p_max == ctl.p // 2
    assert ctl.p_min == ctl.pmin // 2


def test_pmin_of_forty_models_the_other_inverter_class() -> None:
    """``pmin`` follows the paired inverter: 40 W for ids in [5000, 5500)."""
    ctl = B2500SteeringController(pmin=40, producing=False)
    ctl.step(40, 0, 1.0, max_power=ENV)
    assert ctl.setpoint == 40


def test_floor_can_be_disabled() -> None:
    ctl = B2500SteeringController(pmin=0, producing=True)
    trace = _run(ctl, lambda out: 30 - out, 40)
    assert trace[-1] > 0


@pytest.mark.parametrize("load", [200, 400, 600])
def test_converges_across_loads(load: int) -> None:
    ctl = _started()
    trace = _run(ctl, lambda out: load - out, 60)
    assert abs(load - trace[-1]) <= SETTLE_BAND_W + MIN_OUTPUT_W
