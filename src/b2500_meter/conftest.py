"""Shared test fixtures — Mosquitto broker for MQTT integration tests."""

import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

needs_mosquitto = pytest.mark.skipif(
    shutil.which("mosquitto") is None,
    reason="mosquitto not installed",
)


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mqtt_broker():
    if shutil.which("mosquitto") is None:
        pytest.skip("mosquitto not installed")
    port = find_free_port()
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
