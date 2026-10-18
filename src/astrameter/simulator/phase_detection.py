"""Marstek battery-side CT phase detection (HMG-50 and Venus A/D/E).

A Marstek battery finds out which grid phase it is wired to by itself; the CT
takes no part beyond reporting per-phase readings (``docs/ct002-ct003-protocol.md``,
"Battery-side phase detection"). While it does not know its phase it sends the
phase character ``0`` and does not steer. One detection attempt:

1. command 0 W; on the next loop pass (VNSE3-0: after 3 s) snapshot the last
   accepted CT reading — A, B, C and total — as the baseline;
2. probe: discharge above 50 % SoC, charge at or below, at a fixed power
   (HMG-50: 2000 W; Venus: the configured power ``P``);
3. on every loop pass compare each phase's reading with its baseline; a phase
   *moved* when ``|reading - baseline| >= threshold`` (HMG-50 also caps the
   difference at 2500 W), with ``threshold = min(W, 2000) // 2`` where ``W``
   is the HMG-50's upper power window or the Venus ``P``;
4. decide once the move counter reaches 2 (HMG-50: exactly 2, Venus: at least
   2) on a pass where a phase moved:

   * HMG-50: exactly one phase moved -> that phase; more -> phase 5, and the
     detection **ends without retrying**;
   * Venus: the highest-numbered phase that moved on that pass;
   * no single phase moved but the total did -> phase ``D`` (HMG-50 only on
     meter codes 5-9 and only while all three phase readings are exactly 0;
     Venus on every meter type but 0, 1 and 13);

5. no decision before the timeout (20 s; 40 s for CT003 and a few other meter
   types; 60 s for Venus meter type 12) -> phase 5, output 0, retry after
   60 s; after the third failed attempt detection stops for good.

Phase 5 (and 0) is sent as ``0`` and the battery does not steer, so a battery
that failed detection sits at ~0 W reporting phase ``0`` until it is rebooted
or reconfigured. The loop re-evaluates the same CT reading on every pass
(HMG-50 at least every 100 ms, VNSE3-0 every 30 ms), so a single reading that
shows the move is enough: the counter reaches 2 on the next pass.

Provenance
----------
Read out of the archived Control images (`sphings79/marstek-firmware-archiv
<https://github.com/sphings79/marstek-firmware-archiv>`_). Addresses are
**runtime** addresses: HMG-50 v156 loads at ``0x08002800``, VNSE3-0 v151 at
``0x08004800`` (see :mod:`venus_integer_steering` for how the bases were
established). File offset = address - base.

=====================================  ============  ============
site                                   HMG-50 v156   VNSE3-0 v151
=====================================  ============  ============
detector                               0x0802369c    0x0802f594
meter-task loop that calls it          0x08023244    0x0802e8d0
request builder (phase byte -> char)   0x08020fc8    0x08028f0c
phase byte (RAM)                       0x10006fe4    0x20017559
detection active / status (RAM)        0x10006ff4/5  0x20000295/6
one-shot seconds timer                 0x08024c48    0x08030a20
seconds counter (1 ms tick hook)       0x08024294    0x0802c5b4
start detection                        0x08021bbc    (not read)
=====================================  ============  ============

Detection starts at boot when no phase is stored (HMG-50 0x08006a04), when the
meter type or the CT MAC is changed over BLE or MQTT, and on an MQTT
``seq_check`` command; ``rechrg_seq=N`` sets the phase directly. Only the
HMG-50 trigger was read; :meth:`PhaseDetector.start` mirrors it for both.

Inferred, not read: the two gate flags, modelled as ``charge_allowed`` /
``discharge_allowed`` (the firmware tests one BMS word above 50 % SoC and
another at or below, and the pairing with the probe direction suggests their
meaning), and the identity of the Venus probe power ``P`` (a configured power
word, ``0x2000012b + 4``), which the simulator takes to be the discharge limit.
The HMG-50 meter-code-1 path (the "no model suffix" fallback, see
:mod:`firmware_steering`) runs a different comparison and is not modelled:
AstraMeter always announces ``HME-3`` or ``HME-4``. VNSD-0 v151 was compared
with VNSE3-0 v151 instruction by instruction: the detection routine
(0x08032114), the CT reply parser and the MQTT handler are the same code, only
some fields of one structure sit a byte lower. Its power setter adds two PV
rules, modelled in :class:`~astrameter.simulator.battery.BatterySimulator`
(``VENUS_D_*``). VNSA-0 was not compared and is assumed to match. The B2500
(HMJ) runs a different routine, documented in the protocol notes and not
modelled here.
"""

from __future__ import annotations

from dataclasses import dataclass

