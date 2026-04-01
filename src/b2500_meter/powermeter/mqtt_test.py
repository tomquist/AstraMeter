import asyncio
import json
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from .mqtt import MqttPowermeter, extract_json_value

# ---------------------------------------------------------------------------
# extract_json_value unit tests
# ---------------------------------------------------------------------------


def test_extract_curr_w():
    data = {"SML": {"curr_w": 381}}
    assert extract_json_value(data, "$.SML.curr_w") == 381


def test_extract_nonexistent_path():
    data = {"SML": {"curr_w": 381}}
    with pytest.raises(ValueError):
        extract_json_value(data, "$.SML.nonexistent")


def test_extract_float_value():
    data = {"SML": {"curr_w": 381.75}}
    assert extract_json_value(data, "$.SML.curr_w") == 381.75


def test_extract_from_array():
    data = {
        "SML": {
            "measurements": [{"curr_w": 100.5}, {"curr_w": 200.75}, {"curr_w": 300}]
        }
    }
    assert extract_json_value(data, "$.SML.measurements[1].curr_w") == 200.75


# ---------------------------------------------------------------------------
# MqttPowermeter async unit tests (no broker needed)
# ---------------------------------------------------------------------------

TEST_TOPIC = "test/power"


def _make_pm(
    broker: str = "localhost",
    port: int = 1883,
    topic: str = TEST_TOPIC,
    json_path: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> MqttPowermeter:
    return MqttPowermeter(
        broker=broker,
        port=port,
        topic=topic,
        json_path=json_path,
        username=username,
        password=password,
    )


async def test_get_powermeter_watts_async_returns_value():
    pm = _make_pm()
    pm.value = 42.0
    assert await pm.get_powermeter_watts_async() == [42.0]


async def test_get_powermeter_watts_async_raises_when_no_value():
    pm = _make_pm()
    with pytest.raises(ValueError, match="No value received"):
        await pm.get_powermeter_watts_async()


async def test_wait_for_message_async_returns_immediately():
    pm = _make_pm()
    pm.value = 1.0
    await pm.wait_for_message_async(timeout=0.1)


async def test_wait_for_message_async_times_out():
    pm = _make_pm()
    with pytest.raises(TimeoutError, match="Timeout waiting"):
        await pm.wait_for_message_async(timeout=0.1)


async def test_wait_for_message_async_wakes_on_event():
    pm = _make_pm()

    async def _set_later():
        await asyncio.sleep(0.05)
        pm.value = 99.0
        pm._message_event.set()

    task = asyncio.create_task(_set_later())
    await pm.wait_for_message_async(timeout=2)
    await task
    assert pm.value == 99.0


# ---------------------------------------------------------------------------
# Integration tests (require mosquitto)
# ---------------------------------------------------------------------------

_needs_mosquitto = pytest.mark.skipif(
    shutil.which("mosquitto") is None,
    reason="mosquitto not installed",
)


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mqtt_broker():
    port = _find_free_port()
    tmpdir = tempfile.mkdtemp()
    config_path = Path(tmpdir) / "mosquitto.conf"
    config_path.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
    )
    proc = subprocess.Popen(
        ["mosquitto", "-c", str(config_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for broker to be ready
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError("mosquitto did not start in time")

    yield port

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    shutil.rmtree(tmpdir, ignore_errors=True)


@_needs_mosquitto
async def test_receives_plain_value(mqtt_broker):
    import aiomqtt

    port = mqtt_broker
    topic = "test/plain"
    pm = MqttPowermeter(broker="127.0.0.1", port=port, topic=topic)
    await pm.start()
    try:
        await asyncio.wait_for(pm._connected_event.wait(), timeout=5)
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(topic, payload=b"42.5")
        await pm.wait_for_message_async(timeout=5)
        assert await pm.get_powermeter_watts_async() == [42.5]
    finally:
        await pm.stop()


@_needs_mosquitto
async def test_receives_json_value(mqtt_broker):
    import aiomqtt

    port = mqtt_broker
    topic = "test/json"
    pm = MqttPowermeter(broker="127.0.0.1", port=port, topic=topic, json_path="$.power")
    await pm.start()
    try:
        await asyncio.wait_for(pm._connected_event.wait(), timeout=5)
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(topic, payload=json.dumps({"power": 123.4}).encode())
        await pm.wait_for_message_async(timeout=5)
        assert await pm.get_powermeter_watts_async() == [123.4]
    finally:
        await pm.stop()


@_needs_mosquitto
async def test_wait_for_message_timeout_with_no_publish(mqtt_broker):
    port = mqtt_broker
    topic = "test/timeout"
    pm = MqttPowermeter(broker="127.0.0.1", port=port, topic=topic)
    await pm.start()
    try:
        await asyncio.wait_for(pm._connected_event.wait(), timeout=5)
        with pytest.raises(TimeoutError):
            await pm.wait_for_message_async(timeout=0.5)
    finally:
        await pm.stop()


@_needs_mosquitto
async def test_receives_multiple_messages_returns_latest(mqtt_broker):
    import aiomqtt

    port = mqtt_broker
    topic = "test/multi"
    pm = MqttPowermeter(broker="127.0.0.1", port=port, topic=topic)
    await pm.start()
    try:
        await asyncio.wait_for(pm._connected_event.wait(), timeout=5)
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            for val in [10.0, 20.0, 30.0]:
                await pub.publish(topic, payload=str(val).encode())
        # Give the listener time to process all messages
        await asyncio.sleep(0.5)
        assert await pm.get_powermeter_watts_async() == [30.0]
    finally:
        await pm.stop()
