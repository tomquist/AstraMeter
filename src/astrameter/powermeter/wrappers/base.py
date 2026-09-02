from astrameter.powermeter.base import Powermeter


class PowermeterWrapper(Powermeter):
    """Base for wrappers that decorate another Powermeter."""

    def __init__(self, wrapped_powermeter: Powermeter) -> None:
        self.wrapped_powermeter = wrapped_powermeter

    async def get_powermeter_watts_raw(self) -> list[float]:
        return await self.wrapped_powermeter.get_powermeter_watts_raw()

    def stream_online(self) -> bool | None:
        return self.wrapped_powermeter.stream_online()

    async def wait_for_message(self, timeout: float = 5) -> None:
        await self.wrapped_powermeter.wait_for_message(timeout)

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        await self.wrapped_powermeter.wait_for_next_message(timeout)

    async def start(self) -> None:
        await self.wrapped_powermeter.start()

    async def stop(self) -> None:
        await self.wrapped_powermeter.stop()

    def reset(self) -> None:
        self.wrapped_powermeter.reset()