PHASE_UNSET = 0
PHASE_D = 4
PHASE_FAILED = 5

_PHASE_CHARS = {1: "A", 2: "B", 3: "C", 4: "D"}

# Status byte next to the active flag (HMG-50 0x10006ff5): what the app shows.
STATUS_RUNNING = 1
STATUS_FAILED = 2
STATUS_DONE = 3

# Detection compares at most this much of the power window (both families).
WINDOW_CAP_W = 2000
# HMG-50: a phase difference above this is not counted as a move (0x08023840).
HMG50_MAX_MOVE_W = 2500
# HMG-50 probe power (float literals at 0x080239b0 / 0x080239b8).
HMG50_PROBE_W = 2000.0
# HMG-50 "zero" command (0x080239a8); the inverter treats it as idle.
HMG50_ZERO_W = 0.01

BASELINE_WAIT_S = 3
RETRY_WAIT_S = 60
MAX_ATTEMPTS = 3

# Upper bounds on the meter-task loop period: the queue wait times out after
# 100 ms (HMG-50, 0x08023272) / 30 ms (VNSE3-0, 0x0802e902) when no CT reading
# arrives, and every arrival wakes it too.
HMG50_LOOP_S = 0.1
VENUS_LOOP_S = 0.03

# Meter codes. HMG-50: parsed from the CT type of each accepted response
# (0x08026b44 compare chain). Venus: the configured meter type (request
# builder 0x08028f44). Only the CT-protocol types are listed.
HMG50_METER_CODES = {
    "HME-4": 3,
    "HME-3": 6,
    "SMR-0": 0xA,
    "SMR-1": 0xB,
    "SMR-2": 0xC,
    "TPM2-0": 0xD,
}
VENUS_METER_TYPES = {
    "HME-4": 3,
    "HME-3": 4,
    "SMR-0": 8,
    "SMR-1": 9,
    "SMR-2": 0xA,
    "TPM2-0": 0xB,
}


def phase_char(phase: int) -> str:
    """The request's phase character for a phase byte (anything else -> ``0``)."""
    return _PHASE_CHARS.get(phase, "0")


@dataclass(frozen=True)
class DetectorInputs:
    """What one loop pass reads.

    *now* is seconds since boot; the firmware's timers run on its whole-second
    counter. *readings* is the last accepted CT reading — A, B, C and total, in
    W, positive = import — and stays as it was between responses. *window_w* is
    the HMG-50's upper power window (the discharge limit currently allowed) or
    the Venus probe power ``P``.
    """

    now: float
    readings: tuple[int, int, int, int]
    soc_pct: int
    meter_online: bool
    meter_code: int
    window_w: float
    charge_allowed: bool = True
    discharge_allowed: bool = True


@dataclass(frozen=True)
class LoopResult:
    """What a loop pass did: a power command (W, positive = discharge) or
    ``None`` to leave the output alone, and whether the steering integrator's
    stored setpoint was zeroed (VNSE3-0 clears it, ``0x20000288``, whenever
    detection ends and while the meter is offline)."""

    command: float | None = None
    reset_setpoint: bool = False


class _SecondsTimer:
    """The firmware's one-shot elapsed-time helper (HMG-50 0x08024c48,
    VNSE3-0 0x08030a20).

    A zero start means unarmed: the first call arms it and returns ``False``;
    once *seconds* whole seconds have passed a call disarms it and returns
    ``True``. The counter is 16 bits wide, and a start stored at second 0 reads
    as unarmed again, both as in the firmware.
    """

    __slots__ = ("start",)

    def __init__(self) -> None:
        self.start = 0

    def expired(self, now: float, seconds: int) -> bool:
        now_s = int(now) & 0xFFFF
        if self.start == 0:
            self.start = now_s
            return False
        if (now_s - self.start) & 0xFFFF < seconds:
            return False
        self.start = 0
        return True


