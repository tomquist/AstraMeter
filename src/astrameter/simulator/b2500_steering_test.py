"""Tests for :class:`B2500SteeringController` — the B2500 (HMJ) DC-output steering.

Unlike the Venus controllers (an AC inverter nulling grid power with a float
ramp law), the B2500 is **DC-coupled**: it steers its DC output power per channel
with an integer hysteresis loop. ``GOLDEN`` are closed-loop regulator
trajectories ``(cmd, output)`` for the real B2500 — starting ``cmd = 60`` with the
output fed back each cycle (``power := previous output``). ``GATED`` adds the
meter-derived setpoint ``min(0.9 * grid, max_power / 2)`` ahead of the regulator.
The controller must reproduce them exactly.

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


# Full pass: setpoint = min(0.9 * grid, max_power / 2), then the same regulator.
GATED = [
    {
        "name": "grid_360",
        "grid": 360,
        "max_power": 800,
        "setpoint": 324,  # 0.9 * 360, below the 400 W half-envelope
        "steps": next(s["steps"] for s in GOLDEN if s["name"] == "converge_324"),
    },
    {
        "name": "grid_1000_clamped",
        "grid": 1000,
        "max_power": 800,
        "setpoint": 400,  # 0.9 * 1000 = 900, clamped to max_power / 2 = 400
        "steps": next(s["steps"] for s in GOLDEN if s["name"] == "converge_400"),
    },
]


@pytest.mark.parametrize("scenario", GATED, ids=lambda s: s["name"])
def test_matches_gated_trajectory(scenario: dict) -> None:
    c = B2500SteeringController()
    assert (
        c.setpoint_from_grid(scenario["grid"], scenario["max_power"])
        == scenario["setpoint"]
    )
    power = 0
    for i, (exp_cmd, exp_out) in enumerate(scenario["steps"]):
        out = c.step(scenario["grid"], power, scenario["max_power"])
        assert (c.cmd, out) == (exp_cmd, exp_out), (
            f"{scenario['name']} step {i}: got cmd={c.cmd} out={out}, "
            f"want cmd={exp_cmd} out={exp_out}"
        )
        power = out


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


def test_setpoint_from_grid_clamps_to_half_envelope() -> None:
    assert B2500SteeringController.setpoint_from_grid(360, 800) == 324  # 0.9 * 360
    assert B2500SteeringController.setpoint_from_grid(1000, 800) == 400  # clamp 800/2
    assert B2500SteeringController.setpoint_from_grid(0, 800) == 0
