"""Marstek integer self-consumption steering (Venus A/D/E, and the HMG-50).

The AC-coupled Venus units that are not an HMG-50 all run one control law: an
integer proportional integrator behind an input-conditioning gate, one
regulation step per CT response. None of them contains the HMG-50's float
gain-scheduled ramp (:mod:`astrameter.simulator.firmware_steering`).

**The HMG-50 runs this law too**, for every meter it recognises. Its firmware
carries both: it parses a model code out of the meter's greeting and only takes
the float ramp when that code is ``1``, which is the "no model suffix" fallback.
AstraMeter announces itself as ``HME-4``, which the HMG-50's parser maps to a
code of its own, so an HMG-50 steered by AstraMeter runs the integer law below
and never the gain table. Its branch structure is identical to the Venus one;
only the single-unit park (``park_alone``) and the gate's rest deadband
(``deadband``: ±20 W rather than ±10 W) differ.

Per-step law (``g`` = the bucket value to null, positive = importing; ``out`` =
the unit's own measured output, positive = discharging)::

    g = g / nb                                 # share split across the bucket
    if not gate(g, out, nb): return            # spike filter / deadband / hold
    gain = ctrl_ratio / 100                    # 0.30 .. 1.00 (default 1.00)
    if out < 0 or g < 11:
        if out < 1 and g < -10:   setpoint += gain * g
        elif out * g < 0:         setpoint += g
        elif out < 0 and -11 < g < 0:
            setpoint += g - 5
        # else: hold
    else:                         setpoint += gain * g - 5
    setpoint = clamp(setpoint, lo, hi)         # discharge / charge envelope
    if abs(setpoint) < park_sp and g < park_g: # 11/11 alone, 15/15 shared
        setpoint = 0

``setpoint`` is the commanded inverter power in the device's own convention:
**positive = discharge**, negative = charge. ``hi`` is the discharge limit
(positive), ``lo`` the charge limit (negative). The per-step arithmetic runs in
IEEE-754 single precision with truncation toward zero, matching the device's
32-bit FPU, so :func:`_f32` rounds after each operation.

Provenance
----------
Read out of the archived Control images (`rweijnen/marstek-firmware-archive
<https://github.com/rweijnen/marstek-firmware-archive>`_ and
`sphings79/marstek-firmware-archiv
<https://github.com/sphings79/marstek-firmware-archiv>`_). Code sites are given
as **file offsets** into the ``.bin``, because the load base is not obvious and
getting it wrong shifts every address by a constant: these are app images that
start with their own vector table but are flashed **above the bootloader**, at
``0x08004800``, not at ``0x08000000``. Two independent checks agree on that
base — the reset vector (``0x08004a71`` in all three, i.e. file+0x270) and a
scan of which base makes the most pointer literals land on the first byte of a
NUL-terminated string (337-344 hits at ``0x08004800``, at most 28 at any other
alignment). Runtime address = file offset + ``0x08004800``.

=======  =====  ==========  ========  =======  ==============
device   image  integrator  gate      split    setpoint (RAM)
=======  =====  ==========  ========  =======  ==============
VNSD-0   v150   +0x2c8f4    +0x266a0  +0x42b8  0x20000298
VNSE3-0  v150   +0x29efc    +0x23d2c  +0x4218  0x20000290
VNSA-0   v149   +0x2b4c2    +0x2520c  +0x3ec8  0x20000290
=======  =====  ==========  ========  =======  ==============

The integrator offsets point at the branch pair that selects the law
(``cmp out,#0`` / ``cmp g,#0xa``) rather than at a function entry, because on
VNSA-0 the law is inlined into a larger routine instead of standing alone. The
RAM addresses come from pointer literals, so they are absolute and unaffected
by the base.

The VNSD-0 and VNSE3-0 routines are the same code instruction for instruction —
integrator, gate and share split alike. **VNSA-0 runs the same gate**: its
prologue matches byte for byte, and disassembling all three side by side, the
routine is identical for its whole length (the only differences are branch
targets and one register allocation). The law is also stable across versions:
it is identical in VNSE3-0 v144/v148/v1476/v150 and VNSD-0 v147/v149/v1492/v150.

Two properties were verified rather than assumed, because they are what
separates these units from a B2500 (:mod:`b2500_steering`):

* **The integrator adds to its own stored setpoint, never to measured output.**
  Every other write to that RAM word is a reset to zero (lost CT, mode change,
  invalid reading). So a command too small for the inverter to execute still
  accumulates — a repeated 30 W reading walks the setpoint 25, 50, 75 W until
  the unit starts. A B2500, whose setpoint is ``measured_output + 0.9 * grid``,
  never can.
* **The float gain table is absent.** None of the eleven HMG-50 gain constants
  appears in any Venus image, while all eleven appear in all ten archived
  HMG-50 images.

Scope: this is the **run mode 10** carve-out. Ahead of everything above, the
routine tests a mode byte and, when it reads 10, hands ``(out, g)`` to a
different function and stores that result to an unrelated RAM word
(``0x20000300``), leaving the steering setpoint untouched. Nothing here
describes that path; the model covers the CT-following modes the balancer
drives.

``out`` is a signed 16-bit field of the device status struct (``+0x18``). Its
identity is inferred, not read: the gate's spike filter tests *"the grid jumped
>50 W while this value barely moved"*, which is only meaningful for the unit's
own output — were it the unit's own grid reading, the two conditions would
contradict each other. The HMG-50 gate has the identical shape around a value
documented as ``out``. The inference is also cheap: at the default
``ctrl_ratio`` the branches it selects differ only by the 5 W step bias.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

__all__ = [
    "DEADBAND_HMG50_W",
    "DEADBAND_W",
    "DEFAULT_CTRL_RATIO",
    "IMPORT_THRESHOLD_W",
    "PARK_ALONE_HMG50",
    "PARK_ALONE_VENUS",
    "PARK_SHARED_W",
    "PARK_SINGLE_W",
    "SMALL_IMPORT_HOLD_W",
    "SPIKE_JUMP_W",
    "SPIKE_OWN_DELTA_W",
    "STEP_BIAS_W",
    "VenusIntegerSteeringController",
]

# Final-setpoint park: a unit alone on its bucket parks within ±11 W, one
# sharing the bucket within ±15 W.
PARK_SINGLE_W = 11
PARK_SHARED_W = 15
# The HMG-50 runs the same law with a wider park when it is alone on the bucket
# (``|setpoint| <= 20 and g <= 15``, i.e. strict bounds of 21/16); its shared
# park is 15/15, the same as the Venus. It also snaps to ``0.01`` rather than a
# literal ``0.0`` — its settle interlock refuses to re-arm on exactly zero — but
# this model carries integers, where the two are the same value.
PARK_ALONE_VENUS = (PARK_SINGLE_W, PARK_SINGLE_W)
PARK_ALONE_HMG50 = (21, 16)
# An import below this is treated as the export/hold side of the branch split.
IMPORT_THRESHOLD_W = 11
# Per-step bias subtracted on the integrating branches (nudges toward a hair of
# import rather than exact zero).
STEP_BIAS_W = 5
# Input-conditioning thresholds. The gate's rest deadband is ±10 W on a Venus
# and ±20 W on an HMG-50 (see ``deadband``); the spike thresholds and the
# small-import hold are the same on both.
DEADBAND_W = 10
DEADBAND_HMG50_W = 20
SPIKE_JUMP_W = 50
SPIKE_OWN_DELTA_W = 20
SMALL_IMPORT_HOLD_W = 10
# Loop gain in percent. The device accepts 30..100 and falls back to 100 (unity)
# for anything outside that range.
DEFAULT_CTRL_RATIO = 100
_RATIO_MIN, _RATIO_MAX = 30, 100


def _f32(x: float) -> float:
    """Round *x* to single precision, mirroring the device's 32-bit FPU."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


