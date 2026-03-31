import time
import unittest
from unittest.mock import AsyncMock, Mock

import pytest

from .throttling import ThrottledPowermeter


class TestThrottledPowermeter(unittest.TestCase):
    def setUp(self):
        self.mock_powermeter = Mock()
        self.mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]

    def test_no_throttling_always_fetches_fresh_values(self):
        """Test that when throttling is disabled, fresh values are always fetched."""
        throttled = ThrottledPowermeter(self.mock_powermeter, throttle_interval=0)

        # Multiple calls should all fetch fresh values
        result1 = throttled.get_powermeter_watts()
        result2 = throttled.get_powermeter_watts()

        self.assertEqual(result1, [100.0, 200.0, 300.0])
        self.assertEqual(result2, [100.0, 200.0, 300.0])
        self.assertEqual(self.mock_powermeter.get_powermeter_watts.call_count, 2)

    def test_throttling_waits_for_interval(self):
        """Test that throttling waits for remaining time before fetching new values."""
        throttled = ThrottledPowermeter(self.mock_powermeter, throttle_interval=0.2)

        # First call should fetch fresh values
        start_time = time.time()
        result1 = throttled.get_powermeter_watts()
        self.assertEqual(result1, [100.0, 200.0, 300.0])
        self.assertEqual(self.mock_powermeter.get_powermeter_watts.call_count, 1)

        # Change the mock return value for next call
        self.mock_powermeter.get_powermeter_watts.return_value = [400.0, 500.0, 600.0]

        # Second call immediately should wait and then fetch new values
        result2 = throttled.get_powermeter_watts()
        elapsed_time = time.time() - start_time

        # Should have fetched new values after waiting
        self.assertEqual(result2, [400.0, 500.0, 600.0])
        self.assertEqual(self.mock_powermeter.get_powermeter_watts.call_count, 2)
        # Should have waited approximately the throttle interval
        self.assertGreaterEqual(elapsed_time, 0.2)

    def test_throttling_fetches_fresh_after_interval(self):
        """Test that fresh values are fetched after throttling interval passes."""
        throttled = ThrottledPowermeter(self.mock_powermeter, throttle_interval=0.1)

        # First call
        result1 = throttled.get_powermeter_watts()
        self.assertEqual(result1, [100.0, 200.0, 300.0])
        self.assertEqual(self.mock_powermeter.get_powermeter_watts.call_count, 1)

        # Change the mock return value
        self.mock_powermeter.get_powermeter_watts.return_value = [400.0, 500.0, 600.0]

        # Wait for throttling interval to pass
        time.sleep(0.2)

        # Should fetch fresh values now
        result2 = throttled.get_powermeter_watts()
        self.assertEqual(result2, [400.0, 500.0, 600.0])
        self.assertEqual(self.mock_powermeter.get_powermeter_watts.call_count, 2)

    def test_wait_for_message_passthrough(self):
        """Test that wait_for_message is passed through to wrapped powermeter."""
        throttled = ThrottledPowermeter(self.mock_powermeter, throttle_interval=1.0)

        throttled.wait_for_message(timeout=30)
        # Check that the call was made with the timeout parameter
        self.mock_powermeter.wait_for_message.assert_called_once()
        call_args = self.mock_powermeter.wait_for_message.call_args
        # The timeout is passed as a keyword argument
        if call_args[1]:  # Check if there are keyword arguments
            self.assertEqual(call_args[1]["timeout"], 30)
        else:  # If passed as positional argument
            self.assertEqual(call_args[0][0], 30)

    def test_exception_handling(self):
        """Test that exceptions are handled gracefully with cached fallback."""
        throttled = ThrottledPowermeter(self.mock_powermeter, throttle_interval=0.1)

        # First successful call to populate cache
        result1 = throttled.get_powermeter_watts()
        self.assertEqual(result1, [100.0, 200.0, 300.0])

        # Make the mock raise an exception on next call
        self.mock_powermeter.get_powermeter_watts.side_effect = Exception(
            "Network error"
        )

        # Next call should wait for interval, then fail and return cached values
        result2 = throttled.get_powermeter_watts()
        self.assertEqual(result2, [100.0, 200.0, 300.0])


async def test_async_exception_handling_with_cache():
    """Test that async path returns cached values on error, matching sync behavior."""
    mock_pm = Mock()
    mock_pm.start = AsyncMock()
    mock_pm.stop = AsyncMock()
    mock_pm.get_powermeter_watts_async = AsyncMock(return_value=[100.0, 200.0])

    throttled = ThrottledPowermeter(mock_pm, throttle_interval=0.1)

    # First successful call to populate cache
    result1 = await throttled.get_powermeter_watts_async()
    assert result1 == [100.0, 200.0]

    # Make the mock raise on next call
    mock_pm.get_powermeter_watts_async.side_effect = Exception("Network error")

    # Should return cached values instead of raising
    result2 = await throttled.get_powermeter_watts_async()
    assert result2 == [100.0, 200.0]


async def test_async_exception_raises_without_cache():
    """Test that async path raises if no cached values exist."""
    mock_pm = Mock()
    mock_pm.start = AsyncMock()
    mock_pm.stop = AsyncMock()
    mock_pm.get_powermeter_watts_async = AsyncMock(
        side_effect=Exception("Network error")
    )

    throttled = ThrottledPowermeter(mock_pm, throttle_interval=0.1)

    # No cached values -- should raise
    with pytest.raises(Exception, match="Network error"):
        await throttled.get_powermeter_watts_async()


if __name__ == "__main__":
    unittest.main()
