"""Venus integer steering: gate + integrator, pinned to the firmware.

Every expectation traces to the disassembly cited in
:mod:`astrameter.simulator.venus_integer_steering`. ``GOLDEN`` drives the bare
law (:meth:`step_raw`); the gate tests drive :meth:`step`.
"""

from __future__ import annotations

import pytest

from .venus_integer_steering import (
    DEADBAND_W,
    SMALL_IMPORT_HOLD_W,
    VenusIntegerSteeringController,
)


def _run(steps, **kwargs):
    """Feed ``(g, out)`` pairs to a fresh controller; return the setpoints."""
    c = VenusIntegerSteeringController(**kwargs)
    return [c.step(g, 2500.0, -2500.0, out=out) for g, out in steps]


# ---------------------------------------------------------------------------
# The bare law
# ---------------------------------------------------------------------------

# Each trajectory fixes the loop gain (``ctrl_ratio`` %) and the discharge /
# charge limits (``hi`` / ``lo``) and lists ``(g, out, device_count, setpoint)``
# steps the integrator must reproduce exactly (single precision, +setpoint =
# discharge).
GOLDEN = [
    # Sustained 500 W import integrates ~495 W/cycle to the discharge clamp —
    # no near-zero step-size shaping, unlike the HMG-50 ramp.
    {
        "name": "import_ramp",
        "ratio": 100,
        "hi": 2500,
        "lo": -2500,
        "steps": [
            (500, 500, 1, 495),
            (500, 500, 1, 990),
            (500, 500, 1, 1485),
            (500, 500, 1, 1980),
            (500, 500, 1, 2475),
            (500, 500, 1, 2500),
        ],
    },
    # Sustained export integrates the other way (charge), no -5 W bias branch.
    {
        "name": "export_ramp",
        "ratio": 100,
        "hi": 2500,
        "lo": -2500,
        "steps": [
            (-500, -500, 1, -500),
            (-500, -500, 1, -1000),
            (-500, -500, 1, -1500),
        ],
    },
    # ctrl_ratio scales the step: 50 % halves it (before the -5 W bias).
    {
        "name": "half_gain",
        "ratio": 50,
        "hi": 2500,
        "lo": -2500,
        "steps": [
            (500, 500, 1, 245),
            (500, 500, 1, 490),
        ],
    },
    # Alone on the bucket: ±11 W park. A 12 W import acts (→7); 10/8 W park.
    {
        "name": "park_single",
        "ratio": 100,
        "hi": 2500,
        "lo": -2500,
        "steps": [
            (10, 10, 1, 0),
            (8, 8, 1, 0),
            (12, 12, 1, 7),
            (-8, -8, 1, 0),
        ],
    },
    # Sharing the bucket widens the park to ±15 W: 16 W acts (→11).
    {
        "name": "park_shared",
        "ratio": 100,
        "hi": 2500,
        "lo": -2500,
        "steps": [
            (12, 12, 2, 0),
            (14, 14, 2, 0),
            (16, 16, 2, 11),
            (-14, -14, 2, 0),
        ],
    },
    # Discharge clamp at hi=800; charge clamp at lo=-2200.
    {
        "name": "clamp_discharge",
        "ratio": 100,
        "hi": 800,
        "lo": -2200,
        "steps": [(3000, 3000, 1, 800)] * 4,
    },
    {
        "name": "clamp_charge",
        "ratio": 100,
        "hi": 800,
        "lo": -2200,
        "steps": [(-3000, -3000, 1, -2200)] * 4,
    },
    # When the unit's own output disagrees in sign with the bucket value, the
    # step is the full error (gain-1 branch), no -5 W bias.
    {
        "name": "signflip",
        "ratio": 100,
        "hi": 2500,
        "lo": -2500,
        "steps": [
            (50, -50, 1, 50),
            (-50, 50, 1, 0),
            (300, 300, 1, 295),
        ],
    },
]


@pytest.mark.parametrize("case", GOLDEN, ids=lambda c: c["name"])
def test_golden_trajectories(case: dict) -> None:
    """The integrator reproduces its reference trajectories exactly."""
    ctl = VenusIntegerSteeringController(ctrl_ratio=case["ratio"])
    for g, out, device_count, expected in case["steps"]:
        got = ctl.step_raw(
            g, case["hi"], case["lo"], out=out, device_count=device_count
        )
        assert got == expected, f"{case['name']}: g={g} -> {got}, expected {expected}"
        assert ctl.setpoint == got


def test_invalid_ctrl_ratio_falls_back_to_unity() -> None:
    """ctrl_ratio outside 30-100 % falls back to 100 % (unity), like the device."""
    unity = VenusIntegerSteeringController(ctrl_ratio=100).step_raw(500, 2500, -2500)
    for bad in (0, 29, 101, 255):
        ctl = VenusIntegerSteeringController(ctrl_ratio=bad)
        assert ctl.step_raw(500, 2500, -2500) == unity


