# Powermeter classes
class Powermeter:
    async def get_powermeter_watts(self) -> list[float]:
        raise NotImplementedError()

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
