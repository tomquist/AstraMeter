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
import contextlib
import os
import random
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

    def __init__(self, saturation_detection: bool = True) -> None:
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
            saturation_detection=saturation_detection,
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


@contextlib.contextmanager
def _running_esphome_backend():
    """Launch the e2e host binary and yield an EsphomeBackend talking to it.

    Skips (via pytest.skip) when the esphome CLI is unavailable or the UDP
    port can't be acquired. Always tears the process group down on exit.
    """
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
    try:
        yield be
    finally:
        be.close()
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()


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

    with _running_esphome_backend() as be:
        yield be


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


@pytest.mark.timeout(30, func_only=True)
@pytest.mark.parametrize("phase,idx", [("A", 4), ("B", 5), ("C", 6)])
def test_phase_routing(backend, phase, idx) -> None:
    """A single battery's target lands only on the phase it reports.

    ``split_by_phase`` places the whole target on the reporting consumer's
    phase and exactly zero on the others — a wire fact that must hold
    identically on both stacks regardless of target magnitude or smoothing.
    """
    backend.set_clock(9000)
    backend.set_grid(300)  # importing → discharge (+)
    r = backend.poll("DDEEFF001122", phase, 0)
    assert r is not None, f"[{backend.name}] no response for phase {phase}"
    targets = {4: int(r[4]), 5: int(r[5]), 6: int(r[6])}
    assert targets[idx] > 0, (
        f"[{backend.name}] import should discharge on phase {phase}, got {targets[idx]}"
    )
    for other in (4, 5, 6):
        if other != idx:
            assert targets[other] == 0, (
                f"[{backend.name}] phase {'ABC'[other - 4]} should be 0, "
                f"got {targets[other]}"
            )


# Response field indices for the per-phase cross-talk power fields. These
# mirror RESPONSE_LABELS: A/B/C_chrg_power at 15..17, A/B/C_dchrg_power at
# 20..22 (see protocol.py / protocol.cpp).
_A_CHRG, _A_DCHRG = 15, 20


@pytest.mark.timeout(30, func_only=True)
def test_crosstalk_discharge_signals_other_battery(backend) -> None:
    """A discharging battery shows up as *discharge* in another's cross-talk.

    When battery X on phase A is instructed to discharge (grid import), a poll
    from a second battery Y on phase B must carry X's net instructed power in
    the phase-A discharge field and leave the phase-A charge field at zero.
    This exercises ``last_instructed_power`` + ``collect_reports_by_phase`` —
    the multi-battery cross-talk path — identically on both stacks.
    """
    backend.set_clock(11000)
    backend.set_grid(300)  # import → X should be told to discharge (+)
    rx = backend.poll("A1A1A1A1A1A1", "A", 0)
    assert rx is not None, f"[{backend.name}] no response to battery X"
    x_phase_a = int(rx[4])
    assert x_phase_a > 0, f"[{backend.name}] X should discharge on A, got {x_phase_a}"

    # Second battery on phase B polls; X's stored instruction feeds cross-talk.
    ry = backend.poll("B2B2B2B2B2B2", "B", 0)
    assert ry is not None, f"[{backend.name}] no response to battery Y"
    assert int(ry[_A_DCHRG]) > 0, (
        f"[{backend.name}] X's discharge should appear in A_dchrg_power, "
        f"got {ry[_A_DCHRG]}"
    )
    assert int(ry[_A_CHRG]) == 0, (
        f"[{backend.name}] A_chrg_power should be zero while X discharges, "
        f"got {ry[_A_CHRG]}"
    )


@pytest.mark.timeout(30, func_only=True)
def test_crosstalk_charge_signals_other_battery(backend) -> None:
    """A charging battery shows up as *charge* (negative) in cross-talk.

    Mirror of the discharge case under grid export: X on phase A is instructed
    to charge, so a poll from Y on phase B must carry a negative phase-A charge
    value and a zero phase-A discharge value on both stacks.
    """
    backend.set_clock(13000)
    backend.set_grid(-300)  # export → X should be told to charge (-)
    rx = backend.poll("C3C3C3C3C3C3", "A", 0)
    assert rx is not None, f"[{backend.name}] no response to battery X"
    x_phase_a = int(rx[4])
    assert x_phase_a < 0, f"[{backend.name}] X should charge on A, got {x_phase_a}"

    ry = backend.poll("D4D4D4D4D4D4", "B", 0)
    assert ry is not None, f"[{backend.name}] no response to battery Y"
    assert int(ry[_A_CHRG]) < 0, (
        f"[{backend.name}] X's charge should appear in A_chrg_power, got {ry[_A_CHRG]}"
    )
    assert int(ry[_A_DCHRG]) == 0, (
        f"[{backend.name}] A_dchrg_power should be zero while X charges, "
        f"got {ry[_A_DCHRG]}"
    )


