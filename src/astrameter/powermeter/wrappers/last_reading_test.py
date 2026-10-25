import asyncio

import pytest

from .conftest import FakePowermeter
from .last_reading import LastReadingPowermeter, probe_passive_read


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


class _SlowSource(_CountingSource):
    """A polled source whose reads stay in flight until ``release()``."""

    def __init__(self, values: list[float]) -> None:
        super().__init__(values)
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def get_powermeter_watts(self) -> list[float]:
        self.reads += 1
        await self._gate.wait()
        if self.error is not None:
            raise self.error
        return list(self._values)


class _PerReadSource(FakePowermeter):
    """A polled source whose reads each finish when the test says so."""

    def __init__(self) -> None:
        super().__init__([])
        self.reads = 0
        self._pending: list[asyncio.Future[list[float]]] = []

    def finish(self, index: int, values: list[float]) -> None:
        self._pending[index].set_result(values)

    def fail(self, index: int, error: Exception) -> None:
        self._pending[index].set_exception(error)

    async def get_powermeter_watts(self) -> list[float]:
        self.reads += 1
        read: asyncio.Future[list[float]] = asyncio.get_running_loop().create_future()
        self._pending.append(read)
        return await read


class _PushSource(_CountingSource):
    def stream_online(self) -> bool | None:
        return True

    async def get_passive_watts(self) -> list[float]:
        return await self.get_powermeter_watts()


def _make(
    source: FakePowermeter, max_age: float = 5.0
) -> tuple[LastReadingPowermeter, _FakeClock]:
    clock = _FakeClock()
    return LastReadingPowermeter(source, max_age=max_age, clock=clock), clock


async def test_passive_read_reuses_a_recent_control_read() -> None:
    source = _CountingSource([100.0, 200.0])
    pm, clock = _make(source)

    assert await pm.get_powermeter_watts() == [100.0, 200.0]
    source.set([999.0, 999.0])
    clock.now = 5.0

    assert await pm.get_passive_watts() == [100.0, 200.0]
    assert source.reads == 1


async def test_passive_read_reads_the_source_once_the_reading_is_too_old() -> None:
    source = _CountingSource([100.0])
    pm, clock = _make(source)
    await pm.get_powermeter_watts()

    source.set([150.0])
    clock.now = 5.1
    assert await pm.get_passive_watts() == [150.0]
    assert source.reads == 2

    # That read is remembered, so the next passive read within max_age is free.
    clock.now = 6.0
    assert await pm.get_passive_watts() == [150.0]
    assert source.reads == 2


async def test_passive_read_reads_the_source_when_nothing_has_read_it() -> None:
    source = _CountingSource([42.0])
    pm, _ = _make(source)

    assert await pm.get_passive_watts() == [42.0]
    assert source.reads == 1


async def test_failed_or_empty_reads_are_not_remembered() -> None:
    source = _CountingSource([100.0])
    pm, clock = _make(source)
    await pm.get_powermeter_watts()

    clock.now = 10.0
    source.error = ValueError("offline")
    with pytest.raises(ValueError):
        await pm.get_powermeter_watts()
    # The passive read does not fall back to the old reading: it asks the source.
    with pytest.raises(ValueError):
        await pm.get_passive_watts()

    source.error = None
    source.set([])
    assert await pm.get_powermeter_watts() == []
    source.set([7.0])
    assert await pm.get_passive_watts() == [7.0]


async def test_served_reading_is_a_copy() -> None:
    source = _CountingSource([1.0, 2.0])
    pm, _ = _make(source)
    await pm.get_powermeter_watts()

    served = await pm.get_passive_watts()
    served[0] = 999.0
    assert await pm.get_passive_watts() == [1.0, 2.0]


async def test_push_meter_passive_read_goes_straight_to_its_cache() -> None:
    """A push meter answers without I/O and applies its own staleness rule, so
    the wrapper must not serve a reading the meter itself would now refuse."""
    source = _PushSource([100.0])
    pm, _ = _make(source)
    await pm.get_powermeter_watts()

    source.error = ValueError("stale")
    with pytest.raises(ValueError, match="stale"):
        await pm.get_passive_watts()


