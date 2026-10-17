import asyncio
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from astrameter.powermeter.base import Powermeter

from .conftest import FakePowermeter
from .health import HealthTrackingPowermeter
from .last_reading import LastReadingPowermeter
from .throttling import ThrottledPowermeter


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make(wrapped: Powermeter, **kwargs: Any) -> HealthTrackingPowermeter:
    return HealthTrackingPowermeter(wrapped, **kwargs)


async def test_passes_values_through_and_records_success() -> None:
    clock = _FakeClock()
    inner = Mock(spec=Powermeter)
    inner.get_powermeter_watts = AsyncMock(return_value=[100.0, 200.0])
    pm = _make(inner, name="MQTT_1", clock=clock)

    assert pm.last_attempt is None
    assert pm.last_outcome_ok is False

    clock.advance(5.0)
    result = await pm.get_powermeter_watts()

    assert result == [100.0, 200.0]
    assert pm.last_attempt == 5.0
    assert pm.last_outcome_ok is True
    assert pm.last_values == [100.0, 200.0]
    assert pm.name == "MQTT_1"


async def test_last_values_none_until_first_success() -> None:
    inner = Mock(spec=Powermeter)
    inner.get_powermeter_watts = AsyncMock(side_effect=ValueError("stale"))
    pm = _make(inner)

    assert pm.last_values is None
    with pytest.raises(ValueError):
        await pm.get_powermeter_watts()
    # A failed read must not populate last_values.
    assert pm.last_values is None


async def test_records_failure_and_reraises() -> None:
    inner = Mock(spec=Powermeter)
    inner.get_powermeter_watts = AsyncMock(side_effect=ValueError("stale"))
    pm = _make(inner)

    with pytest.raises(ValueError, match="stale"):
        await pm.get_powermeter_watts()

    assert pm.last_attempt is not None
    assert pm.last_outcome_ok is False


async def test_empty_result_counts_as_not_ok() -> None:
    inner = Mock(spec=Powermeter)
    inner.get_powermeter_watts = AsyncMock(return_value=[])
    pm = _make(inner)

    assert await pm.get_powermeter_watts() == []
    assert pm.last_outcome_ok is False


async def test_passive_read_also_tracked() -> None:
    inner = Mock(spec=Powermeter)
    inner.get_passive_watts = AsyncMock(return_value=[7.0])
    pm = _make(inner)

    assert await pm.get_passive_watts() == [7.0]
    assert pm.last_outcome_ok is True


def test_stream_online_is_passed_through() -> None:
    inner = Mock(spec=Powermeter)
    inner.stream_online = Mock(return_value=True)
    pm = _make(inner)
    assert pm.stream_online() is True

    inner.stream_online.return_value = None
    assert pm.stream_online() is None


async def test_lifecycle_delegates_to_inner() -> None:
    inner = Mock(spec=Powermeter)
    inner.start = AsyncMock()
    inner.stop = AsyncMock()
    pm = _make(inner)

    await pm.start()
    await pm.stop()
    inner.start.assert_awaited_once()
    inner.stop.assert_awaited_once()


def test_pipeline_leaves_out_the_shared_reading() -> None:
    """The dashboard's Filters row lists what shapes the reading; the wrapper
    that only shares it with passive readers is not one of those."""
    pm = _make(ThrottledPowermeter(LastReadingPowermeter(FakePowermeter()), 1.0))
    snapshot = pm.status_snapshot()
    assert snapshot.kind == "FakePowermeter"
    assert snapshot.pipeline == ("ThrottledPowermeter",)


class _FailingSource(FakePowermeter):
    """A polled source that can be switched to raising on every read."""

    def __init__(self, values: list[float]) -> None:
        super().__init__(values)
        self.error: Exception | None = None

    async def get_powermeter_watts(self) -> list[float]:
        if self.error is not None:
            raise self.error
        return await super().get_powermeter_watts()


def _shared(
    source: Powermeter, clock: _FakeClock
) -> tuple[HealthTrackingPowermeter, LastReadingPowermeter]:
    shared = LastReadingPowermeter(source, max_age=5.0, clock=clock)
    return _make(shared, clock=clock), shared


async def test_cached_passive_read_does_not_mark_a_failing_meter_healthy() -> None:
    """The reading a passive reader is served from the cache predates the
    failure the control loop just saw, so it must not paper over it."""
    clock = _FakeClock()
    source = _FailingSource([100.0])
    pm, _ = _shared(source, clock)
    await pm.get_powermeter_watts()

    clock.advance(1.0)
    source.error = OSError("offline")
    with pytest.raises(OSError):
        await pm.get_powermeter_watts()
    assert (pm.last_attempt, pm.last_outcome_ok) == (1.0, False)

    clock.advance(1.0)
    assert await pm.get_passive_watts() == [100.0]
    assert (pm.last_attempt, pm.last_outcome_ok) == (1.0, False)


async def test_passive_read_that_reads_the_source_is_recorded() -> None:
    clock = _FakeClock()
    source = _FailingSource([100.0])
    pm, _ = _shared(source, clock)

    clock.advance(1.0)
    assert await pm.get_passive_watts() == [100.0]
    assert (pm.last_attempt, pm.last_outcome_ok) == (1.0, True)

    clock.advance(10.0)
    source.error = OSError("offline")
    with pytest.raises(OSError):
        await pm.get_passive_watts()
    assert (pm.last_attempt, pm.last_outcome_ok) == (11.0, False)


class _PerReadSource(FakePowermeter):
    """A polled source whose reads each finish when the test says so."""

    def __init__(self) -> None:
        super().__init__([])
        self._pending: list[asyncio.Future[list[float]]] = []

    def finish(self, index: int, values: list[float]) -> None:
        self._pending[index].set_result(values)

    def fail(self, index: int, error: Exception) -> None:
        self._pending[index].set_exception(error)

    async def get_powermeter_watts(self) -> list[float]:
        read: asyncio.Future[list[float]] = asyncio.get_running_loop().create_future()
        self._pending.append(read)
        return await read


async def test_passive_read_served_while_waiting_keeps_a_later_failure() -> None:
    """A passive reader waiting on battery reads can be served the one that
    succeeded after another has already failed; that failure is the newer
    fact about the meter, so the passive read must not overwrite it."""
    clock = _FakeClock()
    source = _PerReadSource()
    pm, _ = _shared(source, clock)

    succeeding = asyncio.create_task(pm.get_powermeter_watts())
    failing = asyncio.create_task(pm.get_powermeter_watts())
    await asyncio.sleep(0)
    passive = asyncio.create_task(pm.get_passive_watts())
    await asyncio.sleep(0)

    source.finish(0, [100.0])
    source.fail(1, OSError("timeout"))
    assert await succeeding == [100.0]
    with pytest.raises(OSError):
        await failing
    assert await passive == [100.0]
    assert pm.last_outcome_ok is False
