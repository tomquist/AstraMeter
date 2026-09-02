"""The arithmetic and gate thresholds the Marstek steering laws share.

The HMG-50's gain-scheduled ramp (:mod:`astrameter.simulator.firmware_steering`)
and the Venus integer integrator
(:mod:`astrameter.simulator.venus_integer_steering`) are different control laws,
but they run on the same 32-bit FPU, split a shared bucket the same way, and
their input-conditioning gates use the same spike and small-import thresholds.
Only the gates' rest deadband differs (±20 W on an HMG-50, ±10 W on a Venus), so
that stays with each law.
"""

from __future__ import annotations

import struct

__all__ = [
    "SMALL_IMPORT_HOLD_W",
    "SPIKE_JUMP_W",
    "SPIKE_OWN_DELTA_W",
]

# Input-conditioning thresholds both gates apply before their control law: a
# grid jump this large while the unit's own output barely moved is a spike to
# skip, and a small import is held rather than acted on.
SPIKE_JUMP_W = 50
SPIKE_OWN_DELTA_W = 20
SMALL_IMPORT_HOLD_W = 10


def _f32(x: float) -> float:
    """Round *x* to single precision, mirroring the device's 32-bit FPU."""
    return struct.unpack("<f", struct.pack("<f", x))[0]


def _share_split(g: float, device_count: int) -> int:
    """Divide the bucket value across the batteries sharing it.

    Signed division truncating toward zero, matching the firmware's ``sdiv``.
    """
    nb = max(1, int(device_count))
    g = int(g)
    if nb > 1:
        g = int(g / nb)
    return g
