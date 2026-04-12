from unittest.mock import AsyncMock, Mock, patch

import pytest

from .pid import PidPowermeter


@pytest.fixture
def mock_powermeter():
    """Return a mock powermeter with async stubs for all interface methods."""
    pm = Mock()
    pm.get_powermeter_watts = AsyncMock()
    pm.wait_for_message = AsyncMock()
    pm.wait_for_next_message = AsyncMock()
    pm.start = AsyncMock()
    pm.stop = AsyncMock()
    return pm


# ------------------------------------------------------------------
# Construction / validation
# ------------------------------------------------------------------


def test_invalid_output_max_raises(mock_powermeter):
    """output_max must be positive."""
    with pytest.raises(ValueError):
        PidPowermeter(mock_powermeter, output_max=0.0)
    with pytest.raises(ValueError):
        PidPowermeter(mock_powermeter, output_max=-100.0)


def test_invalid_mode_raises(mock_powermeter):
    """Only 'bias' and 'replace' are accepted."""
    with pytest.raises(ValueError):
        PidPowermeter(mock_powermeter, mode="invalid")


def test_mode_case_insensitive(mock_powermeter):
    """Mode string should be case-insensitive."""
    pm = PidPowermeter(mock_powermeter, mode="BIAS")
    assert pm.mode == "bias"
    pm2 = PidPowermeter(mock_powermeter, mode="Replace")
    assert pm2.mode == "replace"


# ------------------------------------------------------------------
# Proportional-only behaviour
# ------------------------------------------------------------------


async def test_p_only_positive_error(mock_powermeter):
    """With P-only control, a large import (actual=200W) produces
    a negative adjustment to tell the storage device to cover that import."""
    mock_powermeter.get_powermeter_watts.return_value = [200.0]
    pm = PidPowermeter(mock_powermeter, kp=1.0, output_max=800.0)
    # error = -200  →  P output = -200
    result = await pm.get_powermeter_watts()
    # bias mode: 200 + (-200) = 0  (reported as balanced, device stops)
    assert result[0] == pytest.approx(0.0)


async def test_p_only_negative_error(mock_powermeter):
    """Export reading (actual=-100W) produces a positive adjustment."""
    mock_powermeter.get_powermeter_watts.return_value = [-100.0]
    pm = PidPowermeter(mock_powermeter, kp=1.0, output_max=800.0)
    # error = -(-100) = 100  →  P output = 100
    result = await pm.get_powermeter_watts()
    # bias: -100 + 100 = 0
    assert result[0] == pytest.approx(0.0)


# ------------------------------------------------------------------
# Output clamping
# ------------------------------------------------------------------


async def test_output_clamped_positive(mock_powermeter):
    """PID output should not exceed +output_max."""
    mock_powermeter.get_powermeter_watts.return_value = [-1000.0]
    pm = PidPowermeter(mock_powermeter, kp=1.0, output_max=500.0)
    # error = -(-1000) = 1000  →  clamped to 500
    result = await pm.get_powermeter_watts()
    assert result[0] == pytest.approx(-500.0)  # -1000 + 500


async def test_output_clamped_negative(mock_powermeter):
    """PID output should not go below -output_max."""
    mock_powermeter.get_powermeter_watts.return_value = [2000.0]
    pm = PidPowermeter(mock_powermeter, kp=1.0, output_max=500.0)
    # error = -2000  →  clamped to -500
    result = await pm.get_powermeter_watts()
    assert result[0] == pytest.approx(1500.0)  # 2000 + (-500)


# ------------------------------------------------------------------
# Integral behaviour
# ------------------------------------------------------------------


async def test_integral_accumulates_over_time(mock_powermeter):
    """The integral term should grow over successive calls."""
    mock_powermeter.get_powermeter_watts.return_value = [100.0]
    pm = PidPowermeter(mock_powermeter, kp=0.0, ki=1.0, output_max=800.0)

    t0 = 1000.0
    with patch("astrameter.powermeter.pid.time") as mock_time:
        mock_time.monotonic.return_value = t0
        await pm.get_powermeter_watts()  # first call — init state

    with patch("astrameter.powermeter.pid.time") as mock_time:
        mock_time.monotonic.return_value = t0 + 1.0
        r2 = await pm.get_powermeter_watts()

    # error = -100, integral = -100 * 1s = -100
    # I output = 1.0 * -100 = -100
    assert r2[0] == pytest.approx(0.0)  # 100 + (-100) in bias mode


async def test_anti_windup_stops_integration(mock_powermeter):
    """The integral should not grow beyond what output_max allows."""
    mock_powermeter.get_powermeter_watts.return_value = [500.0]
    pm = PidPowermeter(mock_powermeter, kp=0.0, ki=1.0, output_max=200.0)

    t0 = 1000.0
    with patch("astrameter.powermeter.pid.time") as mock_time:
        mock_time.monotonic.return_value = t0
        await pm.get_powermeter_watts()  # init

    with patch("astrameter.powermeter.pid.time") as mock_time:
        mock_time.monotonic.return_value = t0 + 10.0
        result = await pm.get_powermeter_watts()

    # Without anti-windup the integral would be -500*10 = -5000
    # But output is clamped to -200, so bias result >= 500 - 200 = 300
    assert result[0] >= 300.0


