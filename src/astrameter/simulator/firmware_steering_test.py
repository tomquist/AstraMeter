"""Tests for :mod:`astrameter.simulator.firmware_steering`.

``GOLDEN`` is a set of reference ``(g) -> (setpoint, ramp, last)`` trajectories
for the Venus-class steering controller. The controller must reproduce them
exactly (single-precision), so these lock the control law in place.
"""

from __future__ import annotations

import math

import pytest

from astrameter.simulator.firmware_steering import (
    GAIN,
    HARD_CLAMP_W,
    FirmwareSteeringController,
)

GOLDEN = [
    {
        "name": "import_converge",
        "setpoint0": 0.0,
        "hi": 2500.0,
        "lo": -2500.0,
        "steps": [
            (200, -50.23, 0, 200),
            (150, -100.35, -1, 150),
            (100, -160.37, -2, 100),
            (50, -210.37, -3, 50),
            (10, -220.37, -4, 10),
            (0, -220.37, -5, 0),
            (0, -220.37, -5, 0),
        ],
    },
    {
        "name": "export_converge",
        "setpoint0": 0.0,
        "hi": 2500.0,
        "lo": -2500.0,
        "steps": [
            (-200, 200.0, -1, -200),
            (-150, 350.0, 0, -150),
            (-100, 450.0, 0, -100),
            (-50, 500.0, 0, -50),
            (-10, 510.0, 0, -10),
            (0, 510.0, 0, 0),
            (0, 510.0, -1, 0),
        ],
    },
    {
        "name": "const_import",
        "setpoint0": 0.0,
        "hi": 2500.0,
        "lo": -2500.0,
        "steps": [
            (300, -50.23, 0, 300),
            (300, -100.35, -1, 300),
            (300, -160.37, -2, 300),
            (300, -340.67, -3, 300),
            (300, -640.67, -4, 300),
            (300, -940.67, -5, 300),
            (300, -1240.6699, -5, 300),
            (300, -1540.6699, -5, 300),
        ],
    },
    {
        "name": "const_export",
        "setpoint0": 0.0,
        "hi": 2500.0,
        "lo": -2500.0,
        "steps": [
            (-300, 300.0, -1, -300),
            (-300, 600.0, -2, -300),
            (-300, 900.0, -3, -300),
            (-300, 1200.0, -4, -300),
            (-300, 1500.0, -5, -300),
            (-300, 1800.0, -5, -300),
        ],
    },
    {
        "name": "settle_to_zero",
        "setpoint0": 0.0,
        "hi": 2500.0,
        "lo": -2500.0,
        "steps": [
            (300, -50.23, 0, 300),
            (200, -100.35, -1, 200),
            (100, -160.37, -2, 100),
            (0, -160.37, -3, 0),
            (0, -160.37, -4, 0),
            (0, -160.37, -5, 0),
        ],
    },
    {
        "name": "oscillate",
        "setpoint0": 0.0,
        "hi": 2500.0,
        "lo": -2500.0,
        "steps": [
            (200, -50.23, 0, 200),
            (-200, 149.77, -1, -200),
            (200, 139.77, 0, 200),
            (-200, 339.77, -1, -200),
            (200, 329.77, 0, 200),
        ],
    },
    {
        "name": "window_lo_300",
        "setpoint0": 0.0,
        "hi": 2500.0,
        "lo": -300.0,
        "steps": [
            (300, -50.23, 0, 300),
            (300, -100.35, -1, 300),
            (300, -160.37, -2, 300),
            (300, -340.67, -3, 300),
            (300, -200.0, 0, 300),
            (300, -250.12, -1, 300),
            (300, -310.14, -2, 300),
            (300, -200.0, 0, 300),
            (300, -250.12, -1, 300),
            (300, -310.14, -2, 300),
        ],
    },
    {
        "name": "window_hi_300",
        "setpoint0": 0.0,
        "hi": 300.0,
        "lo": -2500.0,
        "steps": [
            (-300, 300.0, -1, -300),
            (-300, 600.0, -2, -300),
            (-300, 200.0, -1, -300),
            (-300, 500.0, -2, -300),
            (-300, 200.0, -1, -300),
            (-300, 500.0, -2, -300),
            (-300, 200.0, -1, -300),
            (-300, 500.0, -2, -300),
            (-300, 200.0, -1, -300),
            (-300, 500.0, -2, -300),
        ],
    },
    {
        "name": "hard_clamp",
        "setpoint0": 0.0,
        "hi": 2500.0,
        "lo": -2500.0,
        "steps": [
            (-700, 700.0, -1, -700),
            (-700, 1400.0, -2, -700),
            (-700, 2100.0, -3, -700),
            (-700, 2500.0, -4, -700),
            (-700, 2500.0, -5, -700),
            (-700, 2500.0, -5, -700),
        ],
    },
    {
        "name": "start_pos",
        "setpoint0": 400.0,
        "hi": 800.0,
        "lo": -800.0,
        "steps": [
            (100, 349.77, 0, 100),
            (-50, 399.77, -1, -50),
            (200, 349.54, 0, 200),
            (-300, 649.54, -1, -300),
            (40, 639.54, 0, 40),
        ],
    },
    {
        "name": "mixed",
        "setpoint0": 0.0,
        "hi": 800.0,
        "lo": -800.0,
        "steps": [
            (123, -50.23, 0, 123),
            (-45, -5.23, -1, -45),
            (67, -15.23, 0, 67),
            (-200, 184.77, -1, -200),
            (15, 174.77, 0, 15),
            (-15, 189.77, -1, -15),
            (5, 184.77, 0, 5),
            (250, 134.54, 0, 250),
        ],
    },
]


