"""Shared test fixtures — Mosquitto broker for MQTT integration tests."""

import shutil
import signal
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
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


_CONFIG_DIR_PREFIX = "astrameter-mosquitto-"


def _start_failure(proc: subprocess.Popen[bytes], what: str) -> RuntimeError:
    """Explain a broker that never came up, with whatever it printed."""
    try:
        _, err = proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
    stderr = err.decode(errors="replace").strip()
    message = f"mosquitto {what}"
    if stderr:
        message += f":\n{stderr}"
    if "Unable to open config file" in stderr:
        # Ubuntu 26.04's AppArmor profile for /usr/sbin/mosquitto only lets it
        # read config under /etc/mosquitto, not the temp file written here.
        message += (
            "\n\nIf AppArmor confines mosquitto (Ubuntu 26.04 and later), let it "
            "read the test config the way CI does:\n"
            f"  echo '{tempfile.gettempdir()}/{_CONFIG_DIR_PREFIX}*/mosquitto.conf r,' "
            "| sudo tee /etc/apparmor.d/local/mosquitto\n"
            "  sudo apparmor_parser -r /etc/apparmor.d/mosquitto"
        )
    return RuntimeError(message)


def _run_mosquitto(extra_config: str = "") -> Iterator[int]:
    if shutil.which("mosquitto") is None:
        pytest.skip("mosquitto not installed")
    port = find_free_port()
    # The prefix is what CI's AppArmor override for mosquitto matches on
    # (.github/workflows/ci.yml, the validate job); keep the two in step.
    tmpdir = tempfile.mkdtemp(prefix=_CONFIG_DIR_PREFIX)
    config_path = Path(tmpdir) / "mosquitto.conf"
    config_path.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n"
        + extra_config
    )
    proc = subprocess.Popen(
        ["mosquitto", "-c", str(config_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for broker to be ready
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise _start_failure(proc, f"exited with status {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.terminate()
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise _start_failure(proc, "did not start in time")

    yield port

    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture(scope="session")
def mqtt_broker() -> Iterator[int]:
    yield from _run_mosquitto()


@pytest.fixture(scope="session")
def strict_mqtt_broker() -> Iterator[int]:
    """A broker that refuses an empty client identifier, as some setups do."""
    yield from _run_mosquitto("allow_zero_length_clientid false\n")