def test_small_sustained_import_walks_the_setpoint_up() -> None:
    """The behaviour the whole model turns on (issue #600 follow-up).

    A 30 W reading is far below what the inverter will start for, and the unit
    reports 0 W throughout — yet the stored setpoint climbs by ``g - 5`` every
    cycle, because the update never consults the measured output. A B2500 in the
    same situation stays at 0 W forever.
    """
    assert _run([(30, 0)] * 6) == [25, 50, 75, 100, 125, 150]


# ---------------------------------------------------------------------------
# The input-conditioning gate
# ---------------------------------------------------------------------------


def test_deadband_holds_a_small_reading_only_while_at_rest() -> None:
    """±10 W deadband, conditioned on the unit's own output (not on the grid).

    A small reading is dropped outright while the unit is at rest, and reaches
    the integrator once it is producing. Both runs first wind the setpoint up to
    50 W — with the unit still reporting 0 W, since a real one takes seconds to
    start — so the difference shows in the setpoint rather than being hidden by
    the final ±11 W park.
    """

    def wind_up_then(last_out: float) -> list[int]:
        c = VenusIntegerSteeringController(prev_g=30)
        out = [c.step(30, 2500.0, -2500.0, out=0) for _ in range(2)]
        return [*out, c.step(-8, 2500.0, -2500.0, out=last_out)]

    assert wind_up_then(0) == [25, 50, 50]  # at rest: dropped by the gate
    assert wind_up_then(200) == [25, 50, 42]  # producing: integrated

    # And from rest, a small export never accumulates at all.
    assert _run([(-8, 0)] * 4) == [0, 0, 0, 0]


def test_small_import_hold_applies_even_while_producing() -> None:
    """A residual import under 10 W is held whatever the unit is doing."""
    assert _run([(9, 500)] * 3) == [0, 0, 0]
    assert _run([(9, 0)] * 3) == [0, 0, 0]


def test_spike_filter_is_a_one_shot() -> None:
    """A >50 W jump the own output cannot explain is skipped exactly once.

    The second sample is forced through even though it still looks like a spike
    relative to nothing having moved — the firmware clears its flag and runs.
    """
    c = VenusIntegerSteeringController()
    assert c.step(20, 2500.0, -2500.0, out=0) == 15  # baseline
    assert c.step(400, 2500.0, -2500.0, out=0) == 15  # spike: held
    assert c.step(400, 2500.0, -2500.0, out=0) == 410  # one-shot expired


def test_spike_needs_the_own_output_to_have_stayed_still() -> None:
    """A jump the unit's own ramp explains is not a spike."""
    c = VenusIntegerSteeringController()
    c.step(20, 2500.0, -2500.0, out=0)
    # Own output moved 300 W between samples, so the grid jump is our own doing.
    assert c.step(400, 2500.0, -2500.0, out=300) == 410


def test_cold_start_spike_filters_the_first_large_reading() -> None:
    """The gate's baselines are zero-initialised globals in the firmware.

    So the first reading after a boot that is more than 50 W away from zero
    looks exactly like a spike and is held — once.
    """
    assert _run([(100, 0)] * 3) == [0, 95, 190]


def test_a_held_sample_leaves_the_integrator_untouched() -> None:
    """The firmware returns before the update; state must not drift."""
    c = VenusIntegerSteeringController()
    c.step(30, 2500.0, -2500.0, out=0)
    before = (c.setpoint, c.ctrl_ratio)
    c.step(4, 2500.0, -2500.0, out=0)  # small-import hold
    assert (c.setpoint, c.ctrl_ratio) == before


def test_gate_thresholds_are_the_venus_ones() -> None:
    """±10 W, not the HMG-50's ±20 W: a 15 W reading at rest is acted on."""
    assert DEADBAND_W == 10
    assert SMALL_IMPORT_HOLD_W == 10
    assert _run([(15, 0)] * 2) == [10, 20]


# ---------------------------------------------------------------------------
# The bucket's device count
# ---------------------------------------------------------------------------


def test_device_count_splits_the_bucket_value() -> None:
    """``g = g / nb`` (signed, truncating) before anything else."""
    c = VenusIntegerSteeringController()
    assert c.step(300, 2500.0, -2500.0, out=0, device_count=3) == 95  # 100 - 5
    c = VenusIntegerSteeringController()
    assert c.step(-300, 2500.0, -2500.0, out=0, device_count=3) == -100


def test_device_count_widens_the_park() -> None:
    """±11 W alone, ±15 W when the bucket is shared.

    Both cases integrate the same 13 W to an 8 W setpoint. Alone, the reading
    (13) is outside ±11 so the setpoint stands; shared, both the setpoint and
    the reading are inside ±15 and the device parks at zero.
    """
    solo = VenusIntegerSteeringController()
    assert solo.step(13, 2500.0, -2500.0, out=100, device_count=1) == 8
    shared = VenusIntegerSteeringController()
    assert shared.step(26, 2500.0, -2500.0, out=100, device_count=2) == 0


def test_shared_bucket_disables_the_spike_filter() -> None:
    c = VenusIntegerSteeringController()
    c.step(20, 2500.0, -2500.0, out=0, device_count=2)
    # Same jump that a solo unit would hold once.
    assert c.step(400, 2500.0, -2500.0, out=0, device_count=2) != 15
