"""Marstek B2500 (HMJ/HMA/HMK) DC-output steering controller.

The B2500 is **DC-coupled**: it has no AC inverter of its own and no wire to
tell one what to do. It feeds one or two external microinverters through their
PV inputs, so the only thing it can do is *pretend to be a solar panel* whose
maximum-power point sits at the wattage it wants to deliver, and let the
microinverter's own tracker find it. Everything below follows from that.

There are two loops, at very different rates:

* **Every 500 ms**, a device-wide integrator turns the CT meter reading into a
  total commanded power ``setpoint``::

      setpoint = clamp(setpoint + gain * grid, pmin_eff, pmax_eff)

  ``gain`` is 1, or **2** while neither output is drawing current (and the unit
  is not in single-output mode). Note the lower clamp: the command is not
  merely *ignored* below ``pmin`` — it can never be *formed* below it.
* **Every 2 ms**, the total is split across the channels, each channel solves
  its synthetic panel curve against the microinverter's present operating point
  and writes a current reference. That inner loop is far faster than any
  balancer poll, so this module models it as "the channel reaches its target
  after the firmware's own dwell" rather than instant.

Two gates decide whether the 500 ms integrator advances at all:

* it steps immediately once the measured output is within **±9 W** of the
  command;
* otherwise it waits ``adjust_time`` seconds (default 6), then **re-seeds the
  command from the measured output** before integrating — so a command the
  hardware cannot reach is pulled back to reality rather than winding up.

And one gate can stop it entirely: **while neither channel is producing, the
integrator does not run**. A stopped unit restarts through a separate check
that needs the grid import to reach ``pmin`` before it will command anything —
which is why a small steady import can leave a B2500 sitting at zero
indefinitely, the observable behind issue #600.

SOC and temperature are handled by a *separate* BMS (charge-current derating,
cell-voltage limits) and are **not** part of this steering loop.

Provenance
----------
Read out of ``HMJ-2`` V118 (`tomquist/hm2500
<https://github.com/tomquist/hm2500>`_), which loads at ``0x08000000`` — its
vector table admits no other base. Two independent analyses agree on the law,
the rates and the thresholds; where they differed (the inverter-``id`` bands),
the disassembly was re-read directly.

=================================  ==============  ==========================
what                               address         value
=================================  ==============  ==========================
CT integrator                      ``0x0801a3c4``  ``setpoint += gain*grid``
settle band                        ``0x0800bafc``  ±9 W
re-seed timer (``adjust_time``)    ``0x0800bb4e``  config, default 6 s
"neither channel producing" gate   ``0x0800ba98``  500 mA per channel
total -> per-channel split         ``0x0800d26a``  ``setpoint / 2``
channel dwell after a change       ``0x0800c9fe``  500 ms
panel curve built                  ``0x0800c70a``  MPP = target, at 35.2 V
curve solved against the load      ``0x0800cb02``  every 2 ms
=================================  ==============  ==========================

The channel state machine has five states, and **states 3 and 4 are dead
code**: no instruction in the image ever writes state 3, and the only writer of
state 4 sits inside state 3. State 4 is the ±10 W hold band with a ±100 mA
trim — the loop this module used to be built on, which never runs. The
reachable path is ``0 -> 1 -> 2`` and then state 2 forever, restarting at 0
whenever the target changes.

Not modelled, deliberately: the device also has a second grid loop — a
perturb-and-observe search used when **no** networked meter is configured
(``0x08008a50``). AstraMeter is a networked meter, so the integrator above is
the path that runs. There is also a second per-channel controller
(``0x0800cb88``) selected when the inverter ``id`` falls in ``[1500, 2000)``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "ADJUST_TIME_S",
    "CHANNEL_DWELL_S",
    "MAX_OUTPUT_W",
    "MIN_CHANNEL_OUTPUT_W",
    "MIN_OUTPUT_W",
    "OUTER_LOOP_S",
    "SETTLE_BAND_W",
    "STANDBY_CMD_DUAL_W",
    "STANDBY_CMD_W",
    "STANDBY_EXPORT_W",
    "STANDBY_PASSES",
    "B2500SteeringController",
]

# The 500 ms task that owns the integrator (0x0800b96c, driven from a 1 ms tick).
OUTER_LOOP_S = 0.5
# The integrator steps as soon as the measured output is this close to the
# command (0x0800bafc: ``adds r0,#9; cmp r0,#0x13``).
SETTLE_BAND_W = 9
# ...otherwise it waits this long, re-seeds from the measurement and steps
# anyway. Config key ``adjust_time``; the firmware default is 6 (0x0800c56e).
ADJUST_TIME_S = 6.0
# A channel waits this long after its target changes before commanding anything
# (state 1, 0x0800c9fe).
CHANNEL_DWELL_S = 0.5

# Config key ``p``: the unit's maximum output. Written by the app and
# **hard-capped at 800 W by the firmware itself** (0x0800c534, 0x08015fb0).
MAX_OUTPUT_W = 800
# Config key ``pmin``: the unit's minimum output.
#
# **This is a property of the paired inverter, not of the battery.** The
# firmware prints it under its own name (``"id=%d,s=%d,c=%d,i=%d,p=%d,pmin=%d,
# adjust_time=%d"``) and keeps it in a CRC-checked config block. Its factory
# default is 80 W (0x0800c568), but the moment the app configures which
# inverter is attached, the firmware **derives pmin from that inverter's id**:
# 40 W when the id falls in [5000, 5500), 80 W otherwise (0x0800e80c-0x0800e882).
# So a real unit is one or the other depending on what it is plugged into.
MIN_OUTPUT_W = 80
# Half the unit's minimum: what one of the two outputs carries when the device
# is at ``pmin``. 20 W or 40 W per output, following ``pmin``. Callers express
# the floor per channel, so this is the unit the public API speaks.
#
# There is *also* a ``pmin/2`` clamp on a per-channel target at 0x0801c0da, but
# it sits in an arbitration pass that the CT-driven split at 0x0800d26a
# overwrites every 2 ms, and applying it would raise the 4-8 W standby command
# back above ``pmin``, which is plainly not what the device does. The two
# independent readings of this firmware disagreed about which writer wins, so
# the model keeps only the device-wide ``pmin`` clamp, which both agree on.
MIN_CHANNEL_OUTPUT_W = MIN_OUTPUT_W // 2
# Below this current a channel counts as "not producing" (0x08009110), which is
# what gates the integrator and selects gain 2 over gain 1.
_PRODUCING_MA = 500
# Standby. The integrator's lower clamp means it can never wind *down* past
# ``pmin``; getting a B2500 to actually stop is a separate detector
# (0x08008f3c). It arms only while the command is already sitting at ``pmin``
# and the grid is exporting more than 10 W, and it needs that to hold for
# several consecutive passes; then the command is pinned at 4 W (8 W with both
# outputs running) and the integrator is skipped entirely.
STANDBY_EXPORT_W = -10
STANDBY_PASSES = 5  # u8 @0x20007d29, firmware-forced into 3..10, default 5
STANDBY_CMD_W = 4
STANDBY_CMD_DUAL_W = 8
# Leaving standby (0x08009048) needs the import to reach ``pmin`` on two
# consecutive passes; the command then restarts at ``pmin``.
_EXIT_PASSES = 2


@dataclass
class _Channel:
    """One DC output. Reaches its target after the firmware's own dwell."""

    target: int = 0
    output: int = 0
    dwell: float = 0.0

    def set_target(self, target: int) -> None:
        if target != self.target:
            self.target = target
            self.dwell = CHANNEL_DWELL_S  # state 0 -> 1: rebuild the curve

    def advance(self, dt: float) -> int:
        if self.dwell > 0.0:
            self.dwell = max(0.0, self.dwell - dt)
            return self.output
        # State 2 solves the panel curve against the microinverter's operating
        # point every 2 ms, with no rate limit, so at any balancer-visible
        # timescale the channel is simply at its target.
        self.output = self.target
        return self.output