@pytest.mark.parametrize("scenario", GOLDEN, ids=lambda s: s["name"])
def test_matches_golden_trajectory(scenario: dict) -> None:
    c = FirmwareSteeringController(setpoint=scenario["setpoint0"])
    for i, (g, exp_sp, exp_ramp, exp_last) in enumerate(scenario["steps"]):
        sp = c.step(g, scenario["hi"], scenario["lo"])
        assert sp == pytest.approx(exp_sp, abs=0.01), (
            f"{scenario['name']} step {i}: setpoint {sp} != {exp_sp}"
        )
        assert c.ramp == exp_ramp, f"{scenario['name']} step {i}: ramp"
        assert c.last == exp_last, f"{scenario['name']} step {i}: last"


def test_import_drives_discharge() -> None:
    """A positive grid (import) drives the setpoint negative (discharge)."""
    c = FirmwareSteeringController()
    sp = c.step(200, 2500.0, -2500.0)
    assert sp < 0


def test_export_drives_charge() -> None:
    """A negative grid (export) drives the setpoint positive (charge)."""
    c = FirmwareSteeringController()
    sp = c.step(-200, 2500.0, -2500.0)
    assert sp > 0


def test_ramp_accelerates_under_sustained_error() -> None:
    """Sustained import makes the per-step correction grow (ramp falls to -5)."""
    c = FirmwareSteeringController()
    deltas = []
    prev = 0.0
    for _ in range(8):
        sp = c.step(300, 2500.0, -2500.0)
        deltas.append(abs(sp - prev))
        prev = sp
    assert c.ramp == -5
    # later steps correct by more than the first (acceleration)
    assert deltas[4] > deltas[0]


def test_hard_clamp() -> None:
    c = FirmwareSteeringController()
    for _ in range(20):
        sp = c.step(-700, 2500.0, -2500.0)
    assert sp == HARD_CLAMP_W


def test_gain_table_values() -> None:
    """The gain magnitudes near zero are ~50 W, growing to ~410 W at the edges."""
    assert GAIN[0] == pytest.approx(50.23, abs=0.01)
    assert GAIN[-5] == pytest.approx(410.35, abs=0.01)
    assert GAIN[5] == pytest.approx(400.40, abs=0.01)
    assert not math.isnan(GAIN[3])


def test_share_split_divides_grid() -> None:
    """With N batteries on a phase, each acts on grid/N.

    Use a small grid value so the ``|g|`` cap binds (a large value is capped by
    the gain table either way and would hide the split).
    """
    solo = FirmwareSteeringController()
    pair = FirmwareSteeringController()
    sp_solo = solo.step(60, 2500.0, -2500.0)  # capped at |g|=60
    sp_pair = pair.step(60, 2500.0, -2500.0, device_count=2)  # acts on g=30
    assert sp_solo == pytest.approx(-50.23, abs=0.01)
    assert sp_pair == pytest.approx(-30.0, abs=0.01)
