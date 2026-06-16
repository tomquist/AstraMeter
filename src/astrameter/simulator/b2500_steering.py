"""Marstek B2500 (HMJ) DC-output steering controller.

The B2500 is **DC-coupled** (PV/DC in, DC out to one or two external
microinverters), so it steers its **DC output power** per channel rather than an
AC inverter setpoint. The controller is integer-only and built from a
meter-derived setpoint feeding a per-channel hysteresis regulator — none of the
Venus float gain table, ``sqrt`` step, or spike filter apply. It is documented in
``docs/ct002-ct003-protocol.md`` ("B2500-class (HMJ) DC-output steering").

``cmd`` is an internal command unit, not watts: the output the device drives is
``(cmd - 5) * 10 / 59``, so a ±100 ``cmd`` step moves the output by only ~17 W per
cycle. The loop holds while the measured output is within a ±10 W deadband of the
setpoint, otherwise nudges ``cmd`` by ±100 — a bounded integrator.

The setpoint is **incremental** (``setpoint = output + 0.9 * grid``), so the loop
integrates the grid toward zero (fixed point ``output = load``); a proportional
``0.9 * grid`` would droop and never null it. The constants are firmware-extracted.

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
APPROACH_NUM, APPROACH_DEN = 9, 10  # correct 90% of the residual grid per cycle


@dataclass
class B2500SteeringController:
    """One DC output channel's steering state. Call :meth:`step` per poll cycle.

    The B2500 has two independent outputs; use one controller per channel.
    """

    cmd: int = 60  # internal command unit (not watts)
    # Output ceiling (W). The command is clamped so its output never runs past
    # this — anti-windup. On real hardware the measured-output feedback
    # saturates at the inverter limit, so the command can't wind up; a
    # watt-domain model needs the clamp explicitly, or the integrator runs away
    # whenever the physical output is capped (and recovers only ~17 W/cycle).
    max_output: int = 2500

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
        ±10 W deadband, else slews ``cmd`` by ±100, clamped so the output stays
        within ``[0, max_output]`` (anti-windup).
        """
        if power > setpoint + DEADBAND_W:
            self.cmd = (self.cmd - CMD_STEP) & 0xFFFF
        elif power < setpoint - DEADBAND_W:
            self.cmd = (self.cmd + CMD_STEP) & 0xFFFF
        cmd_ceiling = CMD_FLOOR + self.max_output * CAL_DEN // CAL_NUM
        if self.cmd > cmd_ceiling:
            self.cmd = cmd_ceiling
        return self.output()

    def step(self, grid: int, power: int, max_power: int) -> int:
        """Full per-cycle pass: form the incremental setpoint, then regulate.

        *grid* is the residual grid power (positive = import), *power* the
        channel's measured output, *max_power* the output envelope. The setpoint
        is ``power + 0.9 * grid`` clamped to ``[0, max_power]`` — incremental, so
        a sustained import winds the output up until the grid is nulled (rather
        than parking at 90% of the residual). The B2500 has no AC input, so the
        setpoint never goes negative: a surplus winds the output down to idle.
        """
        setpoint = power + int(grid) * APPROACH_NUM // APPROACH_DEN
        setpoint = max(0, min(setpoint, int(max_power)))
        return self.regulate(setpoint, power)
