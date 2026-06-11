"""Tests for :mod:`astrameter.simulator.firmware_steering`.

``GOLDEN`` is a set of reference ``(g) -> (setpoint, ramp, last)`` trajectories
for the bare gain-scheduled ramp law (:meth:`step_raw`, no input gate). The
controller must reproduce them exactly (single-precision), so these lock the
control law in place.

``GATED`` covers the full :meth:`step` pipeline (share split, then the
conditioning gate — spike filter, deadband, small-import hold — then the ramp
law). Each step is ``((g, out), acted, setpoint, ramp, last)`` and reproduces
the real HMG-50's behavior for that input sequence. ``acted`` records the gate
decision; a held sample leaves ``setpoint``/``ramp``/``last`` unchanged.
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
        sp = c.step_raw(g, scenario["hi"], scenario["lo"])
        assert sp == pytest.approx(exp_sp, abs=0.01), (
            f"{scenario['name']} step {i}: setpoint {sp} != {exp_sp}"
        )
        assert c.ramp == exp_ramp, f"{scenario['name']} step {i}: ramp"
        assert c.last == exp_last, f"{scenario['name']} step {i}: last"


# Gated reference trajectories. Each step is
# ``((g, out), acted, setpoint, ramp, last)`` and locks the full pipeline
# against the real HMG-50's behavior (see module docstring). A held sample
# (``acted is False``) repeats the previous setpoint/ramp/last.
GATED = [
    {
        # A persistent import step. Sample 1 is debounced (>50 W jump, own
        # output still); from sample 2 own output tracks the setpoint so the
        # gate keeps accepting — except the |g| < 10 tail (last two samples),
        # where the small-import hold parks the setpoint.
        "name": "spike_then_import_converge",
        "steps": [
            ((200, 0), False, 0.0, 0, 0),
            ((200, 0), True, -50.23, 0, 200),
            ((150, 50), True, -100.35, -1, 150),
            ((100, 100), True, -160.37, -2, 100),
            ((50, 160), True, -210.37, -3, 50),
            ((10, 210), True, -220.37, -4, 10),
            ((0, 220), False, -220.37, -4, 10),
            ((0, 220), False, -220.37, -4, 10),
        ],
    },
    {
        # |g| < 20 with the battery idle holds (deadband); once own output
        # exceeds ~1 W the same grid value is acted on again.
        "name": "deadband_idle_hold",
        "steps": [
            ((30, 0), True, -30.0, 0, 30),
            ((15, 0), False, -30.0, 0, 30),
            ((15, 0), False, -30.0, 0, 30),
            ((15, 30), True, -45.0, -1, 15),
            ((10, 45), True, -55.0, -2, 10),
            ((0, 55), False, -55.0, -2, 10),
        ],
    },
    {
        # A one-sample 200 W blip on an idle battery never reaches the ramp:
        # the blip is debounced and the surrounding ~0 W samples are inside the
        # deadband. The return to a real 30 W load is acted on once it persists
        # (the blip is already in the baseline, so its jump is gone).
        "name": "spike_blip_rejected",
        "steps": [
            ((0, 0), False, 0.0, 0, 0),
            ((200, 0), False, 0.0, 0, 0),
            ((0, 0), False, 0.0, 0, 0),
            ((30, 0), True, -30.0, 0, 30),
            ((30, 0), True, -60.0, -1, 30),
        ],
    },
    {
        # A steadily drifting export (every consecutive delta > 50 W) while the
        # battery's own output never moves keeps being skipped — the firmware
        # has no one-shot, it gates on the unexplained jump every cycle.
        "name": "drift_keeps_skipping",
        "steps": [
            ((-9000, 0), False, 0.0, 0, 0),
            ((-8800, 0), False, 0.0, 0, 0),
            ((-8600, 0), False, 0.0, 0, 0),
            ((-8400, 0), False, 0.0, 0, 0),
        ],
    },
    {
        # The deadband test is signed: a battery charging at -300 W with a
        # small |g| is held just like an idle one, and only steers once |g|
        # leaves the deadband.
        "name": "charging_deadband_signed",
        "steps": [
            ((-300, 0), False, 0.0, 0, 0),
            ((15, -300), False, 0.0, 0, 0),
            ((15, -300), False, 0.0, 0, 0),
            ((-30, -300), True, 30.0, -1, -30),
            ((-30, -285), True, 60.0, -2, -30),
        ],
    },
    {
        # A residual import of 0 <= g < 10 is held even while the battery is
        # producing; a 12 W import (>= 10) is acted on.
        "name": "small_import_hold",
        "steps": [
            ((5, 0), False, 0.0, 0, 0),
            ((5, 100), False, 0.0, 0, 0),
            ((12, 100), True, -12.0, 0, 12),
            ((0, 0), False, -12.0, 0, 12),
        ],
    },
]


@pytest.mark.parametrize("scenario", GATED, ids=lambda s: s["name"])
def test_matches_gated_golden_trajectory(scenario: dict) -> None:
    c = FirmwareSteeringController()
    for i, ((g, out), acted, exp_sp, exp_ramp, exp_last) in enumerate(
        scenario["steps"]
    ):
        held_sp = c.setpoint
        sp = c.step(g, 2500.0, -2500.0, out=out)
        assert sp == pytest.approx(exp_sp, abs=0.01), (
            f"{scenario['name']} step {i}: setpoint {sp} != {exp_sp}"
        )
        assert c.ramp == exp_ramp, f"{scenario['name']} step {i}: ramp"
        assert c.last == exp_last, f"{scenario['name']} step {i}: last"
        if not acted:
            assert sp == pytest.approx(held_sp, abs=0.01), (
                f"{scenario['name']} step {i}: held sample changed the setpoint"
            )


def test_import_drives_discharge() -> None:
    """A positive grid (import) drives the setpoint negative (discharge)."""
    c = FirmwareSteeringController()
    sp = c.step_raw(200, 2500.0, -2500.0)
    assert sp < 0


def test_export_drives_charge() -> None:
    """A negative grid (export) drives the setpoint positive (charge)."""
    c = FirmwareSteeringController()
    sp = c.step_raw(-200, 2500.0, -2500.0)
    assert sp > 0


def test_ramp_accelerates_under_sustained_error() -> None:
    """Sustained import makes the per-step correction grow (ramp falls to -5)."""
    c = FirmwareSteeringController()
    deltas = []
    prev = 0.0
    for _ in range(8):
        sp = c.step_raw(300, 2500.0, -2500.0)
        deltas.append(abs(sp - prev))
        prev = sp
    assert c.ramp == -5
    # later steps correct by more than the first (acceleration)
    assert deltas[4] > deltas[0]


def test_hard_clamp() -> None:
    c = FirmwareSteeringController()
    for _ in range(20):
        sp = c.step_raw(-700, 2500.0, -2500.0)
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
    sp_solo = solo.step_raw(60, 2500.0, -2500.0)  # capped at |g|=60
    sp_pair = pair.step_raw(60, 2500.0, -2500.0, device_count=2)  # acts on g=30
    assert sp_solo == pytest.approx(-50.23, abs=0.01)
    assert sp_pair == pytest.approx(-30.0, abs=0.01)


# -- input-conditioning gate (spike filter / deadband / small-import hold) ----


def _raw_first_step(g: int) -> float:
    """The ramp law's response to *g* from a fresh controller."""
    return FirmwareSteeringController().step_raw(g, 2500.0, -2500.0)


