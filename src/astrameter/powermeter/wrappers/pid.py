import asyncio
import time

from astrameter.powermeter.base import Powermeter

from .base import PowermeterWrapper


class PidPowermeter(PowermeterWrapper):
    """PID controller steering the reported power toward zero.

    Runs on the sum of all phases with ``error = -measurement``, so a grid
    import produces a negative output that motivates the storage device to
    cover it. The output is split equally across phases and either added to
    the raw reading (``mode="bias"``) or reported in its place
    (``mode="replace"``). In bias mode the PID acts on top of the device's own
    control loop, which is stable for ``0 < Kp < 1``.

    The integral term is clamped so the total output stays within
    ``[-output_max, +output_max]`` and stops accumulating while saturated.
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
    ) -> None:
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

        self._integral: float = 0.0
        self._prev_error: float | None = None
        self._prev_time: float | None = None
        self._lock = asyncio.Lock()

    async def get_powermeter_watts(self) -> list[float]:
        async with self._lock:
            raw_values = await self.wrapped_powermeter.get_powermeter_watts()
            current_time = time.monotonic()

            total_power = sum(raw_values)
            error = -total_power
            if self._prev_time is None:
                self._prev_error = error
                self._prev_time = current_time
                dt = 0.0
            else:
                dt = current_time - self._prev_time
                if dt <= 0:
                    dt = 0.0

            p_term = self.kp * error

            if dt > 0 and self._prev_error is not None:
                d_term = self.kd * (error - self._prev_error) / dt
            else:
                d_term = 0.0

            if dt > 0:
                tentative_integral = self._integral + error * dt
                tentative_output = p_term + self.ki * tentative_integral + d_term
                # Anti-windup: only accept the new integral if the output is
                # not saturated, or if the integral is unwinding toward zero.
                if abs(tentative_output) <= self.output_max or (
                    self._integral != 0 and self._integral * error < 0
                ):
                    self._integral = tentative_integral
            i_term = self.ki * self._integral

            self._prev_error = error
            self._prev_time = current_time

        pid_output = p_term + i_term + d_term
        pid_output = max(-self.output_max, min(self.output_max, pid_output))

        num_phases = len(raw_values)
        per_phase = pid_output / num_phases if num_phases > 0 else 0.0

        if self.mode == "bias":
            return [value + per_phase for value in raw_values]
        else:
            return [per_phase] * num_phases
