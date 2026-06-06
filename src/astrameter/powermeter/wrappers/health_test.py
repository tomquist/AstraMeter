from unittest.mock import AsyncMock, Mock

import pytest

from astrameter.powermeter.base import Powermeter

from .health import HealthTrackingPowermeter


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _make(wrapped: Powermeter, **kwargs) -> HealthTrackingPowermeter:
    return HealthTrackingPowermeter(wrapped, **kwargs)


async def test_passes_values_through_and_records_success():
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
    assert pm.name == "MQTT_1"


async def test_records_failure_and_reraises():
    inner = Mock(spec=Powermeter)
    inner.get_powermeter_watts = AsyncMock(side_effect=ValueError("stale"))
    pm = _make(inner)

    with pytest.raises(ValueError, match="stale"):
        await pm.get_powermeter_watts()

    assert pm.last_attempt is not None
    assert pm.last_outcome_ok is False


async def test_empty_result_counts_as_not_ok():
    inner = Mock(spec=Powermeter)
    inner.get_powermeter_watts = AsyncMock(return_value=[])
    pm = _make(inner)

    assert await pm.get_powermeter_watts() == []
    assert pm.last_outcome_ok is False


async def test_raw_read_also_tracked():
    inner = Mock(spec=Powermeter)
    inner.get_powermeter_watts_raw = AsyncMock(return_value=[7.0])
    pm = _make(inner)

    assert await pm.get_powermeter_watts_raw() == [7.0]
    assert pm.last_outcome_ok is True


def test_stream_online_is_passed_through():
    inner = Mock(spec=Powermeter)
    inner.stream_online = Mock(return_value=True)
    pm = _make(inner)
    assert pm.stream_online() is True

    inner.stream_online.return_value = None
    assert pm.stream_online() is None


async def test_lifecycle_delegates_to_inner():
    inner = Mock(spec=Powermeter)
    inner.start = AsyncMock()
    inner.stop = AsyncMock()
    pm = _make(inner)

    await pm.start()
    await pm.stop()
    inner.start.assert_awaited_once()
    inner.stop.assert_awaited_once()
