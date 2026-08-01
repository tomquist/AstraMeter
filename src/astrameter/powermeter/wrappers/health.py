import dataclasses
import time
from collections.abc import Awaitable, Callable

from astrameter.powermeter.base import Powermeter

from .base import PowermeterWrapper


@dataclasses.dataclass(frozen=True, slots=True)
class PowermeterHealth:
    """Immutable health/identity view of one configured powermeter.

    ``online`` is tri-state: ``None`` means "pull meter, unknowable without
    I/O" and must stay distinct from ``False`` (a push meter known to be
    down).  ``last_read_age`` is seconds on the wrapper's own clock, never a
    timestamp — that clock is :func:`time.monotonic` by default and would
    render as 1970 if emitted raw.
    """

    name: str
    kind: str
    pipeline: tuple[str, ...]
    online: bool | None
    last_read_age: float | None
    last_read_ok: bool
    last_values: tuple[float, ...] | None
    last_total: float | None


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

    def status_snapshot(self) -> PowermeterHealth:
        """Immutable health view for the status API.

        Reads recorded state only: it must never call
        ``get_powermeter_watts()``, which runs the PID/Hampel/smoothing
        chain and would inject a phantom sample into the control loop.
        """
        values = self._last_values
        age = None
        if self._last_attempt is not None:
            age = max(0.0, self._clock() - self._last_attempt)
        pipeline: list[str] = []
        node: Powermeter = self.wrapped_powermeter
        while isinstance(node, PowermeterWrapper):
            pipeline.append(type(node).__name__)
            node = node.wrapped_powermeter
        return PowermeterHealth(
            name=self.name,
            kind=type(node).__name__,
            # Innermost first, so the list reads in the order a reading
            # travels outward from the meter.
            pipeline=tuple(reversed(pipeline)),
            online=self.stream_online(),
            last_read_age=age,
            last_read_ok=self._last_outcome_ok,
            last_values=tuple(values) if values is not None else None,
            last_total=sum(values) if values else None,
        )

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
