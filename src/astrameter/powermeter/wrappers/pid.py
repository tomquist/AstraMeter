import asyncio
import time

from astrameter.powermeter.base import Powermeter

from .base import PowermeterWrapper


class PidPowermeter(PowermeterWrapper):
    """
    A wrapper around a powermeter that applies a PID (Proportional-Integral-
    Derivative) controller to steer the reported power toward zero (grid balance).

    The PID controller uses the raw power-meter reading as its *process
    variable* and computes an adjustment that is either **added** to the raw
    reading (``mode="bias"``) or **used in place of** the raw reading
    (``mode="replace"``).

    Positive PID output motivates the storage device to increase feed-in power;
    negative output motivates it to decrease feed-in power.

    **Gain sensitivity:** in ``mode="bias"`` the PID and the storage device's own
    closed-loop controller act *together*.  The effective closed-loop gain is
    ``(1 - Kp) * Kb``, where ``Kb`` is the device's internal gain.
    The system is stable for ``0 < Kp < 1``.  Use ``Kp = 0.5`` as the
    recommended starting value.

    **Anti-windup** is built in: the integral term is clamped so that the
    total PID output never exceeds ``[-output_max, +output_max]``, and
    integration is paused while the output is saturated.

    Error convention:
        error = -measurement
    A positive grid import produces a negative error, causing the PID to
    reduce the reported value and motivate the storage device to cover the import.

    To maintain a small import safety buffer (prevent export), set a small
    negative ``POWER_OFFSET`` (e.g. ``POWER_OFFSET = -20``) in the filter
    chain *before* the PID.

    The controller runs on the **sum** of all phases (total grid power)
    and distributes its output equally across phases.

    Config parameters:
        PID_KP          Proportional gain (default 0 → PID disabled)
        PID_KI          Integral gain (default 0)
        PID_KD          Derivative gain (default 0)
        PID_OUTPUT_MAX  Output clamp magnitude in watts (default 800)
        PID_MODE        "bias" or "replace" (default "bias")
    """

    VALID_MODES = ("bias", "replace")

    def __init__(
        self,
        wrapped_powermeter: Powermeter,
        kp: float = 0.0,
        ki: float = 0.0,
        kd: float = 0.0,
        output_max: float = 800.0,
        mode: str = "bias",
    ):
        """
        Initialise the PID powermeter wrapper.

        Args:
            wrapped_powermeter: The actual powermeter instance to wrap.
            kp:  Proportional gain.
            ki:  Integral gain.
            kd:  Derivative gain.
            output_max:  Maximum absolute PID output in watts.  Must be > 0.
            mode:  ``"bias"``  — add PID output to raw reading, or
                   ``"replace"`` — use PID output as the reported value.
        """
        if output_max <= 0:
            raise ValueError(f"PID output_max must be positive, got {output_max}")
        mode = mode.lower()
        if mode not in self.VALID_MODES:
            raise ValueError(
                f"PID mode must be one of {self.VALID_MODES}, got '{mode}'"
            )

        super().__init__(wrapped_powermeter)
        self.kp = kp
        self.ki = ki
        self.kd = kd
        self.output_max = output_max
        self.mode = mode

        # PID state
        self._integral: float = 0.0
        self._prev_error: float | None = None
        self._prev_time: float | None = None
        self._lock = asyncio.Lock()

    async def get_powermeter_watts(self) -> list[float]:
        """Return PID-adjusted power readings for each phase."""
        async with self._lock:
            raw_values = await self.wrapped_powermeter.get_powermeter_watts()
            current_time = time.monotonic()

            # Compute error on the total power across all phases
            total_power = sum(raw_values)
            error = -total_power
            if self._prev_time is None:
                # First call — initialise state, no derivative yet
                self._prev_error = error
                self._prev_time = current_time
                dt = 0.0
            else:
                dt = current_time - self._prev_time
                if dt <= 0:
                    dt = 0.0

            # --- Proportional ---
            p_term = self.kp * error

            # --- Derivative ---
            if dt > 0 and self._prev_error is not None:
                d_term = self.kd * (error - self._prev_error) / dt
            else:
                d_term = 0.0

            # --- Integral with anti-windup ---
            if dt > 0:
                # Tentatively accumulate
                tentative_integral = self._integral + error * dt
                tentative_output = p_term + self.ki * tentative_integral + d_term
                # Only accept the new integral if output is not saturated,
                # or if the integral is moving toward zero (unwinding).
                if abs(tentative_output) <= self.output_max or (
                    self._integral != 0 and self._integral * error < 0
                ):
                    self._integral = tentative_integral
            i_term = self.ki * self._integral

            self._prev_error = error
            self._prev_time = current_time

        # --- Total output with clamping ---
        pid_output = p_term + i_term + d_term
        pid_output = max(-self.output_max, min(self.output_max, pid_output))

        # --- Apply to readings ---
        num_phases = len(raw_values)
        per_phase = pid_output / num_phases if num_phases > 0 else 0.0

        if self.mode == "bias":
            return [value + per_phase for value in raw_values]
        else:
            # replace mode: distribute PID output equally across phases
            return [per_phase] * num_phases
