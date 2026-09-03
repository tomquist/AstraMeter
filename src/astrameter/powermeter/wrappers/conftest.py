"""Fixtures shared by the wrapper tests."""

from __future__ import annotations

from ..base import Powermeter


class FakePowermeter(Powermeter):
    """A source the wrapper tests drive directly, one reading at a time.

    Subclasses :class:`Powermeter` so a wrapper that starts expecting more of
    its inner source fails here rather than at the call site of whichever test
    happened to notice.
    """

    def __init__(self, values: list[float] | None = None) -> None:
        self._values: list[float] = [0.0] if values is None else values
        self.started = False
        self.stopped = False
        self.reset_count = 0

    def set(self, values: list[float]) -> None:
        self._values = values

    async def get_powermeter_watts(self) -> list[float]:
        return list(self._values)

    async def get_powermeter_watts_raw(self) -> list[float]:
        return list(self._values)

    async def wait_for_message(self, timeout: float = 5) -> None:
        pass

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def reset(self) -> None:
        self.reset_count += 1
