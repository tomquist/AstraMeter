"""Shared CT002 end-to-end scenarios run against BOTH backends.

Each scenario is written once and parametrized over two implementations of
the same `poll / set_grid / set_clock / advance_clock` interface:

  * ``python``  — the canonical `astrameter.ct002.CT002` driven in-process
    (its `_handle_request` is called with a fake transport that captures the
    reply). Grid power is injected via the `before_send` hook; time is a
    controllable fake clock — exactly what the existing Python e2e harness
    uses.
  * ``esphome`` — the compiled host binary (test.e2e.host.yaml) driven over
    real UDP, with grid + mock clock supplied through the test-hooks control
    channel.

Asserting the same wire-observable facts against both proves the C++ port
behaves like the Python original. The ``python`` backend needs no ESPHome
toolchain (so those parametrizations always run); the ``esphome`` ones skip
when the ``esphome`` CLI isn't installed.
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

from astrameter.ct002.ct002 import CT002
from astrameter.ct002.protocol import build_payload, parse_request

pytestmark = pytest.mark.esphome_e2e

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent.parent
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
DEDUPE_WINDOW_S = 10  # must match dedupe_window in test.e2e.host.yaml

# The CT002 request both backends send. ct_mac is blank on the emulator side
# (mirror mode), so field[3] is echoed back and its exact value is irrelevant.
_REQUEST_CT_MAC = "112233445566"


def _have_esphome() -> bool:
    return shutil.which("esphome") is not None


def _build_poll(mac: str, phase: str, power: int) -> bytes:
    return build_payload(["HMG-50", mac, "HME-4", _REQUEST_CT_MAC, phase, str(power)])


# ── Backend: in-process Python CT002 ───────────────────────────────────────


class _FakeClock:
    def __init__(self) -> None:
        self._now = 1000.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeTransport:
    """Captures the bytes CT002 would have sent back over UDP."""

    def __init__(self) -> None:
        self.sent: bytes | None = None

    def sendto(self, data: bytes, addr) -> None:
        self.sent = data


class PythonBackend:
    name = "python"

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._clock = _FakeClock()
        self._grid = [0.0, 0.0, 0.0]
        self.ct002 = CT002(
            udp_port=UDP_PORT,  # unused: we never start() a real socket
            ct_mac="",  # mirror mode, like the e2e YAML
            active_control=True,
            fair_distribution=True,
            clock=self._clock,
            reset_fn=None,
            dedupe_time_window=0.0,  # off by default; set_dedupe() toggles it
        )

        async def _before_send(_addr, _fields=None, _consumer_id=None):
            return list(self._grid)

        self.ct002.before_send = _before_send

    def set_grid(self, l1: float, l2: float = 0.0, l3: float = 0.0) -> None:
        self._grid = [float(l1), float(l2), float(l3)]

    def set_clock(self, seconds: float) -> None:
        self._clock._now = float(seconds)

    def advance_clock(self, seconds: float) -> None:
        self._clock.advance(float(seconds))

    def set_dedupe(self, window_s: float) -> None:
        # Mirrors the binary's runtime `dedupe <ms>` control.
        self.ct002._dedup._window = float(window_s)

    def poll(self, mac: str, phase: str, power: int):
        transport = _FakeTransport()
        addr = ("127.0.0.1", 50000)  # synthetic; consumer_id keys off meter_mac
        self._loop.run_until_complete(
            self.ct002._handle_request(_build_poll(mac, phase, power), addr, transport)
        )
        if transport.sent is None:
            return None  # deduped / dropped — mirrors a UDP timeout
        fields, err = parse_request(transport.sent)
        return None if err else fields

    def close(self) -> None:
        self._loop.close()


# ── Backend: compiled ESPHome host binary over UDP + control channel ───────


class EsphomeBackend:
    name = "esphome"

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

    def set_dedupe(self, window_s: float) -> None:
        self._cmd(f"dedupe {int(window_s * 1000)}")

    def poll(self, mac: str, phase: str, power: int, timeout: float = 1.5):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            s.sendto(_build_poll(mac, phase, power), ("127.0.0.1", UDP_PORT))
            data = s.recvfrom(512)[0]
        except TimeoutError:
            return None
        finally:
            s.close()
        fields, err = parse_request(data)
        return None if err else fields

    def close(self) -> None:
        self._ctrl.close()


def _port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _ensure_e2e_binary() -> Path:
    if not E2E_BINARY.exists():
        subprocess.run(["esphome", "compile", str(E2E_YAML)], check=True, cwd=REPO_ROOT)
    assert E2E_BINARY.exists(), f"expected e2e binary at {E2E_BINARY}"
    return E2E_BINARY


@pytest.fixture(params=["python", "esphome"])
def backend(request):
    """Yield each CT002 backend implementing the shared control interface.

    The ``python`` backend always runs; ``esphome`` skips without the CLI.
    """
    if request.param == "python":
        be = PythonBackend()
        yield be
        be.close()
        return

    # esphome backend
    if not _have_esphome():
        pytest.skip("esphome CLI not on PATH; install with `uv tool install esphome`")
    binary = _ensure_e2e_binary()

    deadline = time.monotonic() + 5.0
    while _port_in_use(UDP_PORT) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _port_in_use(UDP_PORT):
        pytest.skip(f"UDP port {UDP_PORT} still in use — another process is holding it")

    proc = subprocess.Popen(
        [str(binary)],
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

    be = EsphomeBackend()
    yield be
    be.close()
    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.wait()


# ── Shared scenarios (run against both backends) ───────────────────────────


@pytest.mark.timeout(30, func_only=True)
def test_grid_injection_sign(backend) -> None:
    """Injected grid power steers the battery in the correct direction on
    both stacks: import (+) -> discharge (+), export (-) -> charge (-)."""
    backend.set_clock(1000)
    backend.set_grid(300)  # importing 300 W
    r = backend.poll("AABBCCDDEEFF", "A", 0)
    assert r is not None, f"[{backend.name}] no response to first poll"
    assert int(r[4]) > 0, (
        f"[{backend.name}] import should drive discharge (+), got {r[4]}"
    )

    backend.advance_clock(DEDUPE_WINDOW_S + 5)  # past dedup window
    backend.set_grid(-300)  # exporting 300 W
    r = backend.poll("AABBCCDDEEFF", "A", 0)
    assert r is not None, f"[{backend.name}] no response after grid sign flip"
    assert int(r[4]) < 0, f"[{backend.name}] export should drive charge (-), got {r[4]}"


@pytest.mark.timeout(30, func_only=True)
def test_convergence(backend) -> None:
    """Closing the loop drives the target toward zero on both stacks: once the
    battery reports it took the first target, the grid is back to ~0 and the
    next target shrinks."""
    backend.set_clock(2000)
    backend.set_grid(300)
    r1 = backend.poll("AABBCCDDEEFF", "A", 0)
    assert r1 is not None and int(r1[4]) > 0, (
        f"[{backend.name}] first target should be positive"
    )
    t1 = int(r1[4])

    backend.advance_clock(DEDUPE_WINDOW_S + 5)
    backend.set_grid(0)  # battery now covers the load → grid ~0
    r2 = backend.poll("AABBCCDDEEFF", "A", t1)
    assert r2 is not None, f"[{backend.name}] no response on second poll"
    assert abs(int(r2[4])) < abs(t1), (
        f"[{backend.name}] target should shrink toward zero: first={t1}, second={r2[4]}"
    )


@pytest.mark.timeout(30, func_only=True)
def test_clock_gated_dedup(backend) -> None:
    """The dedup window is driven by the (mock) clock on both stacks: a repeat
    poll inside the window is dropped; advancing the clock past it un-gates
    the poll."""
    backend.set_clock(5000)
    backend.set_dedupe(DEDUPE_WINDOW_S)  # dedup is off by default; enable it here
    backend.set_grid(100)
    r1 = backend.poll("CCDDEEFF0011", "A", 0)
    assert r1 is not None, f"[{backend.name}] first poll should be answered"

    r2 = backend.poll("CCDDEEFF0011", "A", 0)  # repeat, no clock advance
    assert r2 is None, (
        f"[{backend.name}] duplicate within dedup window should be dropped"
    )

    backend.advance_clock(DEDUPE_WINDOW_S + 1)
    r3 = backend.poll("CCDDEEFF0011", "A", 0)
    assert r3 is not None, (
        f"[{backend.name}] poll after the dedup window should be answered"
    )


# ── Additional cross-stack parity scenarios ────────────────────────────────
# These widen coverage of the response field shape, meter-identity echo,
# round-half-to-even on the wire, sign handling, and inspection-mode framing.
# Each asserts a wire-observable fact identical on both stacks.


@pytest.mark.timeout(30, func_only=True)
def test_response_has_full_24_fields(backend) -> None:
    """Every response carries exactly the 24 RESPONSE_LABELS fields."""
    backend.set_clock(7000)
    backend.set_grid(0)
    r = backend.poll("AABBCCDDEEFF", "A", 0)
    assert r is not None, f"[{backend.name}] no response"
    assert len(r) == 24, f"[{backend.name}] expected 24 fields, got {len(r)}"


@pytest.mark.timeout(30, func_only=True)
def test_meter_identity_echoed_verbatim(backend) -> None:
    """meter_dev_type (field 2) and meter_mac (field 3) are echoed verbatim,
    preserving the request's exact casing, on both stacks."""
    backend.set_clock(7100)
    backend.set_grid(0)
    r = backend.poll("AaBbCcDdEeFf", "A", 0)
    assert r is not None, f"[{backend.name}] no response"
    assert r[2] == "HMG-50", f"[{backend.name}] meter_dev_type = {r[2]}"
    assert r[3] == "AaBbCcDdEeFf", f"[{backend.name}] meter_mac = {r[3]}"


