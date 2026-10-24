"""Tests for :mod:`phase_detection`: each pins one firmware rule.

The detector is driven one loop pass at a time, with the CT reading supplied
directly, so every rule can be checked at the pass it applies on.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from astrameter.simulator.phase_detection import (
    HMG50_PROBE_W,
    HMG50_ZERO_W,
    PHASE_D,
    PHASE_FAILED,
    PHASE_UNSET,
    STATUS_DONE,
    STATUS_FAILED,
    DetectorInputs,
    HMG50PhaseDetector,
    PhaseDetector,
    VenusPhaseDetector,
    make_phase_detector,
    meter_code_for,
    phase_char,
)

HME4_HMG = meter_code_for("HMG-50", "HME-4")
HME3_HMG = meter_code_for("HMG-50", "HME-3")
HME4_VENUS = meter_code_for("VNSE3-0", "HME-4")
HME3_VENUS = meter_code_for("VNSE3-0", "HME-3")

BASE = (100, 50, -20, 130)


def _inputs(**kw: Any) -> DetectorInputs:
    defaults: dict[str, Any] = {
        "now": 10.0,
        "readings": BASE,
        "soc_pct": 80,
        "meter_online": True,
        "meter_code": HME4_HMG,
        "window_w": 800.0,
    }
    defaults.update(kw)
    return DetectorInputs(**defaults)


def _start_probe_hmg(
    **kw: Any,
) -> tuple[HMG50PhaseDetector, DetectorInputs, float | None]:
    det = HMG50PhaseDetector()
    inp = _inputs(**kw)
    assert det.tick(inp).command == HMG50_ZERO_W
    inp = replace(inp, now=inp.now + 0.1)
    probe = det.tick(inp).command
    return det, inp, probe


def _moved(readings: tuple[int, ...], phase: int, by: int) -> tuple[int, int, int, int]:
    out = list(readings)
    out[phase] += by
    out[3] += by
    return (out[0], out[1], out[2], out[3])


class TestHmg50:
    def test_boot_without_stored_phase_detects(self) -> None:
        det = HMG50PhaseDetector()
        assert det.active and det.phase == PHASE_UNSET and det.phase_char == "0"
        assert not det.steering

    def test_stored_phase_skips_detection(self) -> None:
        det = HMG50PhaseDetector(stored_phase=2)
        assert not det.active and det.phase_char == "B" and det.steering
        assert det.tick(_inputs()).command is None

    @pytest.mark.parametrize(
        ("soc", "probe"), [(51, HMG50_PROBE_W), (50, -HMG50_PROBE_W)]
    )
    def test_probe_direction_follows_soc(self, soc: int, probe: float) -> None:
        _, _, cmd = _start_probe_hmg(soc_pct=soc)
        assert cmd == probe

    def test_baseline_is_taken_on_the_pass_after_zeroing(self) -> None:
        det, _, _ = _start_probe_hmg()
        assert det.baseline == BASE

    def test_one_phase_moving_decides_after_two_passes(self) -> None:
        det, inp, _ = _start_probe_hmg()
        moved = replace(inp, now=inp.now + 1, readings=_moved(BASE, 1, -400))
        assert det.tick(moved).command is None  # move counter 1
        assert det.phase == PHASE_UNSET
        res = det.tick(replace(moved, now=moved.now + 0.1))  # same reading again
        assert det.phase == 2 and det.phase_char == "B"
        assert not det.active and det.status == STATUS_DONE
        assert res.command == HMG50_ZERO_W

    @pytest.mark.parametrize(
        ("window", "need"), [(800, 400), (3000, 1000), (-3000, 1000)]
    )
    def test_threshold_is_half_the_capped_window(self, window: int, need: int) -> None:
        det, inp, _ = _start_probe_hmg(window_w=float(window))
        short = replace(inp, now=inp.now + 1, readings=_moved(BASE, 0, need - 1))
        det.tick(short)
        det.tick(replace(short, now=short.now + 0.1))
        assert det.phase == PHASE_UNSET
        enough = replace(short, readings=_moved(BASE, 0, need), now=short.now + 0.2)
        det.tick(enough)
        det.tick(replace(enough, now=enough.now + 0.1))
        assert det.phase == 1

    def test_zero_window_never_decides(self) -> None:
        # Threshold 0: every phase counts on every pass, so the move counter
        # jumps past 2 and the attempt can only time out.
        det, inp, _ = _start_probe_hmg(window_w=0.0)
        for k in range(1, 200):
            det.tick(replace(inp, now=inp.now + 0.1 * k))
            if det.phase != PHASE_UNSET:
                break
        assert det.phase == PHASE_FAILED and det.active

    def test_moves_above_2500_w_are_ignored(self) -> None:
        det, inp, _ = _start_probe_hmg()
        big = replace(inp, now=inp.now + 1, readings=_moved(BASE, 2, 2501))
        det.tick(big)
        det.tick(replace(big, now=big.now + 0.1))
        assert det.phase == PHASE_UNSET

    def test_two_phases_moving_ends_detection_without_retry(self) -> None:
        det, inp, _ = _start_probe_hmg()
        both = _moved(_moved(BASE, 0, 500), 1, 500)
        det.tick(replace(inp, now=inp.now + 1, readings=both))
        assert det.phase == PHASE_FAILED and det.phase_char == "0"
        assert not det.active and det.status == STATUS_DONE
        # No retry, ever.
        later = replace(inp, now=inp.now + 600, readings=both)
        assert det.tick(later).command is None
        assert det.phase == PHASE_FAILED

    def test_total_decides_d_only_on_ct003_with_silent_phases(self) -> None:
        silent = (0, 0, 0, 0)
        det, inp, _ = _start_probe_hmg(meter_code=HME3_HMG, readings=silent)
        det.tick(replace(inp, now=inp.now + 1, readings=(0, 0, 0, -800)))
        assert det.phase == PHASE_D and det.phase_char == "D"

    def test_total_does_not_decide_on_ct002(self) -> None:
        silent = (0, 0, 0, 0)
        det, inp, _ = _start_probe_hmg(meter_code=HME4_HMG, readings=silent)
        for k in range(1, 5):
            det.tick(replace(inp, now=inp.now + k * 0.1, readings=(0, 0, 0, -800)))
        assert det.phase == PHASE_UNSET

    def test_total_ignored_while_a_phase_reads_non_zero(self) -> None:
        det, inp, _ = _start_probe_hmg(meter_code=HME3_HMG, readings=(5, 0, 0, 5))
        for k in range(1, 5):
            det.tick(replace(inp, now=inp.now + k * 0.1, readings=(5, 0, 0, -800)))
        assert det.phase == PHASE_UNSET

    @pytest.mark.parametrize(("code", "timeout"), [(HME4_HMG, 20), (HME3_HMG, 40)])
    def test_timeout_then_three_attempts(self, code: int, timeout: int) -> None:
        det, inp, _ = _start_probe_hmg(meter_code=code)
        start = inp.now  # timeout timer armed on this pass
        t = start
        # Nothing moves: the attempt times out after *timeout* whole seconds.
        while det.phase == PHASE_UNSET:
            t += 0.1
            det.tick(replace(inp, now=t))
        assert int(t) - int(start) == timeout
        assert det.phase == PHASE_FAILED and det.active
        attempts_started = 1
        while det.active:
            t += 0.1
            res = det.tick(replace(inp, now=t))
            if det.phase == PHASE_FAILED:
                assert res.command == HMG50_ZERO_W
            elif det._probing and res.command == HMG50_PROBE_W:
                attempts_started += 1
        assert attempts_started == 3
        assert det.status == STATUS_FAILED and det.attempts == 3
        assert det.phase == PHASE_FAILED and det.phase_char == "0"

    def test_gate_closed_freezes_detection(self) -> None:
        det = HMG50PhaseDetector()
        inp = _inputs(soc_pct=80, discharge_allowed=False)
        for k in range(100):
            assert det.tick(replace(inp, now=10 + k)).command is None
        assert det.phase == PHASE_UNSET and not det._started
        low = _inputs(soc_pct=20, charge_allowed=False)
        assert det.tick(low).command is None

    def test_offline_meter_holds_zero_and_waits(self) -> None:
        det = HMG50PhaseDetector()
        inp = _inputs(meter_online=False)
        for k in range(50):
            assert det.tick(replace(inp, now=10 + k * 0.1)).command == HMG50_ZERO_W
        assert not det._started

    def test_start_restarts_from_scratch(self) -> None:
        det = HMG50PhaseDetector(stored_phase=1)
        det.start()
        assert det.active and det.phase == PHASE_UNSET and det.attempts == 0
        assert det.tick(_inputs()).command == HMG50_ZERO_W

    def test_set_phase_ends_detection(self) -> None:
        det = HMG50PhaseDetector()
        det.set_phase(3)
        assert det.phase_char == "C" and not det.active
        det.set_phase(7)  # out of range: ignored
        assert det.phase == 3


def _start_probe_venus(
    **kw: Any,
) -> tuple[VenusPhaseDetector, DetectorInputs, float | None]:
    kw.setdefault("meter_code", HME4_VENUS)
    det = VenusPhaseDetector()
    inp = _inputs(**kw)
    assert det.tick(inp).command == 0.0  # first pass: 0 W
    t = inp.now
    cmd = None
    while not det._probing:
        t += 0.03
        cmd = det.tick(replace(inp, now=t)).command
    return det, replace(inp, now=t), cmd


class TestVenus:
    def test_baseline_waits_three_seconds(self) -> None:
        det, inp, cmd = _start_probe_venus(now=10.5)
        # Timer armed at second 10 on the pass after zeroing, expires at 13.
        assert int(inp.now) == 13
        assert cmd == 800.0 and det.baseline == BASE

    @pytest.mark.parametrize(
        ("soc", "code", "probe"),
        [(51, HME4_VENUS, 800.0), (50, HME4_VENUS, -800.0), (90, 0xA, -800.0)],
    )
    def test_probe_direction(self, soc: int, code: int, probe: float) -> None:
        _, _, cmd = _start_probe_venus(soc_pct=soc, meter_code=code)
        assert cmd == probe

    def test_threshold_is_half_the_capped_power(self) -> None:
        det, inp, _ = _start_probe_venus(window_w=2500.0)
        r = replace(inp, now=inp.now + 1, readings=_moved(BASE, 0, 999))
        det.tick(r)
        det.tick(replace(r, now=r.now + 0.03))
        assert det.phase == PHASE_UNSET
        r = replace(r, now=r.now + 0.1, readings=_moved(BASE, 0, 1000))
        det.tick(r)
        det.tick(replace(r, now=r.now + 0.03))
        assert det.phase == 1

    def test_no_upper_bound_on_the_move(self) -> None:
        det, inp, _ = _start_probe_venus()
        r = replace(inp, now=inp.now + 1, readings=_moved(BASE, 1, 5000))
        det.tick(r)
        det.tick(replace(r, now=r.now + 0.03))
        assert det.phase == 2

    def test_two_phases_moving_pick_the_highest(self) -> None:
        det, inp, _ = _start_probe_venus()
        res = det.tick(
            replace(inp, now=inp.now + 1, readings=_moved(_moved(BASE, 0, 500), 2, 500))
        )
        assert det.phase == 3 and det.status == STATUS_DONE and not det.active
        assert res.command == 0.0 and res.reset_setpoint

    @pytest.mark.parametrize("code", [HME4_VENUS, HME3_VENUS])
    def test_total_decides_d_when_no_phase_moved(self, code: int) -> None:
        det, inp, _ = _start_probe_venus(meter_code=code)
        det.tick(replace(inp, now=inp.now + 1, readings=(150, 100, 30, 130 + 600)))
        assert det.phase == PHASE_D

    @pytest.mark.parametrize("code", [0, 1, 13])
    def test_total_not_used_for_some_meter_types(self, code: int) -> None:
        det, inp, _ = _start_probe_venus(meter_code=code)
        for k in range(1, 5):
            det.tick(replace(inp, now=inp.now + k * 0.03, readings=(100, 50, -20, 900)))
        assert det.phase == PHASE_UNSET

    @pytest.mark.parametrize(
        ("code", "timeout"), [(HME4_VENUS, 20), (HME3_VENUS, 40), (12, 60)]
    )
    def test_timeout_marks_failed_and_retries(self, code: int, timeout: int) -> None:
        det, inp, _ = _start_probe_venus(meter_code=code)
        t, start = inp.now, inp.now
        res = None
        while det.phase == PHASE_UNSET:
            t += 0.03
            res = det.tick(replace(inp, now=t))
        assert int(t) - int(start) == timeout
        assert det.status == STATUS_FAILED and det.active
        assert res is not None and res.reset_setpoint and res.command == 0.0
        while det.active:
            t += 0.03
            det.tick(replace(inp, now=t))
        assert det.attempts == 3 and det.phase == PHASE_FAILED

    def test_offline_skips_detection_and_clears_the_integrator(self) -> None:
        det = VenusPhaseDetector()
        res = det.tick(_inputs(meter_online=False, meter_code=HME4_VENUS))
        assert res.command == 0.0 and res.reset_setpoint
        assert not det._started


def test_phase_chars() -> None:
    assert [phase_char(p) for p in range(7)] == ["0", "A", "B", "C", "D", "0", "0"]


@pytest.mark.parametrize(
    ("dev", "cls"),
    [("HMG-50", HMG50PhaseDetector), ("VNSE3-0", VenusPhaseDetector)],
)
def test_factory(dev: str, cls: type[PhaseDetector]) -> None:
    assert isinstance(make_phase_detector(dev), cls)


def test_factory_rejects_unmodelled_families() -> None:
    with pytest.raises(ValueError, match="not modelled"):
        make_phase_detector("HMJ-2")


def test_meter_codes() -> None:
    assert (HME4_HMG, HME3_HMG) == (3, 6)
    assert (HME4_VENUS, HME3_VENUS) == (3, 4)
    assert meter_code_for("HMG-50", "XYZ") == 0