# ------------------------------------------------------------------
# Derivative behaviour
# ------------------------------------------------------------------


async def test_derivative_reacts_to_change(mock_powermeter):
    """The D term should respond to changes in error."""
    pm = PidPowermeter(mock_powermeter, kp=0.0, kd=1.0, output_max=800.0)

    t0 = 1000.0
    mock_powermeter.get_powermeter_watts.return_value = [100.0]
    with patch("astrameter.powermeter.pid.time") as mock_time:
        mock_time.monotonic.return_value = t0
        await pm.get_powermeter_watts()

    mock_powermeter.get_powermeter_watts.return_value = [200.0]
    with patch("astrameter.powermeter.pid.time") as mock_time:
        mock_time.monotonic.return_value = t0 + 1.0
        result = await pm.get_powermeter_watts()

    # error1 = -100, error2 = -200
    # d_term = 1.0 * (-200 - (-100)) / 1.0 = -100
    # bias: 200 + (-100) = 100
    assert result[0] == pytest.approx(100.0)


# ------------------------------------------------------------------
# Multi-phase
# ------------------------------------------------------------------


async def test_multiphase_bias(mock_powermeter):
    """PID output should be distributed equally across phases in bias mode."""
    mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]
    pm = PidPowermeter(mock_powermeter, kp=1.0, output_max=800.0)
    # total = 600, error = -600, P = -600
    # per_phase = -600 / 3 = -200
    result = await pm.get_powermeter_watts()
    assert result[0] == pytest.approx(-100.0)  # 100 + (-200)
    assert result[1] == pytest.approx(0.0)  # 200 + (-200)
    assert result[2] == pytest.approx(100.0)  # 300 + (-200)


async def test_multiphase_replace(mock_powermeter):
    """In replace mode, all phases should get equal share of PID output."""
    mock_powermeter.get_powermeter_watts.return_value = [100.0, 200.0, 300.0]
    pm = PidPowermeter(
        mock_powermeter,
        kp=1.0,
        output_max=800.0,
        mode="replace",
    )
    # total = 600, error = -600, P = -600
    # per_phase = -600 / 3 = -200
    result = await pm.get_powermeter_watts()
    assert len(result) == 3
    assert result[0] == pytest.approx(-200.0)
    assert result[1] == pytest.approx(-200.0)
    assert result[2] == pytest.approx(-200.0)


# ------------------------------------------------------------------
# Replace mode basics
# ------------------------------------------------------------------


async def test_replace_mode(mock_powermeter):
    """In replace mode the raw value should be discarded."""
    mock_powermeter.get_powermeter_watts.return_value = [500.0]
    pm = PidPowermeter(
        mock_powermeter,
        kp=1.0,
        output_max=800.0,
        mode="replace",
    )
    # error = -500, P = -500
    result = await pm.get_powermeter_watts()
    assert result[0] == pytest.approx(-500.0)


# ------------------------------------------------------------------
# Zero gains (disabled)
# ------------------------------------------------------------------


async def test_all_gains_zero_passthrough(mock_powermeter):
    """With all gains at zero, the PID should have no effect."""
    mock_powermeter.get_powermeter_watts.return_value = [123.4]
    pm = PidPowermeter(mock_powermeter, kp=0.0, ki=0.0, kd=0.0)
    result = await pm.get_powermeter_watts()
    assert result[0] == pytest.approx(123.4)


# ------------------------------------------------------------------
# wait_for_message pass-through
# ------------------------------------------------------------------


async def test_wait_for_message_passthrough(mock_powermeter):
    """wait_for_message should be delegated to the wrapped powermeter."""
    pm = PidPowermeter(mock_powermeter, kp=1.0)
    await pm.wait_for_message(timeout=7)
    mock_powermeter.wait_for_message.assert_called_once_with(7)


async def test_wait_for_next_message_passthrough(mock_powermeter):
    pm = PidPowermeter(mock_powermeter, kp=1.0)
    await pm.wait_for_next_message(timeout=3)
    mock_powermeter.wait_for_next_message.assert_called_once_with(3)


# ------------------------------------------------------------------
# Immutability
# ------------------------------------------------------------------


async def test_does_not_mutate_wrapped_list(mock_powermeter):
    """The wrapper must not mutate the list from the inner powermeter."""
    raw = [100.0, 200.0]
    mock_powermeter.get_powermeter_watts.return_value = raw
    pm = PidPowermeter(mock_powermeter, kp=0.5)

    await pm.get_powermeter_watts()
    assert raw == [100.0, 200.0]


# ------------------------------------------------------------------
# First call — no derivative spike
# ------------------------------------------------------------------


async def test_first_call_no_derivative_spike(mock_powermeter):
    """The first call should not produce a derivative spike."""
    mock_powermeter.get_powermeter_watts.return_value = [500.0]
    pm = PidPowermeter(mock_powermeter, kp=0.0, kd=100.0, output_max=800.0)
    # First call: dt=0, so D term should be 0
    result = await pm.get_powermeter_watts()
    assert result[0] == pytest.approx(500.0)
