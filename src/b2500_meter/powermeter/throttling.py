import asyncio
import threading
import time

from b2500_meter.config.logger import logger

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
        self.last_update_time = 0.0
        self.last_values: list[float] | None = None
        self.lock = threading.Lock()

        # Async path has its own state so the two paths are independent.
        self._async_lock = asyncio.Lock()
        self._async_last_update_time = 0.0
        self._async_last_values: list[float] | None = None

    # --- Sync path (unchanged, for non-migrated callers / tests) ---

    def wait_for_message(self, timeout=5):
        return self.wrapped_powermeter.wait_for_message(timeout)

    def get_powermeter_watts(self) -> list[float]:
        with self.lock:
            current_time = time.time()

            if self.throttle_interval <= 0:
                values = self.wrapped_powermeter.get_powermeter_watts()
                self.last_values = values
                self.last_update_time = current_time
                return values

            time_since_last_update = current_time - self.last_update_time

            if time_since_last_update < self.throttle_interval:
                wait_time = self.throttle_interval - time_since_last_update
                logger.debug(
                    "Throttling: Waiting %.1fs before fetching fresh values...",
                    wait_time,
                )
                time.sleep(wait_time)
                current_time = time.time()

            try:
                values = self.wrapped_powermeter.get_powermeter_watts()
                self.last_values = values
                prev_update_time = self.last_update_time
                self.last_update_time = current_time
                total_interval = current_time - prev_update_time
                logger.debug(
                    "Throttling: Fetched fresh values after %.1fs interval: %s",
                    total_interval,
                    values,
                )
                return values
            except Exception as e:
                if self.last_values is not None:
                    logger.warning("Throttling: Error getting fresh values: %s", e)
                    logger.debug(
                        "Throttling: Using cached values due to error: %s",
                        self.last_values,
                    )
                    return self.last_values
                logger.error("Throttling: Error getting fresh values: %s", e)
                raise

    # --- Async path (used by the main application) ---

    async def wait_for_message_async(self, timeout=5):
        return await self.wrapped_powermeter.wait_for_message_async(timeout)

    async def start(self):
        await self.wrapped_powermeter.start()

    async def stop(self):
        await self.wrapped_powermeter.stop()

    async def get_powermeter_watts_async(self) -> list[float]:
        if self.throttle_interval <= 0:
            async with self._async_lock:
                values = await self.wrapped_powermeter.get_powermeter_watts_async()
                self._async_last_values = values
                self._async_last_update_time = time.time()
                return values

        # Fast path: return cached values if still fresh (no lock needed).
        current_time = time.time()
        if (
            self._async_last_values is not None
            and (current_time - self._async_last_update_time) < self.throttle_interval
        ):
            return self._async_last_values

        # Slow path: acquire lock, wait for throttle window, fetch fresh values.
        async with self._async_lock:
            # Re-check after acquiring lock — another coroutine may have
            # fetched while we waited.
            current_time = time.time()
            time_since_last_update = current_time - self._async_last_update_time
            if (
                self._async_last_values is not None
                and time_since_last_update < self.throttle_interval
            ):
                return self._async_last_values

            if time_since_last_update < self.throttle_interval:
                wait_time = self.throttle_interval - time_since_last_update
                logger.debug(
                    "Throttling: Waiting %.1fs before fetching fresh values...",
                    wait_time,
                )
                await asyncio.sleep(wait_time)
                current_time = time.time()

            try:
                values = await self.wrapped_powermeter.get_powermeter_watts_async()
                self._async_last_values = values
                prev_update_time = self._async_last_update_time
                self._async_last_update_time = current_time
                total_interval = current_time - prev_update_time
                logger.debug(
                    "Throttling: Fetched fresh values after %.1fs interval: %s",
                    total_interval,
                    values,
                )
                return values
            except Exception as e:
                if self._async_last_values is not None:
                    logger.warning("Throttling: Error getting fresh values: %s", e)
                    logger.debug(
                        "Throttling: Using cached values due to error: %s",
                        self._async_last_values,
                    )
                    return self._async_last_values
                logger.error("Throttling: Error getting fresh values: %s", e)
                raise
