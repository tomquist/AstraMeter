import pytest

from .conftest import FakePowermeter
from .last_reading import LastReadingPowermeter


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _CountingSource(FakePowermeter):
    """A polled source that counts how often it is actually read."""

    def __init__(self, values: list[float]) -> None:
        super().__init__(values)
        self.reads = 0
        self.error: Exception | None = None

    async def get_powermeter_watts(self) -> list[float]:
        self.reads += 1
        if self.error is not None:
            raise self.error
        return await super().get_powermeter_watts()


class _PushSource(_CountingSource):
    def stream_online(self) -> bool | None:
        return True

    async def get_powermeter_watts_raw(self) -> list[float]:
        return await self.get_powermeter_watts()


def _make(
    source: FakePowermeter, max_age: float = 5.0
) -> tuple[LastReadingPowermeter, _FakeClock]:
    clock = _FakeClock()
    return LastReadingPowermeter(source, max_age=max_age, clock=clock), clock


async def test_raw_read_reuses_a_recent_control_read() -> None:
    source = _CountingSource([100.0, 200.0])
    pm, clock = _make(source)

    assert await pm.get_powermeter_watts() == [100.0, 200.0]
    source.set([999.0, 999.0])
    clock.now = 5.0

    assert await pm.get_powermeter_watts_raw() == [100.0, 200.0]
    assert source.reads == 1


async def test_raw_read_reads_the_source_once_the_reading_is_too_old() -> None:
    source = _CountingSource([100.0])
    pm, clock = _make(source)
    await pm.get_powermeter_watts()

    source.set([150.0])
    clock.now = 5.1
    assert await pm.get_powermeter_watts_raw() == [150.0]
    assert source.reads == 2

    # That read is remembered, so the next passive read within max_age is free.
    clock.now = 6.0
    assert await pm.get_powermeter_watts_raw() == [150.0]
    assert source.reads == 2


async def test_raw_read_reads_the_source_when_nothing_has_read_it() -> None:
    source = _CountingSource([42.0])
    pm, _ = _make(source)

    assert await pm.get_powermeter_watts_raw() == [42.0]
    assert source.reads == 1


async def test_failed_or_empty_reads_are_not_remembered() -> None:
    source = _CountingSource([100.0])
    pm, clock = _make(source)
    await pm.get_powermeter_watts()

    clock.now = 10.0
    source.error = ValueError("offline")
    with pytest.raises(ValueError):
        await pm.get_powermeter_watts()
    # The raw read does not fall back to the old reading: it asks the source.
    with pytest.raises(ValueError):
        await pm.get_powermeter_watts_raw()

    source.error = None
    source.set([])
    assert await pm.get_powermeter_watts() == []
    source.set([7.0])
    assert await pm.get_powermeter_watts_raw() == [7.0]


async def test_served_reading_is_a_copy() -> None:
    source = _CountingSource([1.0, 2.0])
    pm, _ = _make(source)
    await pm.get_powermeter_watts()

    served = await pm.get_powermeter_watts_raw()
    served[0] = 999.0
    assert await pm.get_powermeter_watts_raw() == [1.0, 2.0]


async def test_push_meter_raw_read_goes_straight_to_its_cache() -> None:
    """A push meter answers without I/O and applies its own staleness rule, so
    the wrapper must not serve a reading the meter itself would now refuse."""
    source = _PushSource([100.0])
    pm, _ = _make(source)
    await pm.get_powermeter_watts()

    source.error = ValueError("stale")
    with pytest.raises(ValueError, match="stale"):
        await pm.get_powermeter_watts_raw()


async def test_reset_keeps_the_reading() -> None:
    source = _CountingSource([100.0])
    pm, _ = _make(source)
    await pm.get_powermeter_watts()

    pm.reset()
    assert source.reset_count == 1
    assert await pm.get_powermeter_watts_raw() == [100.0]
    assert source.reads == 1
