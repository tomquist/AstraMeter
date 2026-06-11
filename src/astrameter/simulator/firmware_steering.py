"""Marstek Venus-class self-consumption steering controller.

This reproduces the closed-loop control law a real Marstek Venus (HMG-50 /
VNSE3-0) battery runs on the grid value it reads back from the CT: a
gain-scheduled, accelerating step that drives the selected grid power toward
zero. It is the steering law documented in
``docs/ct002-ct003-protocol.md`` ("Steering / ramp logic"); the constants and
per-step arithmetic below are the exact values that controller uses.

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

__all__ = ["HARD_CLAMP_W", "STEP_BASE_W", "FirmwareSteeringController"]

HARD_CLAMP_W = 2500.0
STEP_BASE_W = 10.0
WINDOW_PULLBACK_W = 100.0
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

    def __post_init__(self) -> None:
        self.setpoint = _f32(self.setpoint)

    def step(self, g: float, hi: float, lo: float, *, device_count: int = 1) -> float:
        """Advance one regulation cycle for grid value *g*; return the new setpoint.

        *hi* / *lo* are the charge / discharge power limits. *device_count* is
        the number of batteries sharing this phase bucket (the grid value is
        split evenly between them, as the device does).
        """
        nb = max(1, int(device_count))
        g = int(g)
        if nb > 1:
            g = int(g / nb)  # signed integer division, truncated toward zero

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
