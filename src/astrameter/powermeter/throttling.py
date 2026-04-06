import asyncio
import time

from astrameter.config.logger import logger

from .base import Powermeter


class ThrottledPowermeter(Powermeter):
    """
    A wrapper around powermeter that throttles the rate of value fetching.

    This helps prevent control instability when using slow data sources by
    enforcing a minimum interval between power meter readings. When called
    too frequently, it waits for the remaining time before fetching fresh
    values, ensuring the storage always receives relatively fresh data at
    a controlled rate.
    """

    def __init__(self, wrapped_powermeter: Powermeter, throttle_interval: float = 0.0):
        self.wrapped_powermeter = wrapped_powermeter
        self.throttle_interval = throttle_interval

        # Coalescing fetch pattern: when a fetch is in flight (including the
        # throttle sleep), concurrent callers await the same future so every
        # consumer gets fresh data without hammering the source.
        self._last_update_time: float | None = None
        self._last_values: list[float] | None = None
        self._pending_fetch: asyncio.Future[list[float]] | None = None

    async def wait_for_message(self, timeout=5):
        return await self.wrapped_powermeter.wait_for_message(timeout)

    async def start(self):
        await self.wrapped_powermeter.start()

    async def stop(self):
        await self.wrapped_powermeter.stop()

    async def get_powermeter_watts(self) -> list[float]:
        if self.throttle_interval <= 0:
            return await self.wrapped_powermeter.get_powermeter_watts()

        # If a fetch (including its throttle sleep) is already in progress,
        # coalesce: wait for the same result so every consumer gets fresh
        # data from the same read.
        if self._pending_fetch is not None:
            return list(await asyncio.shield(self._pending_fetch))

        # We are the leader — other callers that arrive while we sleep or
        # fetch will coalesce behind our future.
        self._pending_fetch = asyncio.get_running_loop().create_future()
        try:
            if self._last_update_time is not None:
                now = time.monotonic()
                remaining = self.throttle_interval - (now - self._last_update_time)
            else:
                remaining = 0.0
            if remaining > 0:
                logger.debug(
                    "Throttling: Waiting %.1fs before fetching fresh values...",
                    remaining,
                )
                await asyncio.sleep(remaining)

            values = await self.wrapped_powermeter.get_powermeter_watts()
            self._last_values = values
            self._last_update_time = time.monotonic()
            logger.debug("Throttling: Fetched fresh values: %s", values)
            self._pending_fetch.set_result(values)
            return list(values)
        except (asyncio.CancelledError, KeyboardInterrupt, SystemExit):
            if not self._pending_fetch.done():
                self._pending_fetch.cancel()
            raise
        except Exception as e:
            # Update timestamp even on failure so we respect the throttle
            # interval before retrying — avoids hammering a failing source.
            self._last_update_time = time.monotonic()
            if self._last_values is not None:
                logger.warning(
                    "Throttling: Error getting fresh values: %s", e, exc_info=True
                )
                logger.debug(
                    "Throttling: Using cached values due to error: %s",
                    self._last_values,
                )
                cached = list(self._last_values)
                if not self._pending_fetch.done():
                    self._pending_fetch.set_result(cached)
                return cached
            if not self._pending_fetch.done():
                self._pending_fetch.set_exception(e)
            raise
        finally:
            self._pending_fetch = None