# ── Direct dual-backend wire comparison ────────────────────────────────────
#
# The strongest parity guard: drive the *same* randomized poll sequence
# through the in-process Python emulator AND the live ESPHome binary, and
# assert the response fields agree field-by-field. Where the parametrized
# `backend` scenarios above check that each stack independently satisfies a
# property, this catches any wire-level divergence between the two stacks.

# Wire fields that legitimately differ run-to-run and are excluded from the
# byte-for-byte comparison: info_idx is a free-running per-stack counter (13)
# and wifi_rssi is a per-build constant (12).
_VOLATILE_FIELDS = frozenset({12, 13})


def _diff_fields(py_fields, esp_fields) -> list[str]:
    diffs: list[str] = []
    n = max(len(py_fields), len(esp_fields))
    for i in range(n):
        if i in _VOLATILE_FIELDS:
            continue
        pv = py_fields[i] if i < len(py_fields) else "<missing>"
        ev = esp_fields[i] if i < len(esp_fields) else "<missing>"
        # Numeric-aware compare so "-0" vs "0" and "007" vs "7" don't trip it.
        try:
            if int(pv) == int(ev):
                continue
        except (TypeError, ValueError):
            if pv == ev:
                continue
        diffs.append(f"field[{i}]: python={pv!r} esphome={ev!r}")
    return diffs


@pytest.mark.timeout(60, func_only=True)
def test_python_esphome_wire_identical() -> None:
    """Python emulator and ESPHome binary emit identical response wire fields.

    A shared, seeded sequence of multi-battery polls (varying phase, reported
    power and grid direction, with the clock advancing past the dedup window)
    is replayed against both stacks; every non-volatile response field must
    match. This is the end-to-end imparity detector across the whole request
    handler: parsing, balancer target, phase split, cross-talk fields and
    response framing.

    Saturation detection is disabled on both stacks for this byte-for-byte
    comparison. The saturation score is a long-running EMA accumulated across
    every poll; Python carries it in float64 while the ESPHome port uses
    float32, so over a long sequence the two scores drift by sub-ULP amounts
    that can eventually straddle a participation threshold and amplify into a
    whole-watt target difference — a documented float-vs-double artifact, not
    a wire-format divergence. The EMA path itself is covered (at integer-watt
    tolerance) by the differential fuzzer in test_balancer_parity.py.
    """
    py = PythonBackend(saturation_detection=False)
    try:
        with _running_esphome_backend() as esp:
            # Match the Python backend: rebuild the binary's balancer with
            # saturation off (the cfg control command is test-hooks only).
            esp._cmd("cfg saturation_enabled 0")
            rng = random.Random(20260531)
            macs = ["AAAA00000001", "BBBB00000002", "CCCC00000003"]
            phases = ["A", "B", "C"]
            clock = 20000
            for step in range(60):
                clock += DEDUPE_WINDOW_S + 5  # always clear the dedup window
                py.set_clock(clock)
                esp.set_clock(clock)
                grid = rng.choice([-901, -300, -50, 0, 100, 300, 450, 901, 1500])
                py.set_grid(grid)
                esp.set_grid(grid)
                mac = rng.choice(macs)
                phase = rng.choice(phases)
                reported = rng.choice([-200, -50, 0, 50, 200])

                r_py = py.poll(mac, phase, reported)
                r_esp = esp.poll(mac, phase, reported)
                assert (r_py is None) == (r_esp is None), (
                    f"step {step}: one stack answered and the other didn't "
                    f"(python={r_py is not None}, esphome={r_esp is not None})"
                )
                if r_py is None:
                    continue
                diffs = _diff_fields(r_py, r_esp)
                assert not diffs, (
                    f"step {step} (mac={mac} phase={phase} power={reported} "
                    f"grid={grid}): wire mismatch:\n" + "\n".join(diffs)
                )
    finally:
        py.close()