@dataclass
class B2500SteeringController:
    """One B2500's DC-output steering. Call :meth:`step` per simulator tick.

    Unlike the previous model this is a **device-level** controller: the
    integrator the firmware runs is device-wide, and the split across the two
    channels happens below it.
    """

    # Config, in the firmware's own names.
    p: int = MAX_OUTPUT_W
    pmin: int = MIN_OUTPUT_W
    adjust_time: float = ADJUST_TIME_S
    single_mode: bool = False

    setpoint: int = 0  # the firmware's total commanded power
    producing: bool = False  # is either channel drawing >= 500 mA?
    _channels: list[_Channel] = field(default_factory=lambda: [_Channel(), _Channel()])
    standby: bool = False  # output detector has parked the command
    _both_producing: bool = False  # both outputs above the producing threshold
    _outer: float = 0.0  # time owed to the 500 ms task
    _since_adjust: float = 0.0
    _export_passes: int = 0
    _import_passes: int = 0

    def __post_init__(self) -> None:
        # The firmware refuses to keep a stored ``p`` above 800 W — its NVM
        # loader reverts to the defaults when it reads one (0x0800c534), and the
        # text setter clamps it (0x08015fb0). So no real unit outputs more than
        # this, however large a pack it is attached to, and a scenario asking
        # for more gets the device's answer rather than the one it asked for.
        self.p = max(0, min(int(self.p), MAX_OUTPUT_W))
        self.pmin = max(0, min(int(self.pmin), self.p))

    @property
    def p_max(self) -> int:
        """Upper clamp on the total command (halved in single-output mode)."""
        return self.p // 2 if self.single_mode else self.p

    @property
    def p_min(self) -> int:
        """Lower clamp on the total command — the command cannot go below this."""
        return self.pmin // 2 if self.single_mode else self.pmin

    def step(self, grid: int, measured: int, dt: float, *, max_power: int) -> int:
        """Advance *dt* seconds; return the total commanded DC output (W).

        *grid* is the residual grid power the CT reports (positive = import),
        *measured* the unit's present total output, *max_power* the discharge
        envelope the plant can actually deliver right now.
        """
        self._outer += dt
        while self._outer >= OUTER_LOOP_S:
            self._outer -= OUTER_LOOP_S
            self._since_adjust += OUTER_LOOP_S
            self._integrate(int(grid), int(measured))
        return self._drive(dt, int(max_power))

    def seed(self, power: int) -> None:
        """Start already delivering *power* watts, as a mid-flight scenario does.

        Sets the command, the channel state and the producing flags together.
        Setting them piecemeal leaves the first pass reading a stale gain: the
        channels look idle while ``producing`` says otherwise, which selects
        the gain-2 path against outputs that are conceptually already live.
        """
        power = max(0, int(power))
        self.standby = False
        self.setpoint = max(self.p_min, power) if power else 0
        if self.single_mode:
            targets = [min(self.setpoint, self.p // 2), 0]
        else:
            half = min(self.setpoint // 2, self.p // 2)
            targets = [half, half]
        for ch, t in zip(self._channels, targets, strict=True):
            ch.target = t
            ch.output = t
            ch.dwell = 0.0
        self._refresh_producing()

    def _refresh_producing(self) -> None:
        """Recompute the producing flags from the channels' present state."""
        # "Producing" is the firmware's 500 mA-per-channel test on *measured*
        # current. Current follows a new command within milliseconds, so a
        # channel that has just been given a non-zero target counts as
        # producing even though this model's 500 ms dwell has not released its
        # output yet — otherwise every target change would read as a stop and
        # send the unit through the standby-exit path.
        live = [ch.output > 0 or ch.target > 0 for ch in self._channels]
        self.producing = any(live)
        self._both_producing = all(live)

    def _integrate(self, grid: int, measured: int) -> None:
        """One pass of the 500 ms task (0x0800b96c -> 0x0801a3c4)."""
        if self.standby or not self.producing:
            self._check_exit(grid)
            return
        if self._check_enter(grid):
            return
        if abs(measured - self.setpoint) <= SETTLE_BAND_W:
            self._since_adjust = 0.0
        elif self._since_adjust >= self.adjust_time:
            # Cannot settle: pull the command back to what the hardware is
            # actually doing before stepping again, so a command the hardware
            # cannot reach does not wind up.
            self.setpoint = measured
            self._since_adjust = 0.0
        else:
            return
        # Gain 2 whenever *either* output is below the producing threshold and
        # the unit is not in single-output mode (0x0801a3f4-0x0801a422: the
        # branch falls through to the doubling path when ch0 is below it, and
        # again when ch0 is above but ch1 is below). That is a weaker condition
        # than the gate above, which returns only when *neither* output is
        # producing — so on the device, exactly one output running is the
        # gain-2 state.
        #
        # In *this model* that state does not arise: the split below gives both
        # outputs the same target, so they are always both live or both idle,
        # and single-output mode forces gain 1 regardless. The condition is
        # written to match the firmware rather than to be exercised, and only
        # the unit test drives it. Real asymmetry between the two channels
        # comes from hardware and measurement differences this model does not
        # carry, so inventing it here would be speculation, not fidelity.
        gain = 1 if (self.single_mode or self._both_producing) else 2
        sp = self.setpoint + gain * grid
        self.setpoint = max(self.p_min, min(sp, self.p_max))

    def _check_enter(self, grid: int) -> bool:
        """Standby entry: only from ``pmin``, only on a sustained export."""
        if self.setpoint <= self.p_min and grid < STANDBY_EXPORT_W:
            self._export_passes += 1
            if self._export_passes >= STANDBY_PASSES:
                self.standby = True
                self._export_passes = 0
                self.setpoint = (
                    STANDBY_CMD_W if self.single_mode else STANDBY_CMD_DUAL_W
                )
                return True
        else:
            self._export_passes = 0
        return False

    def _check_exit(self, grid: int) -> None:
        """Standby exit: the import has to reach ``pmin`` two passes running."""
        if grid >= self.p_min:
            self._import_passes += 1
            if self._import_passes >= _EXIT_PASSES:
                self.standby = False
                self.producing = True
                self._import_passes = 0
                self.setpoint = self.p_min
        else:
            self._import_passes = 0

    def _drive(self, dt: float, max_power: int) -> int:
        """Split the total across the channels and advance them."""
        ceiling = self.p // 2
        if self.single_mode:
            targets = [min(self.setpoint, ceiling), 0]
        else:
            half = self.setpoint // 2
            targets = [min(half, ceiling), min(half, ceiling)]
        for ch, t in zip(self._channels, targets, strict=True):
            ch.set_target(t)
        out = sum(ch.advance(dt) for ch in self._channels)
        out = min(out, max(0, max_power))
        self._refresh_producing()
        return out
