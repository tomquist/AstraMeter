"""Marstek Venus D (VNSD-0) self-consumption steering controller.

Unlike the Venus C / HMG-50 (:mod:`astrameter.simulator.firmware_steering`), the
Venus D does **not** run the float gain-scheduled ramp. Its CT-following loop is
an integer proportional *integrator*: each CT response nudges a persistent
setpoint by a gain-scaled share of the grid error, clamps it to the discharge /
charge envelope, then parks it inside a small deadband. The behaviour is
documented in ``docs/ct002-ct003-protocol.md`` ("Model scope", VNSD-0 note).

Per-step law (``g`` = grid value to null, positive = importing)::

    error = g                                  # already net of any grid_standard
    gain  = ctrl_ratio / 100                   # 0.30 .. 1.00 (default 1.00)
    if measured_grid < 0 or error < 11:        # exporting, or a sub-11 W import
        if measured_grid < 1 and error < -10:  #   export: integrate gain*error
            setpoint += gain * error
        elif measured_grid * error < 0:        #   meter/error disagree: += error
            setpoint += error
        elif measured_grid < 0 and -11 < error < 0:
            setpoint += error - 5              #   small export drift, -5 W bias
        # else: hold
    else:                                      # import >= 11 W
        setpoint += gain * error - 5           #   integrate gain*error, -5 W bias
    setpoint = clamp(setpoint, lo, hi)         # charge / discharge envelope
    if abs(setpoint) < deadband and error < deadband:
        setpoint = 0                           # +-11 W single / +-15 W combined

Conventions
-----------
The returned ``setpoint`` is the commanded inverter power in the device's own
convention: **positive = discharge** (covers an import), **negative = charge**
(absorbs an export). ``hi`` is the discharge limit (positive), ``lo`` the charge
limit (negative). ``ctrl_ratio`` is the loop gain in percent; values outside
30-100 fall back to 100 (unity), matching the device. ``measured_grid`` is the
device's own grid reading and only selects the per-step branch; in a closed loop
it tracks ``error`` and defaults to it.

The per-step arithmetic is done in IEEE-754 single precision with truncation
toward zero, matching the device's 32-bit FPU, so :func:`_f32` rounds after each
operation.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

__all__ = [
    "DEADBAND_COMBINED_W",
    "DEADBAND_SINGLE_W",
    "DEFAULT_CTRL_RATIO",
    "IMPORT_THRESHOLD_W",
    "STEP_BIAS_W",
    "VenusDSteeringController",
]

# Final-setpoint deadband: a single-phase reporter parks within +-11 W, a
# combined (phase D / 合相) reporter within +-15 W.
DEADBAND_SINGLE_W = 11
DEADBAND_COMBINED_W = 15
# An import below this is treated as the export/hold side of the branch split.
IMPORT_THRESHOLD_W = 11
# Per-step bias subtracted on the integrating branches (nudges toward a hair of
# import rather than exact zero).
STEP_BIAS_W = 5
# Loop gain in percent. The device accepts 30..100 and falls back to 100 (unity)
# for anything outside that range.
DEFAULT_CTRL_RATIO = 100
_RATIO_MIN, _RATIO_MAX = 30, 100


def _f32(x: float) -> float:
    """Round *x* to single precision, mirroring the device's 32-bit FPU."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


# Percent -> gain fraction (``ctrl_ratio * 0.01`` in single precision).
_RATIO_SCALE = _f32(0.009999999776482582)


@dataclass
class VenusDSteeringController:
    """One battery's Venus-D steering state. Call :meth:`step` per CT response."""

    setpoint: int = 0
    ctrl_ratio: int = DEFAULT_CTRL_RATIO

    def _gain(self) -> float:
        ratio = int(self.ctrl_ratio)
        if ratio < _RATIO_MIN or ratio > _RATIO_MAX:
            ratio = DEFAULT_CTRL_RATIO
        return _f32(_f32(float(ratio)) * _RATIO_SCALE)

    def step(
        self,
        error: int,
        hi: float,
        lo: float,
        *,
        measured_grid: int | None = None,
        phase_count: int = 1,
    ) -> int:
        """Advance one regulation cycle for grid *error*; return the new setpoint.

        *hi* / *lo* are the discharge (positive) / charge (negative) limits.
        *measured_grid* selects the per-step branch (defaults to *error*).
        *phase_count* < 2 uses the +-11 W deadband, otherwise +-15 W.
        """
        err = int(error)
        mg = err if measured_grid is None else int(measured_grid)
        sp = int(self.setpoint)
        gain = self._gain()

        if mg < 0 or err < IMPORT_THRESHOLD_W:
            if mg < 1 and err < -10:
                sp = int(_f32(_f32(float(sp)) + _f32(_f32(float(err)) * gain)))
            elif mg * err < 0:
                sp = sp + err
            elif mg < 0 and -IMPORT_THRESHOLD_W < err < 0:
                sp = err - STEP_BIAS_W + sp
            # else: hold the setpoint unchanged
        else:
            step = _f32(_f32(_f32(float(err)) * gain) - _f32(float(STEP_BIAS_W)))
            sp = int(_f32(step + _f32(float(sp))))

        hi_i, lo_i = int(hi), int(lo)
        if sp > hi_i:
            sp = hi_i
        if sp < lo_i:
            sp = lo_i

        deadband = DEADBAND_SINGLE_W if phase_count < 2 else DEADBAND_COMBINED_W
        if abs(sp) < deadband and err < deadband:
            sp = 0

        self.setpoint = sp
        return sp
