import time
from collections.abc import Callable

from astrameter.powermeter.base import Powermeter, stream_fresh

from .base import PowermeterWrapper

#: How old the control loop's last reading may be before a passive reader
#: (the Marstek app responder, cloud reporting) reads the source itself.
#: Batteries poll about once a second, so while any battery reads this meter
#: a passive reader never adds a request of its own.
PASSIVE_READING_MAX_AGE_S = 5.0


class LastReadingPowermeter(PowermeterWrapper):
    """Innermost wrapper that lets passive readers reuse the control loop's read.

    Every real read of the source passes through here and is remembered.
    :meth:`get_powermeter_watts_raw` — what the diagnostic readers call —
    serves that reading while it is at most ``max_age`` seconds old, and only
    reads the source itself when nothing has read it for longer, so those
    readers add no load on a meter the batteries are already polling.

    A push meter already answers from the reading it was sent, with no I/O, so
    its raw reads go straight through and keep the meter's own staleness rule.

    ``reset()`` passes through without forgetting the reading: it clears the
    filters' rolling state, and this is a fact about the meter.
    """

    def __init__(
        self,
        wrapped_powermeter: Powermeter,
        *,
        max_age: float = PASSIVE_READING_MAX_AGE_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(wrapped_powermeter)
        self.max_age = max_age
        self._clock = clock or time.monotonic
        self._values: list[float] | None = None
        self._read_at: float | None = None

    async def get_powermeter_watts(self) -> list[float]:
        values = await self.wrapped_powermeter.get_powermeter_watts()
        if values:
            self._values = list(values)
            self._read_at = self._clock()
        return values

    async def get_powermeter_watts_raw(self) -> list[float]:
        if self.wrapped_powermeter.stream_online() is not None:
            return await self.wrapped_powermeter.get_powermeter_watts_raw()
        if self._values is not None and stream_fresh(
            self._read_at, self.max_age, self._clock
        ):
            return list(self._values)
        return await self.get_powermeter_watts()
