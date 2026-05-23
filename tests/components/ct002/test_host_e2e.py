"""Tier D end-to-end test: drives the AstraMeter BatterySimulator against the
ESPHome host-platform binary.

This is the canonical-client validation that the plan calls out as Tier D —
real UDP wire bytes from the same code real users run against their B2500s,
flowing through the real ct002 host binary (sensor cache → filter pipeline →
balancer → response builder → wire). Any drift between Python's BatterySimulator
expectations and the C++ port's response is caught here in a single integration
test.

Skipped when the ESPHome toolchain is not installed locally. Runs in CI
via the dedicated ``ct002-host-e2e`` job (.github/workflows/ci.yml), which
installs esphome, builds the host binary, and runs this test.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

import pytest

from astrameter.simulator.battery import BatterySimulator

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent.parent
HOST_YAML = HERE / "test.host.yaml"
HOST_BINARY = (
    HERE
    / ".esphome"
    / "build"
    / "ct002-host-test"
    / ".pioenvs"
    / "ct002-host-test"
    / "program"
)


def _have_esphome() -> bool:
    return shutil.which("esphome") is not None


pytestmark = pytest.mark.skipif(
    not _have_esphome(),
    reason="esphome CLI not on PATH; install with `uv tool install esphome` to run E2E tests",
)


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


@pytest.fixture(scope="module")
def host_binary() -> Path:
    """Compile the ct002 host binary on first use; reuse on subsequent tests."""
    if not HOST_BINARY.exists():
        subprocess.run(
            ["esphome", "compile", str(HOST_YAML)],
            check=True,
            cwd=REPO_ROOT,
        )
    assert HOST_BINARY.exists(), f"expected host binary at {HOST_BINARY}"
    return HOST_BINARY


@pytest.fixture
def running_binary(host_binary: Path):
    """Spawn the host binary; tear it down after the test."""
    # Wait for any previous test's binary to fully release the port.
    deadline = time.monotonic() + 5.0
    while _port_in_use(12345) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _port_in_use(12345):
        pytest.skip("UDP port 12345 still in use — another process is holding it")

    proc = subprocess.Popen(
        [str(host_binary)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    # Wait for the binary to bind the port.
    deadline = time.monotonic() + 5.0
    while not _port_in_use(12345) and time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"host binary exited prematurely with code {proc.returncode}")
        time.sleep(0.1)
    if not _port_in_use(12345):
        proc.terminate()
        pytest.fail("host binary did not bind UDP 12345 within 5s")
    yield proc
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()


@pytest.mark.timeout(30, func_only=True)
def test_battery_simulator_round_trip(running_binary: subprocess.Popen) -> None:
    """One BatterySimulator step against the host binary returns parsed fields
    matching the canonical CT002 response shape.

    func_only=True so the 30s bound covers only the UDP round-trip — the
    module-scoped host_binary fixture's `esphome compile` (cold builds can
    take minutes) is deliberately left unbounded.
    """

    async def go() -> list[str] | None:
        battery = BatterySimulator(
            mac="AABBCCDDEEFF",
            phase="A",
            ct_mac="112233445566",
            ct_host="127.0.0.1",
            ct_port=12345,
            poll_interval=1.0,
            startup_delay=0.0,
        )
        # First few steps may be inspection-mode (startup_delay applies via
        # inspection_count even with startup_delay=0); drive enough steps to
        # land at least one normal-mode response.
        last_fields: list[str] | None = None
        for _ in range(5):
            result = await battery.step(dt=0.1)
            if result is not None:
                last_fields = result
                if last_fields[4] != "0":  # not inspection mode
                    break
            await asyncio.sleep(0.05)
        return last_fields

    fields = asyncio.run(go())
    assert fields is not None, "BatterySimulator never received a response"
    # The response is parsed CT002 fields. RESPONSE_LABELS has 24 entries.
    assert len(fields) == 24, f"expected 24 fields, got {len(fields)}: {fields}"
    # ct_type and ct_mac come back as configured on the ct002 component.
    assert fields[0] == "HME-4", fields[0]
    # meter_dev_type/meter_mac echo the request.
    assert fields[2] == "HMG-50", fields[2]
    assert fields[3].lower() == "aabbccddeeff", fields[3]
