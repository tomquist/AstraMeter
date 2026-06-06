# Powermeter classes
from collections.abc import Callable


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


class Powermeter:
    # Labels the powermeter's diagnostic device in MQTT Insights. Set by the
    # outermost HealthTrackingPowermeter wrapper to the config section name.
    name: str = ""

    async def get_powermeter_watts(self) -> list[float]:
        raise NotImplementedError()

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

    async def wait_for_message(self, timeout=5):
        pass

    async def wait_for_next_message(self, timeout=5):
        """Block until a *new* measurement arrives (push-based powermeters).

        Unlike ``wait_for_message`` (which returns immediately once data has
        been received *at least once*), this method waits for the *next*
        update, ensuring callers always get fresh data.  Polling-based
        powermeters leave the default no-op.
        """

    # --- Lifecycle (no-op by default, override for push-based powermeters) ---

    async def start(self):
        pass

    async def stop(self):
        pass

    def reset(self):
        pass