async def test_reset_keeps_the_reading() -> None:
    source = _CountingSource([100.0])
    pm, _ = _make(source)
    await pm.get_powermeter_watts()

    pm.reset()
    assert source.reset_count == 1
    assert await pm.get_passive_watts() == [100.0]
    assert source.reads == 1


async def test_passive_read_waits_for_a_control_read_in_flight() -> None:
    source = _SlowSource([100.0])
    pm, _ = _make(source)

    control = asyncio.create_task(pm.get_powermeter_watts())
    await asyncio.sleep(0)
    passive = asyncio.create_task(pm.get_passive_watts())
    await asyncio.sleep(0)
    source.release()

    assert await control == [100.0]
    assert await passive == [100.0]
    assert source.reads == 1


async def test_overlapping_passive_reads_share_one_source_read() -> None:
    source = _SlowSource([42.0])
    pm, _ = _make(source)

    readers = [asyncio.create_task(pm.get_passive_watts()) for _ in range(3)]
    await asyncio.sleep(0)
    source.release()

    assert await asyncio.gather(*readers) == [[42.0], [42.0], [42.0]]
    assert source.reads == 1


async def test_passive_read_reads_itself_when_the_read_it_waited_for_fails() -> None:
    source = _SlowSource([100.0])
    source.error = ValueError("offline")
    pm, _ = _make(source)

    control = asyncio.create_task(pm.get_powermeter_watts())
    await asyncio.sleep(0)
    passive = asyncio.create_task(pm.get_passive_watts())
    await asyncio.sleep(0)
    source.release()

    with pytest.raises(ValueError):
        await control
    # The failure leaves nothing to reuse, so the passive reader asks the
    # source and gets its own answer, never a stale value.
    with pytest.raises(ValueError):
        await passive
    assert source.reads == 2


async def test_passive_read_waits_for_every_control_read_in_flight() -> None:
    """Without a throttle, battery polls overlap. A newer one coming back
    empty must not hide the older one still running from a passive reader."""
    source = _PerReadSource()
    pm, _ = _make(source)

    older = asyncio.create_task(pm.get_powermeter_watts())
    newer = asyncio.create_task(pm.get_powermeter_watts())
    await asyncio.sleep(0)
    passive = asyncio.create_task(pm.get_passive_watts())
    await asyncio.sleep(0)

    source.finish(1, [])
    assert await newer == []
    await asyncio.sleep(0)
    assert not passive.done()

    source.finish(0, [100.0])
    assert await older == [100.0]
    assert await passive == [100.0]
    assert source.reads == 2


async def test_passive_read_does_not_wait_for_reads_started_after_it() -> None:
    """A meter that keeps failing while battery polls overlap must still answer
    a passive reader, with an error, rather than keep it waiting for ever."""
    source = _PerReadSource()
    pm, _ = _make(source)

    first = asyncio.create_task(pm.get_powermeter_watts())
    await asyncio.sleep(0)
    passive = asyncio.create_task(pm.get_passive_watts())
    await asyncio.sleep(0)
    later = asyncio.create_task(pm.get_powermeter_watts())
    await asyncio.sleep(0)

    source.fail(0, OSError("timeout"))
    with pytest.raises(OSError):
        await first
    await asyncio.sleep(0)
    # The passive reader asks the source itself instead of waiting for the
    # battery read that started after it.
    assert source.reads == 3
    source.fail(2, OSError("timeout"))
    with pytest.raises(OSError):
        await passive

    source.finish(1, [100.0])
    assert await later == [100.0]


async def test_probe_reports_only_this_tasks_source_reads() -> None:
    source = _SlowSource([100.0])
    pm, clock = _make(source)

    control = asyncio.create_task(pm.get_powermeter_watts())
    await asyncio.sleep(0)
    with probe_passive_read() as probe:
        passive = asyncio.create_task(pm.get_passive_watts())
        await asyncio.sleep(0)
        source.release()
        assert await passive == [100.0]
    await control
    # Served from the battery read it waited for, not from a read of its own.
    assert probe.reached_source is False

    clock.now = 10.0
    with probe_passive_read() as probe:
        assert await pm.get_passive_watts() == [100.0]
    assert probe.reached_source is True