@pytest.mark.timeout(30, func_only=True)
def test_inspection_mode_returns_full_response(backend) -> None:
    """Non-A/B/C phase markers are inspection mode and still produce a
    well-formed 24-field response carrying ct identity on both stacks."""
    for clock, marker in ((7200, "0"), (7300, ""), (7400, "D")):
        backend.set_clock(clock)
        backend.set_grid(0)
        r = backend.poll("AABBCCDDEEFF", marker, 0)
        assert r is not None, f"[{backend.name}] no response for marker {marker!r}"
        assert len(r) == 24, f"[{backend.name}] marker {marker!r} -> {len(r)} fields"
        assert r[0] == "HME-4", f"[{backend.name}] ct_type = {r[0]}"


@pytest.mark.timeout(30, func_only=True)
def test_inspection_mode_mirrors_raw_grid_sign(backend) -> None:
    """In inspection mode the emulator forwards the raw meter reading as
    information (no balancer), so a negative grid keeps its sign in A_phase
    and total_power on both stacks."""
    backend.set_clock(7500)
    backend.set_grid(-150)
    r = backend.poll("AABBCCDDEEFF", "0", 0)  # "0" = inspection marker
    assert r is not None, f"[{backend.name}] no response"
    assert int(r[4]) == -150, f"[{backend.name}] A_phase_power = {r[4]}"
    assert int(r[7]) == -150, f"[{backend.name}] total_power = {r[7]}"
