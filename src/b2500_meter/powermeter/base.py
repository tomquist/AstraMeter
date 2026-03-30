import asyncio


# Powermeter classes
class Powermeter:
    # --- Sync interface (kept for non-migrated powermeter subclasses) ---

    def get_powermeter_watts(self) -> list[float]:
        raise NotImplementedError()

    def wait_for_message(self, timeout=5):
        pass

    # --- Async interface (used by the main application) ---
    # Default implementations wrap the sync methods via asyncio.to_thread().
    # Subclasses migrated to native async override these directly.
    # Once all subclasses are converted, the sync methods and the _async
    # suffix will be removed.

    async def get_powermeter_watts_async(self) -> list[float]:
        return await asyncio.to_thread(self.get_powermeter_watts)

    async def wait_for_message_async(self, timeout=5):
        return await asyncio.to_thread(self.wait_for_message, timeout)

    # --- Lifecycle (no-op by default, override for push-based powermeters) ---

    async def start(self):
        pass

    async def stop(self):
        pass