# Percent -> gain fraction (``ctrl_ratio * 0.01`` in single precision).
_RATIO_SCALE = _f32(0.009999999776482582)


def _share_split(g: float, device_count: int) -> int:
    """Divide the bucket value across the batteries sharing it.

    Signed division truncating toward zero, matching the firmware's ``sdiv``.
    """
    nb = max(1, int(device_count))
    g = int(g)
    if nb > 1:
        g = int(g / nb)
    return g


@dataclass
class VenusIntegerSteeringController:
    """One Venus unit's steering state. Call :meth:`step` per CT response."""

    setpoint: int = 0
    ctrl_ratio: int = DEFAULT_CTRL_RATIO
    # ``(setpoint, grid)`` bounds inside which a unit alone on its bucket parks
    # at zero. The Venus and the HMG-50 differ here and nowhere else.
    park_alone: tuple[int, int] = PARK_ALONE_VENUS
    # The gate's rest deadband. ±10 W on a Venus, ±20 W on an HMG-50.
    deadband: int = DEADBAND_W
    # Gate baselines and the spike filter's one-shot flag.
    prev_g: int = 0
    prev_out: int = 0
    spike_pending: bool = False

    def _gain(self) -> float:
        ratio = int(self.ctrl_ratio)
        if ratio < _RATIO_MIN or ratio > _RATIO_MAX:
            ratio = DEFAULT_CTRL_RATIO
        return _f32(_f32(float(ratio)) * _RATIO_SCALE)

    def step(
        self,
        g: float,
        hi: float,
        lo: float,
        *,
        out: float = 0.0,
        device_count: int = 1,
    ) -> int:
        """Advance one regulation cycle for bucket value *g*; return the setpoint.

        *g* is the selected bucket (this phase, or the combined bucket for a
        phase-D reporter), already net of ``grid_standard``, **positive =
        importing**. *hi* / *lo* are the discharge (positive) / charge (negative)
        limits. *out* is the unit's own measured output power (positive =
        discharging). *device_count* is the ``*_chrg_nb`` count for the bucket:
        it divides *g*, disables the spike filter, and widens the park from
        ±11 W to ±15 W.

        A sample the gate holds leaves the setpoint untouched, exactly as the
        firmware's early return does.
        """
        g_i = _share_split(g, device_count)
        if not self._gate(g_i, int(out), device_count):
            return self.setpoint
        return self.step_raw(g_i, hi, lo, out=out, device_count=device_count)

    def _gate(self, g: int, out: int, device_count: int) -> bool:
        """The firmware's pre-integrator gate; ``True`` ⇒ run the integrator.

        Three holds, in order: a >50 W spike the own output cannot explain, a
        ±10 W deadband that applies only while the unit is at rest, and a hold on
        a small residual import. The spike filter is a **one-shot** — the sample
        after a skipped one is forced through, bypassing the two holds below —
        and it is skipped entirely when several units share the bucket, which is
        also when the baselines stop advancing. The firmware additionally
        requires the run mode to be in 1..6 and not 2; a CT-following unit
        always is, so that test is not modelled.
        """
        if device_count <= 1:
            d_out = abs(out - self.prev_out)
            d_g = abs(g - self.prev_g)
            self.prev_g = g
            self.prev_out = out
            spike = (
                abs(g) > self.deadband
                and d_g > SPIKE_JUMP_W
                and d_out < SPIKE_OWN_DELTA_W
            )
            if spike and not self.spike_pending:
                self.spike_pending = True
                return False
            if self.spike_pending:
                self.spike_pending = False
                return True
        self.spike_pending = False
        if abs(g) < self.deadband and out < 1:
            return False
        return not 0 <= g < SMALL_IMPORT_HOLD_W

    def step_raw(
        self,
        g: float,
        hi: float,
        lo: float,
        *,
        out: float | None = None,
        device_count: int = 1,
    ) -> int:
        """Advance the bare integrator, bypassing the gate and the share split.

        This is the inner law :meth:`step` runs on samples the gate passes.
        *out* selects the per-step branch and defaults to *g*; in a closed loop
        against a real house the two track each other in sign most of the time.
        """
        err = int(g)
        o = err if out is None else int(out)
        sp = int(self.setpoint)
        gain = self._gain()

        if o < 0 or err < IMPORT_THRESHOLD_W:
            if o < 1 and err < -10:
                sp = int(_f32(_f32(float(sp)) + _f32(_f32(float(err)) * gain)))
            elif o * err < 0:
                sp = sp + err
            elif o < 0 and -IMPORT_THRESHOLD_W < err < 0:
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

        if device_count < 2:
            park_sp, park_g = self.park_alone
        else:
            park_sp = park_g = PARK_SHARED_W
        if abs(sp) < park_sp and err < park_g:
            sp = 0

        self.setpoint = sp
        return sp
