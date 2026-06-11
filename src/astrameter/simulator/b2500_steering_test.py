"""Tests for :class:`B2500SteeringController` — the B2500 (HMJ) DC-output steering.

Unlike the Venus controllers (an AC inverter nulling grid power with a float
ramp law), the B2500 is **DC-coupled**: it steers its DC output power per channel
with an integer hysteresis loop. ``GOLDEN`` are regulator trajectories
``(cmd, output)`` for the real B2500 — fixed setpoint, ``cmd = 60`` start, output
fed back each cycle (``power := previous output``); the controller reproduces
them exactly. The full :meth:`step` uses an **incremental** setpoint
(``output + 0.9 * grid``), so in a closed loop (``grid = load - output``) the
output integrates the grid to zero — verified separately.

``cmd`` is an internal command unit, not watts: ``output = (cmd - 5) * 10 / 59``,
so a ±100 ``cmd`` step moves the output by only ~17 W/cycle. SOC and temperature
are a separate BMS subsystem and never enter this loop.
"""

from __future__ import annotations

import pytest

from astrameter.simulator.b2500_steering import B2500SteeringController

# Closed-loop regulator trajectories: (cmd, output) per cycle, output fed back.
GOLDEN = [
    {
        "name": "converge_300",
        "setpoint": 300,
        "steps": [
            (160, 26),
            (260, 43),
            (360, 60),
            (460, 77),
            (560, 94),
            (660, 111),
            (760, 127),
            (860, 144),
            (960, 161),
            (1060, 178),
            (1160, 195),
            (1260, 212),
            (1360, 229),
            (1460, 246),
            (1560, 263),
            (1660, 280),
            (1760, 297),
            (1760, 297),
            (1760, 297),
        ],
    },
    {
        "name": "converge_324",
        "setpoint": 324,
        "steps": [
            (160, 26),
            (260, 43),
            (360, 60),
            (460, 77),
            (560, 94),
            (660, 111),
            (760, 127),
            (860, 144),
            (960, 161),
            (1060, 178),
            (1160, 195),
            (1260, 212),
            (1360, 229),
            (1460, 246),
            (1560, 263),
            (1660, 280),
            (1760, 297),
            (1860, 314),
            (1860, 314),
            (1860, 314),
        ],
    },
    {
        "name": "converge_400",
        "setpoint": 400,
        "steps": [
            (160, 26),
            (260, 43),
            (360, 60),
            (460, 77),
            (560, 94),
            (660, 111),
            (760, 127),
            (860, 144),
            (960, 161),
            (1060, 178),
            (1160, 195),
            (1260, 212),
            (1360, 229),
            (1460, 246),
            (1560, 263),
            (1660, 280),
            (1760, 297),
            (1860, 314),
            (1960, 331),
            (2060, 348),
            (2160, 365),
            (2260, 382),
            (2360, 399),
            (2360, 399),
            (2360, 399),
        ],
    },
]


@pytest.mark.parametrize("scenario", GOLDEN, ids=lambda s: s["name"])
def test_matches_golden_trajectory(scenario: dict) -> None:
    c = B2500SteeringController()
    power = 0
    for i, (exp_cmd, exp_out) in enumerate(scenario["steps"]):
        out = c.regulate(scenario["setpoint"], power)
        assert (c.cmd, out) == (exp_cmd, exp_out), (
            f"{scenario['name']} step {i}: got cmd={c.cmd} out={out}, "
            f"want cmd={exp_cmd} out={exp_out}"
        )
        power = out


def _closed_loop(load: int, max_power: int = 800, cycles: int = 120) -> int:
    """Drive one channel against *load* with ``grid = load - output``; return the
    settled output."""
    c = B2500SteeringController()
    power = 0
    for _ in range(cycles):
        power = c.step(load - power, power, max_power)
    return power


@pytest.mark.parametrize("load", [300, 600])
def test_step_nulls_the_grid(load: int) -> None:
    """The incremental setpoint integrates the residual grid to ~zero: in a
    closed loop the output converges to the load (within the ±10 W deadband)."""
    output = _closed_loop(load)
    assert abs(output - load) <= 15  # grid nulled


def test_step_clamps_to_envelope() -> None:
    """A load above the envelope parks the output at the max (not beyond)."""
    output = _closed_loop(2000, max_power=800)
    assert 780 <= output <= 815


def test_step_surplus_winds_down_to_idle() -> None:
    """A grid surplus winds the output down to idle — the B2500 has no AC input
    and never charges."""
    c = B2500SteeringController()
    power = 0
    for _ in range(60):
        power = c.step(300 - power, power, 800)  # wind up against a load
    assert power > 200
    for _ in range(60):
        power = c.step(-400, power, 800)  # sustained surplus
    assert 0 <= power <= 20  # idle, never negative


def test_setpoint_drop_winds_output_back_down() -> None:
    """After settling at 300 W, dropping the setpoint to 120 W winds the output
    back to within the deadband (≈127 W)."""
    c = B2500SteeringController()
    power = 0
    for _ in range(8):
        power = c.regulate(300, power)
    for _ in range(8):
        power = c.regulate(120, power)
    assert power == 127  # cmd 760, within +/-10 W of 120


def test_deadband_holds_within_10w() -> None:
    """The ±10 W deadband is inclusive: power == setpoint ± 10 holds."""
    for power, expect in [(289, "up"), (290, "hold"), (310, "hold"), (311, "down")]:
        c = B2500SteeringController(cmd=500)
        c.regulate(300, power)
        if expect == "up":
            assert c.cmd == 600
        elif expect == "down":
            assert c.cmd == 400
        else:
            assert c.cmd == 500


def test_output_calibration() -> None:
    """output = (cmd - 5) * 10 // 59."""
    for cmd, out in [
        (50, 7),
        (100, 16),
        (200, 33),
        (500, 83),
        (1000, 168),
        (2000, 338),
    ]:
        assert B2500SteeringController(cmd=cmd).output() == out


def test_step_setpoint_is_incremental() -> None:
    """``step`` targets ``output + 0.9 * grid`` (not an absolute fraction of
    grid): from a 100 W output with 200 W residual import it heads toward
    100 + 180 = 280 W, so the first cycle steps the output up."""
    c = B2500SteeringController(cmd=600)  # output 100 W
    assert c.output() == 100
    out = c.step(grid=200, power=100, max_power=800)
    assert out > 100  # rising toward 280, not parking at an absolute fraction
