import time
from collections.abc import Awaitable, Callable

from astrameter.powermeter.base import Powermeter

from .base import PowermeterWrapper


class HealthTrackingPowermeter(PowermeterWrapper):
    """Outermost wrapper that records read outcomes for health reporting.

    Wraps a fully-built powermeter (after every processing wrapper) so the
    MQTT Insights health loop can report a per-powermeter "Online" diagnostic
    sensor. For push powermeters the loop reads ``stream_online()`` (passed
    through by :class:`PowermeterWrapper`); for pull powermeters it reuses the
    most recent control-loop read recorded here, avoiding extra I/O while the
    control loop is active.

    Behaviour is otherwise transparent: values pass through unchanged and
    exceptions re-raise, so CT002 ``before_send`` keeps serving cached values
    on error exactly as before.
    """

    def __init__(
        self,
        wrapped_powermeter: Powermeter,
        *,
        name: str = "",
        clock: Callable[[], float] | None = None,
    ) -> None:
        super().__init__(wrapped_powermeter)
        self.name = name
        self._clock = clock or time.monotonic
        self._last_attempt: float | None = None
        self._last_outcome_ok = False
        # Last successful processed read, so the health loop can publish the
        # most recent readings without issuing an extra read.
        self._last_values: list[float] | None = None

    @property
    def last_attempt(self) -> float | None:
        return self._last_attempt

    @property
    def last_outcome_ok(self) -> bool:
        return self._last_outcome_ok

    @property
    def last_values(self) -> list[float] | None:
        return self._last_values

    async def get_powermeter_watts(self) -> list[float]:
        result = await self._tracked(self.wrapped_powermeter.get_powermeter_watts)
        self._last_values = list(result)
        return result

    async def get_powermeter_watts_raw(self) -> list[float]:
        return await self._tracked(self.wrapped_powermeter.get_powermeter_watts_raw)

    async def _tracked(self, fn: Callable[[], Awaitable[list[float]]]) -> list[float]:
        self._last_attempt = self._clock()
        try:
            result = await fn()
        except Exception:
            self._last_outcome_ok = False
            raise
        self._last_outcome_ok = bool(result)
        return result
