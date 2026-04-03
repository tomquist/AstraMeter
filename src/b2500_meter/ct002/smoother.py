"""EMA-based target smoother with deadband and sign-change catchup."""

from __future__ import annotations


class TargetSmoother:
    """Exponential moving average smoother with deadband and step limiting.

    Smooths a noisy input signal using EMA.  Provides faster catchup when
    the sign of the input flips (e.g. grid switches from import to export)
    and respects a deadband around zero.
    """

    def __init__(self, alpha: float, max_step: float = 0, deadband: float = 0) -> None:
        self._alpha = alpha
        self._max_step = max_step
        self._deadband = deadband
        self._value: float | None = None
        self._last_sample: tuple | None = None

    @property
    def value(self) -> float | None:
        """Current smoothed value, or ``None`` if no samples yet."""
        return self._value

    def update(self, raw_total: float, sample_id: tuple) -> float:
        """Smooth *raw_total*.  Deduplicates by *sample_id* so that
        multiple consumers calling with the same meter reading in one
        cycle do not compound the EMA update.

        Returns the current smoothed value.
        """
        if self._value is None:
            self._value = raw_total
            self._last_sample = sample_id
        elif sample_id != self._last_sample:
            self._last_sample = sample_id
            if self._deadband > 0 and abs(raw_total) < self._deadband:
                delta = -self._alpha * self._value
            else:
                catchup_alpha = self._alpha
                if (raw_total > 0) != (self._value > 0):
                    catchup_alpha = min(0.5, self._alpha * 4)
                delta = catchup_alpha * (raw_total - self._value)
            if self._max_step > 0:
                delta = max(-self._max_step, min(self._max_step, delta))
            self._value += delta
        return self._value