def test_held_sample_preserves_ramp_state_but_advances_baseline() -> None:
    """A held sample leaves the ramp-law state untouched, but — like the
    firmware — still advances the gate's own ``prev_g`` / ``prev_out``."""
    c = FirmwareSteeringController()
    c.step(30, 2500.0, -2500.0, out=0.0)
    ramp_state = (c.setpoint, c.ramp, c.last, c.s58, c.ref)
    sp = c.step(19, 2500.0, -2500.0, out=0.0)  # |g| < 20, out < 1 → deadband
    assert sp == ramp_state[0]
    assert (c.setpoint, c.ramp, c.last, c.s58, c.ref) == ramp_state
    assert (c.prev_g, c.prev_out) == (19, 0)  # baseline moved


def test_deadband_boundary_20w_steps() -> None:
    """abs(g) == 20 is outside the deadband and is acted on."""
    c = FirmwareSteeringController()
    sp = c.step(20, 2500.0, -2500.0, out=0.0)
    assert sp == _raw_first_step(20)


def test_deadband_inactive_when_own_output_at_least_1w() -> None:
    """The deadband needs the battery's own output below 1 W."""
    c = FirmwareSteeringController()
    sp = c.step(19, 2500.0, -2500.0, out=1.0)
    assert sp == _raw_first_step(19)


def test_deadband_is_signed_holds_a_charging_battery() -> None:
    """The own-output condition is signed (``out < 1``): a battery charging at
    -300 W with a small |g| is held just like an idle one."""
    c = FirmwareSteeringController()
    sp = c.step(19, 2500.0, -2500.0, out=-300.0)
    assert sp == 0.0
    assert c.last == 0  # ramp law never ran


def test_small_import_hold_below_10w() -> None:
    """A residual import of 0 <= g < 10 is held even while producing; 10 W
    (the boundary) is acted on."""
    held = FirmwareSteeringController()
    assert held.step(5, 2500.0, -2500.0, out=100.0) == 0.0
    assert held.last == 0
    acted = FirmwareSteeringController()
    assert acted.step(10, 2500.0, -2500.0, out=100.0) == _raw_first_step(10)


def test_deadband_applies_to_share_split_value() -> None:
    """The split (g/nb) happens before the gate, as on the device."""
    c = FirmwareSteeringController()
    sp = c.step(30, 2500.0, -2500.0, device_count=2, out=0.0)  # acts on g=15
    assert sp == 0.0
    assert c.last == 0


def test_spike_updates_baseline_but_skips_ramp() -> None:
    """A skipped spike still becomes the next cycle's comparison baseline."""
    c = FirmwareSteeringController()
    sp = c.step(200, 2500.0, -2500.0, out=0.0)
    assert sp == 0.0
    assert c.prev_g == 200
    assert (c.ramp, c.last) == (0, 0)


def test_spike_not_filtered_when_own_output_moved() -> None:
    """A grid jump explained by the battery's own output change (>= 20 W) is
    acted on immediately — it is not an external transient."""
    c = FirmwareSteeringController()
    sp = c.step(200, 2500.0, -2500.0, out=20.0)
    assert sp == _raw_first_step(200)


def test_spike_has_no_one_shot_sustained_drift_keeps_skipping() -> None:
    """The filter gates on the unexplained jump every cycle (no one-shot): a
    steady drift whose own output never moves keeps being skipped."""
    c = FirmwareSteeringController()
    for g in (-9000, -8800, -8600, -8400):
        assert c.step(g, 2500.0, -2500.0, out=0.0) == 0.0
    assert c.last == 0  # ramp law never ran


def test_spike_boundary_50w_not_filtered() -> None:
    """A 50 W jump is not a spike (the filter needs > 50 W)."""
    c = FirmwareSteeringController()
    sp = c.step(50, 2500.0, -2500.0, out=0.0)
    assert sp == _raw_first_step(50)