class PhaseDetector:
    """State shared by both families; subclasses run the per-pass logic."""

    loop_period_s: float = HMG50_LOOP_S
    zero_w: float = 0.0

    def __init__(self, stored_phase: int = PHASE_UNSET) -> None:
        # Boot: a stored phase means no detection; none stored starts one
        # (HMG-50 0x08006a04).
        self.phase = stored_phase if 0 < stored_phase <= PHASE_D else PHASE_UNSET
        self.active = self.phase == PHASE_UNSET
        self.status = 0
        self.attempts = 0
        self.baseline: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._reset_pending = False
        self._started = False
        self._probing = False
        self._moves = 0
        self._baseline_timer = _SecondsTimer()
        self._timeout_timer = _SecondsTimer()
        self._retry_timer = _SecondsTimer()

    @property
    def phase_char(self) -> str:
        return phase_char(self.phase)

    @property
    def steering(self) -> bool:
        """Whether the regular steering runs: only with a phase of 1-4."""
        return 0 < self.phase <= PHASE_D

    def start(self) -> None:
        """Start a detection from scratch (HMG-50 0x08021bbc), as a meter-type
        or CT-MAC change or ``seq_check`` does."""
        self.active = True
        self.status = STATUS_RUNNING
        self.attempts = 0
        self.phase = PHASE_UNSET
        self._started = False
        self._reset_pending = True

    def set_phase(self, phase: int) -> None:
        """Set the phase directly, as ``rechrg_seq=N`` does (0x08006dac): 1-4
        ends any running detection, 0 leaves the phase unset."""
        if not 0 <= phase <= PHASE_D:
            return
        self.phase = phase
        if phase:
            self.active = False

    def tick(self, inputs: DetectorInputs) -> LoopResult:  # pragma: no cover
        raise NotImplementedError

    def _consume_reset(self) -> None:
        if not self._reset_pending:
            return
        self._baseline_timer.start = 0
        self._timeout_timer.start = 0
        self._retry_timer.start = 0
        self._probing = False
        self._moves = 0
        self._reset_pending = False

    def _gate_open(self, inputs: DetectorInputs) -> bool:
        # Above 50 % SoC the discharge flag must be set, at or below the
        # charge flag; otherwise the pass returns before any timer runs.
        if inputs.soc_pct > 50:
            return inputs.discharge_allowed
        return inputs.charge_allowed

    def _retry_or_give_up(self, now: float) -> bool:
        """Phase 5: wait 60 s, then retry; after the third attempt stop.

        Returns ``True`` when detection just stopped for good.
        """
        if not self._retry_timer.expired(now, RETRY_WAIT_S):
            return False
        self.attempts += 1
        if self.attempts >= MAX_ATTEMPTS:
            self.active = False
            self.status = STATUS_FAILED
            self.attempts = MAX_ATTEMPTS
            return True
        self.phase = PHASE_UNSET
        return False

    @staticmethod
    def _threshold(window_w: float) -> int:
        # Venus: ``P`` is an unsigned word, capped at 2000 W, then halved.
        return min(max(0, int(window_w)), WINDOW_CAP_W) // 2


class HMG50PhaseDetector(PhaseDetector):
    """HMG-50 (Venus E 2.0) v156, detector at 0x0802369c."""

    loop_period_s = HMG50_LOOP_S
    zero_w = HMG50_ZERO_W

    @staticmethod
    def _threshold(window_w: float) -> int:
        # 0x080237d6: a window beyond +-2000 W reads as 2000; otherwise the
        # float itself is converted to unsigned, so a negative one reads as 0.
        if abs(int(window_w)) > WINDOW_CAP_W:
            return WINDOW_CAP_W // 2
        return (int(window_w) if window_w > 0 else 0) // 2

    def tick(self, inputs: DetectorInputs) -> LoopResult:
        # The meter-task loop holds the output at ~0 whenever the meter is
        # offline (0x08023256), then calls the detector regardless.
        offline_hold = None if inputs.meter_online else HMG50_ZERO_W
        cmd = self._detect(inputs)
        return LoopResult(command=cmd if cmd is not None else offline_hold)

    def _detect(self, inputs: DetectorInputs) -> float | None:
        self._consume_reset()
        if not self.active or not self._gate_open(inputs):
            return None
        now = inputs.now
        cmd: float | None = None
        if self.phase == PHASE_FAILED:
            cmd = HMG50_ZERO_W
            if self._retry_or_give_up(now):
                return cmd
        if self.phase != PHASE_UNSET or not inputs.meter_online:
            return cmd
        if not self._started:
            self._started = True
            return HMG50_ZERO_W
        if not self._probing:
            # The first call arms the timer and returns False, so the baseline
            # is taken on this pass; only a timer left armed by an earlier
            # attempt costs one extra pass.
            if self._baseline_timer.expired(now, BASELINE_WAIT_S):
                return cmd
            self.baseline = inputs.readings
            cmd = HMG50_PROBE_W if inputs.soc_pct > 50 else -HMG50_PROBE_W
            self._probing = True

        threshold = self._threshold(inputs.window_w)
        cur, base = inputs.readings, self.baseline
        moved = 0
        moved_phase = 0
        any_signal = False
        for i in range(3):
            diff = abs(cur[i] - base[i])
            if not any_signal:
                any_signal = cur[i] != 0
            if threshold <= diff <= HMG50_MAX_MOVE_W:
                any_signal = True
                self._moves += 1
                moved += 1
                moved_phase = i + 1
        if 5 <= inputs.meter_code <= 9 and not any_signal:
            diff = abs(cur[3] - base[3])
            if threshold <= diff <= HMG50_MAX_MOVE_W:
                self._moves = 2
                moved = 1
                moved_phase = PHASE_D

        if moved and self._moves == 2:
            # Decided (0x08023932). Detection ends here even when the result is
            # ambiguous: the active flag is cleared before the phase is chosen.
            self._started = False
            self.active = False
            self.status = STATUS_DONE
            self._probing = False
            self._moves = 0
            self._timeout_timer.start = 0
            self.phase = moved_phase if moved == 1 else PHASE_FAILED
            return HMG50_ZERO_W

        timeout = 40 if inputs.meter_code in (5, 6, 9) else 20
        if self._started and self._timeout_timer.expired(now, timeout):
            self._started = False
            self.phase = PHASE_FAILED
            self._probing = False
            self._moves = 0
            return HMG50_ZERO_W
        return cmd


