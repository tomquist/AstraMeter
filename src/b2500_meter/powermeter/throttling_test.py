import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from .throttling import ThrottledPowermeter


async def test_no_throttling_always_fetches_fresh_values():
    """Test that when throttling is disabled, fresh values are always fetched."""
    mock_pm = Mock()
    mock_pm.get_powermeter_watts = AsyncMock(return_value=[100.0, 200.0, 300.0])
    throttled = ThrottledPowermeter(mock_pm, throttle_interval=0)

    result1 = await throttled.get_powermeter_watts()
    result2 = await throttled.get_powermeter_watts()

    assert result1 == [100.0, 200.0, 300.0]
    assert result2 == [100.0, 200.0, 300.0]
    assert mock_pm.get_powermeter_watts.call_count == 2


async def test_throttling_waits_for_interval():
    """Test that throttling waits for remaining time before fetching new values."""
    mock_pm = Mock()
    mock_pm.get_powermeter_watts = AsyncMock(return_value=[100.0, 200.0, 300.0])
    throttled = ThrottledPowermeter(mock_pm, throttle_interval=0.2)

    result1 = await throttled.get_powermeter_watts()
    assert result1 == [100.0, 200.0, 300.0]
    assert mock_pm.get_powermeter_watts.call_count == 1

    mock_pm.get_powermeter_watts.return_value = [400.0, 500.0, 600.0]

    loop = asyncio.get_running_loop()
    start_time = loop.time()
    result2 = await throttled.get_powermeter_watts()
    elapsed = loop.time() - start_time

    assert result2 == [400.0, 500.0, 600.0]
    assert mock_pm.get_powermeter_watts.call_count == 2
    assert elapsed >= 0.2


async def test_throttling_fetches_fresh_after_interval():
    """Test that fresh values are fetched after throttling interval passes."""
    mock_pm = Mock()
    mock_pm.get_powermeter_watts = AsyncMock(return_value=[100.0, 200.0, 300.0])
    throttled = ThrottledPowermeter(mock_pm, throttle_interval=0.1)

    result1 = await throttled.get_powermeter_watts()
    assert result1 == [100.0, 200.0, 300.0]
    assert mock_pm.get_powermeter_watts.call_count == 1

    mock_pm.get_powermeter_watts.return_value = [400.0, 500.0, 600.0]

    await asyncio.sleep(0.2)

    result2 = await throttled.get_powermeter_watts()
    assert result2 == [400.0, 500.0, 600.0]
    assert mock_pm.get_powermeter_watts.call_count == 2


async def test_wait_for_message_passthrough():
    """Test that wait_for_message is passed through to wrapped powermeter."""
    mock_pm = Mock()
    mock_pm.wait_for_message = AsyncMock()
    throttled = ThrottledPowermeter(mock_pm, throttle_interval=1.0)

    await throttled.wait_for_message(timeout=30)
    mock_pm.wait_for_message.assert_called_once_with(30)


async def test_exception_handling_with_cache():
    """Test that cached values are returned on error after a successful fetch."""
    mock_pm = Mock()
    mock_pm.start = AsyncMock()
    mock_pm.stop = AsyncMock()
    mock_pm.get_powermeter_watts = AsyncMock(return_value=[100.0, 200.0])

    throttled = ThrottledPowermeter(mock_pm, throttle_interval=0.1)

    result1 = await throttled.get_powermeter_watts()
    assert result1 == [100.0, 200.0]

    mock_pm.get_powermeter_watts.side_effect = Exception("Network error")

    result2 = await throttled.get_powermeter_watts()
    assert result2 == [100.0, 200.0]


async def test_exception_raises_without_cache():
    """Test that exceptions propagate if no cached values exist."""
    mock_pm = Mock()
    mock_pm.start = AsyncMock()
    mock_pm.stop = AsyncMock()
    mock_pm.get_powermeter_watts = AsyncMock(side_effect=Exception("Network error"))

    throttled = ThrottledPowermeter(mock_pm, throttle_interval=0.1)

    with pytest.raises(Exception, match="Network error"):
        await throttled.get_powermeter_watts()
