# The lifecycle and wait hooks are deliberate no-op defaults, not forgotten
# abstract methods.
# ruff: noqa: B027
import asyncio
from abc import ABC, abstractmethod
from collections.abc import Callable


def as_list(value: str | list[str]) -> list[str]:
    """Normalise a config value that may be a single entry or a list of them."""
    return [value] if isinstance(value, str) else list(value)


def stream_fresh(
    last_monotonic: float | None,
    max_age: float,
    clock: Callable[[], float],
) -> bool:
    """Freshness check shared by cadence-based push powermeters.

    Returns ``False`` if nothing has been received yet; ``True`` when
    ``max_age <= 0`` (freshness disabled); otherwise ``True`` only while the
    last message is no older than ``max_age`` seconds.
    """
    if last_monotonic is None:
        return False
    if max_age <= 0:
        return True
    return (clock() - last_monotonic) <= max_age


class Powermeter(ABC):
    # Labels the powermeter's diagnostic device in MQTT Insights. Set by the
    # outermost HealthTrackingPowermeter wrapper to the config section name.
    name: str = ""

    @abstractmethod
    async def get_powermeter_watts(self) -> list[float]: ...

    async def get_powermeter_watts_raw(self) -> list[float]:
        """Per-phase watts before section/global processing wrappers.

        Used when a consumer (e.g. Marstek MQTT display) should match the physical
        meter while control still uses :meth:`get_powermeter_watts`. Defaults to
        the same values as :meth:`get_powermeter_watts` for sources with no inner
        pipeline.
        """
        return await self.get_powermeter_watts()

    def stream_online(self) -> bool | None:
        """Health hook for the MQTT Insights "Online" diagnostic sensor.

        ``None`` (the default) means "don't know" — used by pull/polling
        powermeters; the health loop falls back to reusing the control loop's
        last read or, when idle, a single bounded probe. Push powermeters
        override this to report their own connection/validity state with no
        I/O.
        """
        return None

    async def wait_for_message(self, timeout: float = 5) -> None:
        pass

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        """Block until a *new* measurement arrives (push-based powermeters).

        Unlike ``wait_for_message`` (which returns immediately once data has
        been received *at least once*), this method waits for the *next*
        update, ensuring callers always get fresh data.  Polling-based
        powermeters leave the default no-op.
        """

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    def reset(self) -> None:
        pass


class PushPowermeter(Powermeter):
    """Source that receives measurements instead of polling for them.

    Subclasses set ``_message_event`` on every measurement they receive, so
    ``wait_for_message`` returns once one has arrived and
    ``wait_for_next_message`` (which clears the event first) blocks until the
    next.
    """

    _TIMEOUT_MESSAGE = "Timeout waiting for message"

    def __init__(self) -> None:
        self._message_event = asyncio.Event()

    async def _wait(self, event: asyncio.Event, timeout: float) -> None:
        # Normalize to the builtin TimeoutError so callers get the same exception
        # on every Python version (asyncio.TimeoutError is only an alias for it
        # from 3.11 on).
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(self._TIMEOUT_MESSAGE) from None

    async def wait_for_message(self, timeout: float = 5) -> None:
        await self._wait(self._message_event, timeout)

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        self._message_event.clear()
        await self._wait(self._message_event, timeout)