class VenusPhaseDetector(PhaseDetector):
    """Venus A/D/E (VNSE3-0 v151 read), detector at 0x0802f594."""

    loop_period_s = VENUS_LOOP_S
    zero_w = 0.0

    def tick(self, inputs: DetectorInputs) -> LoopResult:
        # The loop skips the detector entirely while the meter is offline and
        # holds 0 W with the integrator cleared (0x0802e8de).
        if not inputs.meter_online:
            return LoopResult(command=0.0, reset_setpoint=True)
        return self._detect(inputs)

    def _detect(self, inputs: DetectorInputs) -> LoopResult:
        self._consume_reset()
        if not self.active or not self._gate_open(inputs):
            return LoopResult()
        now = inputs.now
        cmd: float | None = None
        if self.phase == PHASE_FAILED:
            cmd = 0.0
            if self._retry_or_give_up(now):
                return LoopResult(command=cmd)
        if self.phase != PHASE_UNSET:
            return LoopResult(command=cmd)
        if not self._started:
            self._started = True
            return LoopResult(command=0.0)
        if not self._probing:
            if not self._baseline_timer.expired(now, BASELINE_WAIT_S):
                return LoopResult(command=cmd)
            self.baseline = inputs.readings
            probe = max(0, int(inputs.window_w))
            # Meter type 10 always probes by charging (0x0802f6d8).
            discharge = inputs.soc_pct > 50 and inputs.meter_code != 0xA
            cmd = float(probe if discharge else -probe)
            self._probing = True

        threshold = self._threshold(inputs.window_w)
        cur, base = inputs.readings, self.baseline
        moved = 0
        moved_phase = 0
        for i in range(3):
            if abs(cur[i] - base[i]) >= threshold:
                self._moves += 1
                moved += 1
                moved_phase = i + 1
        total_allowed = inputs.meter_code not in (0, 1, 13)
        if not moved and total_allowed and abs(cur[3] - base[3]) >= threshold:
            self._moves = 2
            moved = 1
            moved_phase = PHASE_D

        if moved and self._moves >= 2:
            self._started = False
            self.active = False
            self.status = STATUS_DONE
            self._probing = False
            self._moves = 0
            self._timeout_timer.start = 0
            self.phase = moved_phase
            return LoopResult(command=0.0, reset_setpoint=True)

        mode = inputs.meter_code
        timeout = 60 if mode == 12 else 40 if mode in (2, 4, 7, 9) else 20
        if self._started and self._timeout_timer.expired(now, timeout):
            self._started = False
            self.phase = PHASE_FAILED
            self.status = STATUS_FAILED
            self._probing = False
            self._moves = 0
            return LoopResult(command=0.0, reset_setpoint=True)
        return LoopResult(command=cmd)


def make_phase_detector(
    meter_dev_type: str, stored_phase: int = PHASE_UNSET
) -> PhaseDetector:
    """The detector for a battery type; raises for families not modelled."""
    dt = meter_dev_type.upper()
    if dt.startswith("HMG"):
        return HMG50PhaseDetector(stored_phase)
    if dt.startswith("VNS"):
        return VenusPhaseDetector(stored_phase)
    raise ValueError(f"phase detection is not modelled for {meter_dev_type!r}")


def meter_code_for(meter_dev_type: str, ct_type: str) -> int:
    """The meter code a battery of *meter_dev_type* derives from *ct_type*."""
    table = (
        HMG50_METER_CODES
        if meter_dev_type.upper().startswith("HMG")
        else VENUS_METER_TYPES
    )
    return table.get(ct_type, 0)
