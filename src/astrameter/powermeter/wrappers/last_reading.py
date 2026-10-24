import asyncio
import contextlib
import time
from collections.abc import Callable, Iterator
from contextvars import ContextVar

from astrameter.powermeter.base import Powermeter, stream_fresh

from .base import PowermeterWrapper

#: How old the control loop's last reading may be before a passive reader
#: (the Marstek app responder, cloud reporting) reads the source itself.
#: Batteries poll about once a second, so while any battery reads this meter
#: a passive reader never adds a request of its own.
PASSIVE_READING_MAX_AGE_S = 5.0


class PassiveReadProbe:
    """Says whether one passive read was answered by the source."""

    reached_source = False


_probe: ContextVar[PassiveReadProbe | None] = ContextVar(
    "passive_read_probe", default=None
)


@contextlib.contextmanager
def probe_passive_read() -> Iterator[PassiveReadProbe]:
    """Report whether the passive reads made inside reached the source.

    The health wrapper uses it to tell a reading the source just gave from one
    served from the cache, which proves nothing about the meter now. It goes
    by the calling task, so reads other tasks make meanwhile, the batteries'
    or other passive readers', do not count.
    """
    probe = PassiveReadProbe()
    token = _probe.set(probe)
    try:
        yield probe
    finally:
        _probe.reset(token)


class LastReadingPowermeter(PowermeterWrapper):
    """Innermost wrapper that lets passive readers reuse the control loop's read.

    Every real read of the source passes through here and is remembered.
    :meth:`get_passive_watts` — what the diagnostic readers call —
    serves that reading while it is at most ``max_age`` seconds old, and only
    reads the source itself when nothing has read it for longer, so those
    readers add no load on a meter the batteries are already polling.

    A push meter already answers from the reading it was sent, with no I/O, so
    its passive reads go straight through and keep the meter's own staleness
    rule.

    A passive read that finds no fresh reading first waits for the reads of
    the source already in flight when it arrived, a battery's or another
    passive reader's, and reads the source itself only if they left nothing
    fresh behind. Reads that start while it waits are not waited for, so a
    meter that keeps failing under continuous battery polls answers the
    passive reader after the reads it found running and one of its own,
    rather than keeping it waiting for as long as the polls overlap.

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
        # Every read of the source still running: battery polls can overlap
        # when no throttle sits above. Each resolves to None however its read
        # ends, so a waiting passive reader looks at the cache afterwards
        # rather than at the read's outcome.
        self._in_flight: set[asyncio.Future[None]] = set()

    async def get_powermeter_watts(self) -> list[float]:
        done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._in_flight.add(done)
        try:
            values = await self.wrapped_powermeter.get_powermeter_watts()
            if values:
                self._values = list(values)
                self._read_at = self._clock()
            return values
        finally:
            done.set_result(None)
            self._in_flight.discard(done)

    async def get_passive_watts(self) -> list[float]:
        if self.wrapped_powermeter.stream_online() is not None:
            return await self.wrapped_powermeter.get_passive_watts()
        pending = set(self._in_flight)
        while True:
            fresh = self._fresh_values()
            if fresh is not None:
                return fresh
            pending = {read for read in pending if not read.done()}
            if not pending:
                probe = _probe.get()
                if probe is not None:
                    probe.reached_source = True
                return await self.get_powermeter_watts()
            # asyncio.wait never cancels what it waits on, even if this
            # reader is cancelled, so the reads it waits for run to the end.
            await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)

    def _fresh_values(self) -> list[float] | None:
        if self._values is not None and stream_fresh(
            self._read_at, self.max_age, self._clock
        ):
            return list(self._values)
        return None
