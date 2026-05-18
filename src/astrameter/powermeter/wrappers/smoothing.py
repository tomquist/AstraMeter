"""EMA-based smoothing and deadband powermeter wrappers."""

from __future__ import annotations

from astrameter.config.logger import logger
from astrameter.powermeter.base import Powermeter

from .base import PowermeterWrapper


class SmoothedPowermeter(PowermeterWrapper):
    """EMA smoother that filters per-phase power readings.

    Applies an exponential moving average on the *total* power, then
    distributes the smoothed total proportionally across phases so that
    per-phase ratios are preserved.

    Dedup logic prevents multiple consumers polling within a single meter
    cycle from compounding the EMA update: an update is skipped only when
    **both** the per-phase sample identity and the raw total are unchanged.
    """

    def __init__(
        self,
        wrapped_powermeter: Powermeter,
        alpha: float,
        max_step: float = 0,
    ) -> None:
        super().__init__(wrapped_powermeter)
        self._alpha = alpha
        self._max_step = max_step
        self._value: float | None = None
        self._last_sample: tuple[float, ...] | None = None
        self._last_raw_total: float | None = None

    @property
    def smoothed_value(self) -> float | None:
        """Current smoothed value, or ``None`` if no samples yet."""
        return self._value

    def reset(self) -> None:
        super().reset()
        logger.debug("SmoothedPowermeter: reset (previous value=%s)", self._value)
        self._value = None
        self._last_sample = None
        self._last_raw_total = None

    async def get_powermeter_watts(self) -> list[float]:
        raw_values = await self.wrapped_powermeter.get_powermeter_watts()
        raw_total = sum(raw_values)
        sample_id = tuple(raw_values)

        if self._value is None:
            self._value = raw_total
            self._last_sample = sample_id
            self._last_raw_total = raw_total
            logger.debug(
                "SmoothedPowermeter: seed value=%.2f (raw=%.2f)",
                self._value,
                raw_total,
            )
            return self._distribute(raw_values, raw_total)

        if sample_id == self._last_sample and raw_total == self._last_raw_total:
            logger.debug(
                "SmoothedPowermeter: dedup hit (raw=%.2f value=%.2f)",
                raw_total,
                self._value,
            )
            return self._distribute(raw_values, raw_total)

        self._last_sample = sample_id
        self._last_raw_total = raw_total

        catchup_alpha = self._alpha
        if (raw_total > 0) != (self._value > 0):
            catchup_alpha = max(self._alpha, min(0.5, self._alpha * 4))
        delta = catchup_alpha * (raw_total - self._value)

        if self._max_step > 0:
            delta = max(-self._max_step, min(self._max_step, delta))

        prev = self._value
        self._value += delta
        logger.debug(
            "SmoothedPowermeter: update raw=%.2f prev=%.2f delta=%.2f new=%.2f",
            raw_total,
            prev,
            delta,
            self._value,
        )
        return self._distribute(raw_values, raw_total)

    def _distribute(self, raw_values: list[float], raw_total: float) -> list[float]:
        """Distribute the smoothed total proportionally across phases."""
        if raw_total == 0 or self._value is None:
            return list(raw_values)
        ratio = self._value / raw_total
        return [v * ratio for v in raw_values]


class DeadbandPowermeter(PowermeterWrapper):
    """Gate that returns zeros when total power is below the deadband threshold.

    Stateless: the upstream :class:`SmoothedPowermeter` provides EMA
    inertia, so the signal approaches the threshold gradually and the
    entry/exit discontinuity is bounded by the deadband value.
    """

    def __init__(
        self,
        wrapped_powermeter: Powermeter,
        deadband: float,
    ) -> None:
        super().__init__(wrapped_powermeter)
        self._deadband = deadband

    async def get_powermeter_watts(self) -> list[float]:
        values = await self.wrapped_powermeter.get_powermeter_watts()
        if self._deadband > 0 and abs(sum(values)) < self._deadband:
            return [0.0] * len(values)
        return values
