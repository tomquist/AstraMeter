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

# Test-hooks build: same emulator, plus the UDP control channel (grid
# injection + mock clock) enabled by `test_control_port:` in this YAML.
E2E_YAML = HERE / "test.e2e.host.yaml"
E2E_BINARY = (
    HERE
    / ".esphome"
    / "build"
    / "ct002-e2e-test"
    / ".pioenvs"
    / "ct002-e2e-test"
    / "program"
)
UDP_PORT = 12345
CONTROL_PORT = 12346


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


# ───────────────────────────────────────────────────────────────────────────
# Controllable e2e: the test-hooks binary exposes a UDP control channel so the
# harness can inject grid power and drive a mock clock — the two affordances
# the in-process Python e2e has (via the before_send hook + FakeClock) that a
# black-box binary otherwise lacks. This is the foundation for a shared
# Python↔ESPHome e2e suite; for now it proves the affordances work end to end.
# ───────────────────────────────────────────────────────────────────────────

from astrameter.ct002.protocol import build_payload, parse_request  # noqa: E402


class _Ct002Control:
    """Thin client for the test-hooks control channel + the CT002 UDP port."""

    def __init__(self) -> None:
        self._ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._ctrl.settimeout(2.0)

    def _cmd(self, cmd: str) -> str:
        self._ctrl.sendto(cmd.encode(), ("127.0.0.1", CONTROL_PORT))
        reply = self._ctrl.recvfrom(128)[0].decode()
        assert reply.startswith("ok"), f"control command {cmd!r} failed: {reply!r}"
        return reply

    def set_grid(self, l1: float, l2: float = 0.0, l3: float = 0.0) -> None:
        self._cmd(f"grid {l1} {l2} {l3}")

    def set_clock(self, seconds: float) -> None:
        self._cmd(f"clock_set {seconds}")

    def advance_clock(self, seconds: float) -> None:
        self._cmd(f"clock_advance {seconds}")

    def poll(self, mac: str, phase: str, power: int, timeout: float = 1.5):
        """Send one CT002 poll; return the 24 parsed response fields, or None
        on timeout (e.g. when the request was deduplicated)."""
        payload = build_payload(
            ["HMG-50", mac, "HME-4", "112233445566", phase, str(power)]
        )
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(payload, ("127.0.0.1", UDP_PORT))
            data = s.recvfrom(512)[0]
        except TimeoutError:
            return None
        finally:
            s.close()
        fields, err = parse_request(data)
        return None if err else fields

    def close(self) -> None:
        self._ctrl.close()


@pytest.fixture(scope="module")
def e2e_binary() -> Path:
    """Compile the test-hooks binary (test.e2e.host.yaml) on first use."""
    if not E2E_BINARY.exists():
        subprocess.run(["esphome", "compile", str(E2E_YAML)], check=True, cwd=REPO_ROOT)
    assert E2E_BINARY.exists(), f"expected e2e binary at {E2E_BINARY}"
    return E2E_BINARY


@pytest.fixture
def control(e2e_binary: Path):
    """Spawn the test-hooks binary and yield a _Ct002Control client. Each test
    gets a fresh process (clean consumer/balancer state)."""
    deadline = time.monotonic() + 5.0
    while _port_in_use(UDP_PORT) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _port_in_use(UDP_PORT):
        pytest.skip(f"UDP port {UDP_PORT} still in use — another process is holding it")

    proc = subprocess.Popen(
        [str(e2e_binary)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    deadline = time.monotonic() + 5.0
    while not _port_in_use(UDP_PORT) and time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"e2e binary exited prematurely with code {proc.returncode}")
        time.sleep(0.1)
    if not _port_in_use(UDP_PORT):
        proc.terminate()
        pytest.fail("e2e binary did not bind UDP port within 5s")
    time.sleep(0.3)  # let the control socket finish binding too

    client = _Ct002Control()
    yield client
    client.close()
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()


@pytest.mark.timeout(30, func_only=True)
def test_e2e_grid_injection_sign(control: _Ct002Control) -> None:
    """Injected grid power steers the battery in the correct direction: a
    positive (import) reading tells the battery to discharge (+), a negative
    (export) reading tells it to charge (-)."""
    control.set_clock(1000)
    control.set_grid(300)  # importing 300 W
    r = control.poll("AABBCCDDEEFF", "A", 0)
    assert r is not None, "no response to first poll"
    assert int(r[4]) > 0, (
        f"import should drive discharge (+), got phase-A target {r[4]}"
    )

    control.advance_clock(30)  # past the 10 s dedup window
    control.set_grid(-300)  # exporting 300 W
    r = control.poll("AABBCCDDEEFF", "A", 0)
    assert r is not None, "no response after grid sign flip"
    assert int(r[4]) < 0, f"export should drive charge (-), got phase-A target {r[4]}"


@pytest.mark.timeout(30, func_only=True)
def test_e2e_convergence(control: _Ct002Control) -> None:
    """Closing the loop drives the target toward zero: once the battery
    reports it has taken on the first target, the grid is back to ~0 and the
    next target shrinks."""
    control.set_clock(2000)
    control.set_grid(300)
    r1 = control.poll("AABBCCDDEEFF", "A", 0)
    assert r1 is not None and int(r1[4]) > 0
    t1 = int(r1[4])

    # Battery now outputs ~t1, so the grid it sees drops to ~0.
    control.advance_clock(30)
    control.set_grid(0)
    r2 = control.poll("AABBCCDDEEFF", "A", t1)
    assert r2 is not None
    assert abs(int(r2[4])) < abs(t1), (
        f"target should shrink toward zero as the grid converges: "
        f"first={t1}, second={r2[4]}"
    )


@pytest.mark.timeout(30, func_only=True)
def test_e2e_clock_gated_dedup(control: _Ct002Control) -> None:
    """The mock clock drives the dedup window: a repeat poll inside the
    window is dropped (no response); advancing the clock past it un-gates the
    poll. Proves both the dedup feature and clock control are wired."""
    control.set_clock(5000)
    control.set_grid(100)
    r1 = control.poll("CCDDEEFF0011", "A", 0)
    assert r1 is not None, "first poll should be answered"

    r2 = control.poll("CCDDEEFF0011", "A", 0)  # repeat, no clock advance
    assert r2 is None, "duplicate poll within the dedup window should be dropped"

    control.advance_clock(11)  # past the 10 s window
    r3 = control.poll("CCDDEEFF0011", "A", 0)
    assert r3 is not None, "poll after the dedup window elapses should be answered"
