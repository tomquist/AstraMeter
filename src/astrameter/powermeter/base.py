# Powermeter classes
class Powermeter:
    async def get_powermeter_watts(self) -> list[float]:
        raise NotImplementedError()

    async def wait_for_message(self, timeout=5):
        pass

    # --- Lifecycle (no-op by default, override for push-based powermeters) ---

    async def start(self):
        pass

    async def stop(self):
        pass
