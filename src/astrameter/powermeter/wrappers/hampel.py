"""Hampel outlier-rejection powermeter wrapper."""

from __future__ import annotations

import statistics
from collections import deque

from astrameter.config.logger import logger
from astrameter.powermeter.base import Powermeter

from .base import PowermeterWrapper


class HampelPowermeter(PowermeterWrapper):
    """Rolling-median outlier filter for sum-of-phases power readings.

    Maintains a rolling window of the most recent ``window`` totals. When the
    next total lies more than ``n_sigma * 1.4826 * MAD`` away from the window
    median (with a floor of ``min_threshold`` watts to handle the constant-
    signal MAD=0 degenerate case), the sample is treated as an outlier: the
    reported total is replaced by the median and per-phase values are
    redistributed proportionally (equal split when ``|raw_total|`` is near
    zero). The window entry itself is mutated to the median so a single spike
    does not poison future detections — this is the canonical Hampel
    identifier formulation used in control literature.

    Operates on the sum of phases, mirroring :class:`SmoothedPowermeter`.
    A phase-cancelling outlier (e.g. +1000 W on L1 and -1000 W on L2) is
    therefore invisible to this filter; that is acceptable because every
    downstream wrapper (EMA, deadband, PID) also operates on sum-of-phases.
    """

    MAD_SCALE = 1.4826

    def __init__(
        self,
        wrapped_powermeter: Powermeter,
        window: int,
        n_sigma: float = 3.0,
        min_threshold: float = 0.0,
    ) -> None:
        if window < 1:
            raise ValueError(f"Hampel window must be >= 1, got {window}")
        if n_sigma < 0:
            raise ValueError(f"Hampel n_sigma must be >= 0, got {n_sigma}")
        if min_threshold < 0:
            raise ValueError(f"Hampel min_threshold must be >= 0, got {min_threshold}")
        super().__init__(wrapped_powermeter)
        self._window: deque[float] = deque(maxlen=window)
        self._window_size = window
        self._n_sigma = n_sigma
        self._min_threshold = min_threshold

    def reset(self) -> None:
        super().reset()
        logger.debug("HampelPowermeter: reset (window size=%d)", len(self._window))
        self._window.clear()

    async def get_powermeter_watts(self) -> list[float]:
        raw_values = await self.wrapped_powermeter.get_powermeter_watts()
        if not raw_values:
            return []

        raw_total = sum(raw_values)
        self._window.append(raw_total)

        if len(self._window) < self._window_size:
            return list(raw_values)

        median = statistics.median(self._window)
        mad = statistics.median(abs(x - median) for x in self._window)
        threshold = max(self._n_sigma * self.MAD_SCALE * mad, self._min_threshold)

        if threshold <= 0 or abs(raw_total - median) <= threshold:
            return list(raw_values)

        self._window[-1] = median
        logger.debug(
            "HampelPowermeter: outlier rejected raw=%.2f median=%.2f threshold=%.2f",
            raw_total,
            median,
            threshold,
        )

        if abs(raw_total) < 1e-9:
            return [median / len(raw_values)] * len(raw_values)
        ratio = median / raw_total
        return [v * ratio for v in raw_values]
