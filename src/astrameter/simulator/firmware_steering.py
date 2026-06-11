"""Marstek HMG-50 self-consumption steering controller.

This reproduces the closed-loop control law a real Marstek HMG-50 (Venus C/D)
battery runs on the grid value it reads back from the CT: a gain-scheduled,
accelerating step that drives the selected grid power toward zero, preceded by
the device's input-conditioning gate (a >50 W spike filter, a ±20 W deadband
and a small-import hold). It is the steering law documented in
``docs/ct002-ct003-protocol.md`` ("Steering / ramp logic"); the gain table and
per-step arithmetic, and the gate thresholds and ordering, are the exact values
the HMG-50 firmware uses.

Scope: the gain table and ramp arithmetic here are the **HMG-50** (Venus C/D)
ones. The VNSE3-0 (Venus E) shares the *same input-conditioning gate* — the
same >50 W spike filter, <20 W own-output exemption, signed deadband and
small-import hold — but with a tighter ±10 W deadband, and it uses a different
ramp/step law (no float gain table), so the GOLDEN ramp vectors here are
HMG-50-specific.

Conventions
-----------
``g`` is the grid value to null, **positive = importing** from the grid.
``setpoint`` is the commanded inverter power, **positive = charge, negative =
discharge** (so a positive import drives the setpoint negative → discharge).
``hi`` / ``lo`` are the charge / discharge power limits (``hi`` positive,
``lo`` negative); the absolute hardware clamp is ±2500 W.

The arithmetic is done in IEEE-754 single precision (the device has a 32-bit
FPU), so :func:`_f32` rounds after each operation to match the device
bit-for-bit.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

__all__ = [
    "DEADBAND_W",
    "HARD_CLAMP_W",
    "SMALL_IMPORT_HOLD_W",
    "SPIKE_JUMP_W",
    "SPIKE_OWN_DELTA_W",
    "STEP_BASE_W",
    "FirmwareSteeringController",
]

HARD_CLAMP_W = 2500.0
STEP_BASE_W = 10.0
WINDOW_PULLBACK_W = 100.0
# Input-conditioning thresholds the HMG-50 applies before the ramp law (see
# the module docstring).
DEADBAND_W = 20
SPIKE_JUMP_W = 50
SPIKE_OWN_DELTA_W = 20
SMALL_IMPORT_HOLD_W = 10
_RAMP_MIN, _RAMP_MAX = -5, 5


def _f32(x: float) -> float:
    """Round *x* to single precision, mirroring the device's 32-bit FPU."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _hex_f32(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits))[0]


# Maximum correction step (W) per regulation cycle, indexed by the ramp /
# acceleration counter. Values are the exact single-precision constants the
# device uses (given here by their bit patterns to avoid rounding drift).
GAIN: dict[int, float] = {
    -5: _hex_f32(0x43CD2CCD),  # 410.35
    -4: _hex_f32(0x43AF347B),  # 350.41
    -3: _hex_f32(0x43344CCD),  # 180.30
    -2: _hex_f32(0x4270147B),  # 60.02
    -1: _hex_f32(0x42487AE1),  # 50.12
    0: _hex_f32(0x4248EB85),  # 50.23
    1: _hex_f32(0x42486666),  # 50.10
    2: _hex_f32(0x4248D70A),  # 50.21
    3: _hex_f32(0x42C8051F),  # 100.01
    4: _hex_f32(0x4348051F),  # 200.02
    5: _hex_f32(0x43C83333),  # 400.40
}


def _u32(x: int) -> int:
    return x & 0xFFFFFFFF


def _share_split(g: float, device_count: int) -> int:
    """Split the grid value across the batteries sharing the bucket."""
    nb = max(1, int(device_count))
    g = int(g)
    if nb > 1:
        g = int(g / nb)  # signed integer division, truncated toward zero
    return g


