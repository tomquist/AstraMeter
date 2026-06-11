"""Marstek B2500 (HMJ) DC-output steering controller.

The B2500 is **DC-coupled** (PV/DC in, DC out to one or two external
microinverters), so it steers its **DC output power** per channel rather than an
AC inverter setpoint. The controller is integer-only and built from a
meter-derived setpoint feeding a per-channel hysteresis regulator — none of the
Venus float gain table, ``sqrt`` step, or spike filter apply. It is documented in
``docs/ct002-ct003-protocol.md`` ("B2500-class (HMJ) DC-output steering").

``cmd`` is an internal command unit, not watts: the output the device drives is
``(cmd - 5) * 10 / 59``, so a ±100 ``cmd`` step moves the output by only ~17 W per
cycle. The loop holds while the measured output power is within a ±10 W deadband
of the setpoint, and otherwise nudges ``cmd`` by ±100 — a plain bounded
integrator toward the setpoint.

SOC and temperature are handled by a *separate* BMS (charge-current derating,
cell-voltage limits) and are **not** part of this steering loop.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["B2500SteeringController"]

DEADBAND_W = 10  # hold while abs(power - setpoint) <= 10 W
CMD_STEP = 100  # internal command step per cycle (~17 W of output)
CMD_FLOOR = 5  # output = (cmd - CMD_FLOOR) * CAL_NUM / CAL_DEN
CAL_NUM, CAL_DEN = 10, 59  # command -> output (watts) calibration
APPROACH_NUM, APPROACH_DEN = 9, 10  # setpoint = grid * 0.9


@dataclass
class B2500SteeringController:
    """One DC output channel's steering state. Call :meth:`step` per poll cycle.

    The B2500 has two independent outputs; use one controller per channel.
    """

    cmd: int = 60  # internal command unit (not watts)

    def output(self) -> int:
        """The DC output power (W) the current command maps to.

        Mirrors the firmware calibration ``(cmd - 5) * 10 / 59`` in 16-bit
        unsigned arithmetic (so the ``cmd < 5`` underflow matches the device; in
        normal operation ``cmd`` stays well above the floor).
        """
        r = ((self.cmd - CMD_FLOOR) & 0xFFFFFFFF) * CAL_NUM & 0xFFFFFFFF
        return (r // CAL_DEN) & 0xFFFF

    def regulate(self, setpoint: int, power: int) -> int:
        """Advance one hysteresis cycle toward *setpoint*; return the new output.

        *power* is the channel's measured output power (W). Holds within the
        ±10 W deadband, else slews ``cmd`` by ±100.
        """
        if power > setpoint + DEADBAND_W:
            self.cmd = (self.cmd - CMD_STEP) & 0xFFFF
        elif power < setpoint - DEADBAND_W:
            self.cmd = (self.cmd + CMD_STEP) & 0xFFFF
        return self.output()

    @staticmethod
    def setpoint_from_grid(grid: int, max_power: int) -> int:
        """Meter-derived output setpoint: 90% of grid, clamped to half the envelope."""
        return min(int(grid) * APPROACH_NUM // APPROACH_DEN, int(max_power) // 2)

    def step(self, grid: int, power: int, max_power: int) -> int:
        """Full per-cycle pass: derive the setpoint from *grid*, then regulate."""
        return self.regulate(self.setpoint_from_grid(grid, max_power), power)
