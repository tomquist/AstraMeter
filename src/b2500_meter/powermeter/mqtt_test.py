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
    topic: str | list[str] = TEST_TOPIC,
    json_path: str | list[str] | None = None,
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


async def test_get_powermeter_watts_returns_value():
    pm = _make_pm()
    pm.value = 42.0
    assert await pm.get_powermeter_watts() == [42.0]


async def test_get_powermeter_watts_raises_when_no_value():
    pm = _make_pm()
    with pytest.raises(ValueError, match="No value received"):
        await pm.get_powermeter_watts()


async def test_wait_for_message_returns_immediately():
    pm = _make_pm()
    pm.value = 1.0
    await pm.wait_for_message(timeout=0.1)


async def test_wait_for_message_times_out():
    pm = _make_pm()
    with pytest.raises(TimeoutError, match="Timeout waiting"):
        await pm.wait_for_message(timeout=0.1)


async def test_wait_for_message_wakes_on_event():
    pm = _make_pm()

    async def _set_later():
        await asyncio.sleep(0.05)
        pm.value = 99.0
        pm._message_event.set()

    task = asyncio.create_task(_set_later())
    await pm.wait_for_message(timeout=2)
    await task
    assert pm.value == 99.0


# ---------------------------------------------------------------------------
# Multi-phase constructor unit tests
# ---------------------------------------------------------------------------


def test_single_topic_backward_compat():
    pm = _make_pm(topic="t1")
    assert len(pm._subscriptions) == 1
    assert pm._subscriptions[0] == ("t1", None)
    assert len(pm.values) == 1


def test_single_topic_with_json_path_backward_compat():
    pm = _make_pm(topic="t1", json_path="$.power")
    assert pm._subscriptions == [("t1", "$.power")]


def test_multi_topic_constructor():
    pm = _make_pm(topic=["t1", "t2", "t3"])
    assert len(pm._subscriptions) == 3
    assert len(pm.values) == 3
    assert pm._subscriptions == [("t1", None), ("t2", None), ("t3", None)]


def test_single_topic_multi_json_paths():
    pm = _make_pm(topic="t", json_path=["$.a", "$.b", "$.c"])
    assert len(pm._subscriptions) == 3
    assert pm._subscriptions == [("t", "$.a"), ("t", "$.b"), ("t", "$.c")]


def test_multi_topic_single_json_path():
    pm = _make_pm(topic=["t1", "t2"], json_path="$.p")
    assert pm._subscriptions == [("t1", "$.p"), ("t2", "$.p")]


def test_multi_topic_multi_json_path_matching():
    pm = _make_pm(topic=["t1", "t2"], json_path=["$.a", "$.b"])
    assert pm._subscriptions == [("t1", "$.a"), ("t2", "$.b")]


def test_empty_topic_list_raises():
    with pytest.raises(ValueError, match="At least one MQTT topic"):
        _make_pm(topic=[])


def test_multi_topic_multi_json_path_length_mismatch():
    with pytest.raises(ValueError, match="must match"):
        _make_pm(topic=["t1", "t2"], json_path=["$.a", "$.b", "$.c"])


def test_topic_indices_mapping():
    pm = _make_pm(topic="t", json_path=["$.a", "$.b"])
    assert pm._topic_indices == {"t": [0, 1]}


def test_multi_topic_indices_mapping():
    pm = _make_pm(topic=["t1", "t2", "t3"])
    assert pm._topic_indices == {"t1": [0], "t2": [1], "t3": [2]}


# ---------------------------------------------------------------------------
# Multi-phase get/wait unit tests
# ---------------------------------------------------------------------------


async def test_get_watts_raises_when_partial_values():
    pm = _make_pm(topic=["t1", "t2"])
    pm.values[0] = 100.0
    with pytest.raises(ValueError, match="No value received"):
        await pm.get_powermeter_watts()


async def test_get_watts_returns_all_phases():
    pm = _make_pm(topic=["t1", "t2", "t3"])
    pm.values[0] = 100.0
    pm.values[1] = 200.0
    pm.values[2] = 300.0
    assert await pm.get_powermeter_watts() == [100.0, 200.0, 300.0]


async def test_wait_for_message_returns_when_all_set():
    pm = _make_pm(topic=["t1", "t2"])

    async def _set_later():
        await asyncio.sleep(0.05)
        pm.values[0] = 10.0
        pm._message_event.set()
        await asyncio.sleep(0.05)
        pm.values[1] = 20.0
        pm._message_event.set()

    task = asyncio.create_task(_set_later())
    await pm.wait_for_message(timeout=2)
    await task
    assert await pm.get_powermeter_watts() == [10.0, 20.0]


async def test_wait_for_message_times_out_with_partial():
    pm = _make_pm(topic=["t1", "t2"])
    pm.values[0] = 10.0
    # values[1] is still None
    with pytest.raises(TimeoutError, match="Timeout waiting"):
        await pm.wait_for_message(timeout=0.2)


async def test_value_property_backward_compat():
    pm = _make_pm()
    assert pm.value is None
    pm.value = 42.0
    assert pm.values[0] == 42.0
    assert pm.value == 42.0


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
        await pm.wait_for_message(timeout=5)
        assert await pm.get_powermeter_watts() == [42.5]
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
        await pm.wait_for_message(timeout=5)
        assert await pm.get_powermeter_watts() == [123.4]
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
            await pm.wait_for_message(timeout=0.5)
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
        assert await pm.get_powermeter_watts() == [30.0]
    finally:
        await pm.stop()


@_needs_mosquitto
async def test_receives_multi_topic_values(mqtt_broker):
    import aiomqtt

    port = mqtt_broker
    topics = ["test/phase/l1", "test/phase/l2", "test/phase/l3"]
    pm = MqttPowermeter(broker="127.0.0.1", port=port, topic=topics)
    await pm.start()
    try:
        await asyncio.wait_for(pm._connected_event.wait(), timeout=5)
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(topics[0], payload=b"100.0")
            await pub.publish(topics[1], payload=b"200.0")
            await pub.publish(topics[2], payload=b"300.0")
        await pm.wait_for_message(timeout=5)
        assert await pm.get_powermeter_watts() == [100.0, 200.0, 300.0]
    finally:
        await pm.stop()


@_needs_mosquitto
async def test_receives_single_topic_multi_json_paths(mqtt_broker):
    import aiomqtt

    port = mqtt_broker
    topic = "test/multijson"
    json_paths = ["$.l1.power", "$.l2.power", "$.l3.power"]
    pm = MqttPowermeter(
        broker="127.0.0.1", port=port, topic=topic, json_path=json_paths
    )
    await pm.start()
    try:
        await asyncio.wait_for(pm._connected_event.wait(), timeout=5)
        payload = {
            "l1": {"power": 110.5},
            "l2": {"power": 220.3},
            "l3": {"power": 330.1},
        }
        async with aiomqtt.Client(hostname="127.0.0.1", port=port) as pub:
            await pub.publish(topic, payload=json.dumps(payload).encode())
        await pm.wait_for_message(timeout=5)
        assert await pm.get_powermeter_watts() == [110.5, 220.3, 330.1]
    finally:
        await pm.stop()
