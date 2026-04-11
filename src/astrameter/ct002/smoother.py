"""EMA-based target smoother with deadband and sign-change catchup."""

from __future__ import annotations

from astrameter.config.logger import logger


class TargetSmoother:
    """Exponential moving average smoother with deadband and step limiting.

    Smooths a noisy input signal using EMA.  Provides faster catchup when
    the sign of the input flips (e.g. grid switches from import to export)
    and respects a deadband around zero.

    Multiple consumers polling within a single meter cycle must not
    compound the EMA update.  Dedup is keyed on the tuple identity of
    *sample_id* **and** the actual *raw_total*: if ``raw_total`` differs
    between two calls we always accept the new value, even when the
    caller happens to reuse the same ``sample_id``.  This prevents a
    contrived caller from masking fresh readings, and also means that a
    stale push-based powermeter cannot silently freeze the smoother at
    an arbitrary past value — the smoother can still be forcibly
    reseeded via :meth:`reseed`.
    """

    def __init__(
        self,
        alpha: float,
        max_step: float = 0,
        deadband: float = 0,
    ) -> None:
        self._alpha = alpha
        self._max_step = max_step
        self._deadband = deadband
        self._value: float | None = None
        self._last_sample: tuple | None = None
        self._last_raw_total: float | None = None

    @property
    def value(self) -> float | None:
        """Current smoothed value, or ``None`` if no samples yet."""
        return self._value

    def reseed(self) -> None:
        """Clear all smoother state.

        The next :meth:`update` call will seed ``_value`` directly from
        the caller's ``raw_total`` — bypassing the EMA entirely so that
        post-event state catches up in a single step.  Used after
        efficiency-rotation probe handoffs where the balancer needs a
        fresh baseline and any residual EMA state would drag in stale
        pre-handoff readings.
        """
        logger.debug("TargetSmoother: reseed (previous value=%s)", self._value)
        self._value = None
        self._last_sample = None
        self._last_raw_total = None

    def update(self, raw_total: float, sample_id: tuple) -> float:
        """Smooth *raw_total*.

        Dedup fires only when **both** ``sample_id`` and ``raw_total``
        are unchanged; multi-consumer polls within one meter tick will
        therefore still coalesce (they share the same ``sample_id``
        and ``raw_total``), but a fresh ``raw_total`` is never lost to
        a stale dedup key.

        Returns the current smoothed value.
        """
        if self._value is None:
            self._value = raw_total
            self._last_sample = sample_id
            self._last_raw_total = raw_total
            logger.debug(
                "TargetSmoother: seed value=%.2f (raw=%.2f)",
                self._value,
                raw_total,
            )
            return self._value

        # ``raw_total == self._last_raw_total`` uses exact equality on
        # a float, which would normally be fragile.  It's safe here
        # because the production caller is
        # :meth:`astrameter.ct002.ct002.CT002._compute_smooth_target`
        # which computes ``raw_total = sum(parse_int(v, 0) for v in values)``
        # — all ints, so equality is exact.  Tests pass floats but
        # reuse the same value without intervening arithmetic, so
        # equality is also exact there.  If a future caller starts
        # feeding computed floats through this path, swap to
        # ``math.isclose`` and expect to justify the tolerance.
        if sample_id == self._last_sample and raw_total == self._last_raw_total:
            logger.debug(
                "TargetSmoother: dedup hit (raw=%.2f value=%.2f)",
                raw_total,
                self._value,
            )
            return self._value

        self._last_sample = sample_id
        self._last_raw_total = raw_total

        if self._deadband > 0 and abs(raw_total) < self._deadband:
            delta = -self._alpha * self._value
        else:
            catchup_alpha = self._alpha
            if (raw_total > 0) != (self._value > 0):
                catchup_alpha = min(0.5, self._alpha * 4)
            delta = catchup_alpha * (raw_total - self._value)
        if self._max_step > 0:
            delta = max(-self._max_step, min(self._max_step, delta))
        prev = self._value
        self._value += delta
        logger.debug(
            "TargetSmoother: update raw=%.2f prev=%.2f delta=%.2f new=%.2f",
            raw_total,
            prev,
            delta,
            self._value,
        )
        return self._value