@dataclass
class FirmwareSteeringController:
    """One battery's steering state. Call :meth:`step` once per CT response."""

    setpoint: float = 0.0
    ramp: int = 0
    last: int = 0
    # ``s58`` tracks a running (unsigned) minimum of ``g``; ``ref`` is the value
    # captured the last time the grid reading rose. Both feed the step size.
    s58: int = 0
    ref: int = 0
    # Previous cycle's (share-split) grid value and own output (as an int), the
    # baseline the conditioning gate compares the next sample against. The
    # firmware updates both on *every* regulation cycle — held and skipped
    # samples included.
    prev_g: int = 0
    prev_out: int = 0

    def __post_init__(self) -> None:
        self.setpoint = _f32(self.setpoint)

    def step(
        self,
        g: float,
        hi: float,
        lo: float,
        *,
        device_count: int = 1,
        out: float = 0.0,
    ) -> float:
        """Advance one regulation cycle for grid value *g*; return the new setpoint.

        Runs the firmware's input-conditioning gate (:meth:`_gate`) before the
        ramp law (:meth:`step_raw`). When the gate holds the sample the setpoint
        is returned unchanged and the ramp-law state is untouched; only the
        gate's own baseline (``prev_g`` / ``prev_out``) advances.

        *out* is the battery's own measured output power (positive =
        discharging, the same convention as the request's power field). *hi* /
        *lo* are the charge / discharge power limits. *device_count* is the
        number of batteries sharing this phase bucket (the grid value is split
        evenly between them, as the device does, before the gate sees it).
        """
        g = _share_split(g, device_count)
        if not self._gate(g, out):
            return self._clamp()
        return self.step_raw(g, hi, lo)

    def _gate(self, g: int, out: float) -> bool:
        """The firmware's pre-ramp conditioning gate; ``True`` ⇒ run the ramp.

        Mirrors the HMG-50 firmware's pre-ramp conditioning bit-for-bit. Three
        conditions hold the setpoint, in this order:

        - **>50 W spike filter** — a grid jump over 50 W from the previous
          sample that the battery's own output change (< 20 W) does not explain
          is treated as a transient and skipped. There is **no** one-shot: the
          baseline advances every cycle, so a *sustained* drift whose own
          output never moves keeps being skipped, while a real load step is
          picked up on the next sample once the jump is in the baseline.
        - **±20 W deadband** — ``abs(g) < 20`` with the battery's own output
          below 1 W (a **signed** test: a charging battery reads negative and
          is also held) parks the setpoint.
        - **small-import hold** — a residual import of ``0 <= g < 10`` is held
          even while the battery is producing, so it doesn't chase the last
          few watts of import.

        ``prev_g`` / ``prev_out`` are updated on every call, matching the
        firmware (which stores them before any of the hold branches).
        """
        out_i = int(out)
        is_spike = (
            abs(g) > DEADBAND_W
            and abs(g - self.prev_g) > SPIKE_JUMP_W
            and abs(out_i - self.prev_out) < SPIKE_OWN_DELTA_W
        )
        self.prev_g = g
        self.prev_out = out_i
        if is_spike:
            return False
        if abs(g) < DEADBAND_W and out_i < 1:
            return False
        # A small residual import (0 <= g < 10) is held; everything else runs.
        return not 0 <= g < SMALL_IMPORT_HOLD_W

    def step_raw(
        self, g: float, hi: float, lo: float, *, device_count: int = 1
    ) -> float:
        """Advance the bare gain-scheduled ramp law, bypassing the input gates.

        This is the inner law :meth:`step` runs on samples that pass the
        deadband / spike-filter conditioning. *hi* / *lo* are the charge /
        discharge power limits. *device_count* splits the grid value across
        the batteries sharing the bucket.
        """
        g = _share_split(g, device_count)

        sp = self.setpoint
        # Keep the setpoint inside the dynamic power window; pulling it back in
        # also resets the ramp. ``last`` is still updated (the device falls
        # through to the same store/apply tail).
        if sp > hi:
            self.setpoint = _f32(hi - WINDOW_PULLBACK_W)
            self.ramp = -1
            self.last = g
            return self._clamp()
        if sp < lo:
            self.setpoint = _f32(lo + WINDOW_PULLBACK_W)
            self.ramp = 0
            self.last = g
            return self._clamp()

        # Running unsigned minimum of g (a negative g reads as a large unsigned
        # value, so it never lowers the minimum).
        m = self.s58 if _u32(self.s58) < _u32(g) else g
        self.s58 = m
        if g > self.last:  # grid rose: brake the ramp toward zero, re-baseline
            self.ref = m
            self.s58 = g
            self.ramp = -1 if self.ramp > 0 else 0
        else:  # grid steady/falling: accelerate in the current direction
            self.ramp = (
                min(self.ramp + 1, _RAMP_MAX)
                if self.ramp > 0
                else max(self.ramp - 1, _RAMP_MIN)
            )

        # Step size: sqrt(|g**2 - ref**2|) + 10, capped by the gain table and by
        # g itself. The g cap is signed, so a negative g forces a negative step
        # (i.e. the opposite direction → charging).
        v = _u32(g * g - self.ref * self.ref)
        step = _f32(_f32(math.sqrt(float(v))) + STEP_BASE_W)
        gain = GAIN[self.ramp]
        if gain < step:
            step = gain
        gf = float(g)
        if gf < step:
            step = gf

        if self.ramp > 0:
            self.setpoint = _f32(self.setpoint + step)
        else:
            self.setpoint = _f32(self.setpoint - step)
        self.last = g
        return self._clamp()

    def _clamp(self) -> float:
        if self.setpoint > HARD_CLAMP_W:
            self.setpoint = HARD_CLAMP_W
        elif self.setpoint < -HARD_CLAMP_W:
            self.setpoint = -HARD_CLAMP_W
        return self.setpoint
