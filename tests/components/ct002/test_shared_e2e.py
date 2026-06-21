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
        self._meter_unavailable = False
        self.ct002 = CT002(
            udp_port=UDP_PORT,  # unused: we never start() a real socket
            ct_mac="",  # mirror mode, like the e2e YAML
            active_control=True,
            fair_distribution=True,
            clock=self._clock,
            reset_fn=None,
            dedupe_time_window=0.0,  # off by default; set_dedupe() toggles it
            saturation_detection=saturation_detection,
            consumer_ttl=100000,  # fixed, matching test.e2e.host.yaml
        )

        async def _before_send(_addr, _fields=None, _consumer_id=None):
            if self._meter_unavailable:
                # Mirror a powermeter that detects its own staleness and
                # raises (HomeAssistant / HomeWizard). This is the #403 trigger.
                raise ValueError("powermeter unavailable (test)")
            return list(self._grid)

        self.ct002.before_send = _before_send

    def set_grid(self, l1: float, l2: float = 0.0, l3: float = 0.0) -> None:
        self._grid = [float(l1), float(l2), float(l3)]

    def set_meter_unavailable(self, unavailable: bool = True) -> None:
        # The Python counterpart of the ESPHome `sensor_stale` hook: the next
        # poll's before_send raises instead of returning a grid reading.
        self._meter_unavailable = unavailable

    def set_clock(self, seconds: float) -> None:
        self._clock._now = float(seconds)

    def advance_clock(self, seconds: float) -> None:
        self._clock.advance(float(seconds))

    def set_dedupe(self, window_s: float) -> None:
        # Mirrors the binary's runtime `dedupe <ms>` control.
        self.ct002._dedup._window = float(window_s)

    def set_active_control(self, enabled: bool) -> None:
        # Mirrors the binary's `cfg active_control` control command.
        self.ct002.active_control = bool(enabled)

    def set_consumer_ttl(self, seconds: float | None) -> None:
        # None = adaptive eviction (the production default); mirrors the
        # binary's `cfg consumer_ttl` control command (-1 = adaptive).
        self.ct002.consumer_ttl = seconds

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

    def set_active_control(self, enabled: bool) -> None:
        self._cmd(f"cfg active_control {1 if enabled else 0}")

    def set_consumer_ttl(self, seconds: float | None) -> None:
        self._cmd(f"cfg consumer_ttl {-1 if seconds is None else seconds}")

    def set_sensor_stale(self) -> None:
        # Back-date the sensor stamps so the next read reports the grid sensor
        # unavailable (SensorBackedPowermeter returns {} -> handler uses [0,0,0]).
        self._cmd("sensor_stale")

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
    """Closing the loop drives the target toward zero on both stacks.

    Each poll the battery reports the output it has integrated so far and the
    grid reflects the *remaining* imbalance (a fixed load minus that output) —
    a physically consistent loop, so the adaptive grid-state predictor sees the
    same monotonic catch-up the real meter would.  With a fixed load both the
    grid and the per-poll correction must drive toward zero.
    """
    load = 300
    backend.set_clock(2000)
    backend.set_grid(load)
    r1 = backend.poll("AABBCCDDEEFF", "A", 0)
    assert r1 is not None and int(r1[4]) > 0, (
        f"[{backend.name}] first target should be positive"
    )
    t1 = int(r1[4])

    # Drive the closed loop: the battery integrates each delta into its reported
    # output, and the grid is load - output.
    reported = t1
    last = t1
    for _ in range(12):
        backend.advance_clock(DEDUPE_WINDOW_S + 5)
        backend.set_grid(load - reported)
        r = backend.poll("AABBCCDDEEFF", "A", reported)
        assert r is not None, f"[{backend.name}] no response while converging"
        last = int(r[4])
        reported += last
    assert abs(last) < abs(t1), (
        f"[{backend.name}] per-poll correction should shrink toward zero: "
        f"first={t1}, last={last}"
    )
    assert abs(load - reported) < 25, (
        f"[{backend.name}] loop should converge to ~0 grid: "
        f"load={load}, battery={reported}"
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


# Bucket field indices beyond A/B/C: x_chrg=14/x_dchrg=19 (unassigned /
# inspection), ABC_chrg_nb=11, ABC_chrg=18/ABC_dchrg=23 (combined, phase "D").
_X_CHRG, _X_DCHRG = 14, 19
_ABC_NB, _ABC_CHRG, _ABC_DCHRG = 11, 18, 23


@pytest.mark.timeout(30, func_only=True)
def test_relay_buckets_carry_reported_power(backend) -> None:
    """Relay mode forwards each battery's *reported* power in the cross-talk
    buckets — not reported+grid — matching the real CT (issue #457)."""
    backend.set_clock(15000)
    backend.set_active_control(False)
    backend.set_grid(300)  # nonzero grid: the pre-#457 bug added this in
    rx = backend.poll("A5A5A5A5A5A5", "A", -100)
    assert rx is not None, f"[{backend.name}] no response to battery X"

    ry = backend.poll("B6B6B6B6B6B6", "B", 0)
    assert ry is not None, f"[{backend.name}] no response to battery Y"
    assert int(ry[_A_CHRG]) == -100, (
        f"[{backend.name}] relay A_chrg_power must be X's reported -100 "
        f"(not -100+300), got {ry[_A_CHRG]}"
    )
    assert int(ry[_A_DCHRG]) == 0, (
        f"[{backend.name}] A_dchrg_power should stay 0, got {ry[_A_DCHRG]}"
    )


@pytest.mark.timeout(30, func_only=True)
def test_inspection_reporter_lands_in_x_bucket(backend) -> None:
    """An inspection ('0') reporter populates the x bucket and is excluded
    from phase A's count/aggregate (issue #460)."""
    backend.set_clock(16000)
    backend.set_active_control(False)
    backend.set_grid(0)
    rx = backend.poll("C7C7C7C7C7C7", "0", -200)  # mid phase-detection
    assert rx is not None, f"[{backend.name}] no response to inspecting battery"

    ry = backend.poll("D8D8D8D8D8D8", "A", 50)
    assert ry is not None, f"[{backend.name}] no response to battery Y"
    assert int(ry[_X_CHRG]) == -200, (
        f"[{backend.name}] inspecting battery's -200 must land in x_chrg_power, "
        f"got {ry[_X_CHRG]}"
    )
    assert int(ry[8]) == 1, (
        f"[{backend.name}] A_chrg_nb must count only Y (not the inspecting "
        f"battery), got {ry[8]}"
    )
    assert int(ry[_A_DCHRG]) == 50, (
        f"[{backend.name}] A_dchrg_power should carry only Y's reported 50, "
        f"got {ry[_A_DCHRG]}"
    )


@pytest.mark.timeout(30, func_only=True)
def test_combined_phase_d_lands_in_abc_bucket(backend) -> None:
    """A combined-mode (phase 'D') reporter populates the ABC bucket and
    ABC_chrg_nb instead of phase A (issue #460)."""
    backend.set_clock(17000)
    backend.set_active_control(False)
    backend.set_grid(0)
    rx = backend.poll("E9E9E9E9E9E9", "D", 300)
    assert rx is not None, f"[{backend.name}] no response to combined battery"

    ry = backend.poll("FAFAFAFAFAFA", "A", 0)
    assert ry is not None, f"[{backend.name}] no response to battery Y"
    assert int(ry[_ABC_NB]) == 1, (
        f"[{backend.name}] ABC_chrg_nb must count the phase-D battery, got {ry[_ABC_NB]}"
    )
    assert int(ry[_ABC_DCHRG]) == 300, (
        f"[{backend.name}] phase-D battery's 300 must land in ABC_dchrg_power, "
        f"got {ry[_ABC_DCHRG]}"
    )
    assert int(ry[8]) == 1, (
        f"[{backend.name}] A_chrg_nb must count only Y itself (not the "
        f"phase-D battery), got {ry[8]}"
    )


@pytest.mark.timeout(30, func_only=True)
def test_adaptive_eviction_drops_silent_battery_from_relay_count(backend) -> None:
    """With the default adaptive TTL, a battery that misses ~2 of its own
    poll cycles drops out of the relay count/aggregate (issue #462)."""
    backend.set_clock(18000)
    backend.set_active_control(False)
    backend.set_consumer_ttl(None)  # adaptive (the production default)
    backend.set_grid(0)

    # Establish a ~10 s cadence for both batteries.
    for step in range(3):
        backend.set_clock(18000 + step * 10)
        assert backend.poll("ABABABABABAB", "A", 100) is not None
        assert backend.poll("CDCDCDCDCDCD", "A", 50) is not None

    # Y goes silent; X polls again 25 s later (> 2x the 10 s cadence).
    backend.set_clock(18020 + 25)
    rx = backend.poll("ABABABABABAB", "A", 100)
    assert rx is not None, f"[{backend.name}] no response to battery X"
    assert int(rx[8]) == 1, (
        f"[{backend.name}] silent battery should be evicted from A_chrg_nb, got {rx[8]}"
    )
    assert int(rx[_A_DCHRG]) == 100, (
        f"[{backend.name}] A_dchrg_power should drop the silent battery's 50, "
        f"got {rx[_A_DCHRG]}"
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
            # "0" (inspection → x bucket) and "D" (combined → ABC bucket)
            # exercise the non-A/B/C aggregation paths added for issue #460.
            phases = ["A", "B", "C", "0", "D"]
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


@pytest.mark.timeout(60, func_only=True)
def test_meter_unavailable_zero_delta_parity() -> None:
    """Both stacks respond with a zero-delta hold when the grid meter is
    unavailable (issue #403).

    Python's powermeter hook raises (the HomeAssistant/HomeWizard staleness
    error); ESPHome's sensor is back-dated past its freshness window so
    ``SensorBackedPowermeter`` returns ``{}``. Both must then answer the
    battery with a zero per-phase adjustment so it holds its current output —
    NOT a delta re-derived from the last-known reading.

    This is the cross-stack regression guard for the fix: before it, the Python
    handler kept the stale ``consumer.values`` (the seeded 300 W) and re-ran the
    balancer on them, so the response would carry that stale target instead of a
    hold — diverging from ESPHome's ``[0, 0, 0]`` path.

    A single battery is used so the hold is exactly zero: with one consumer the
    balancer's inter-battery equalization term is zero, so a zero grid yields a
    zero target on every phase. (Saturation is disabled on both stacks — the
    documented float32/float64 byte-parity guard.)
    """
    py = PythonBackend(saturation_detection=False)
    try:
        with _running_esphome_backend() as esp:
            esp._cmd("cfg saturation_enabled 0")
            mac = "AAAA00000001"
            phases = ["A", "B", "C", "0"]  # "0" = inspection / self-diagnosis
            clock = 30000

            # Seed both stacks with an identical NON-zero reading and a warm
            # poll, so prior consumer state is in lockstep and the pre-fix
            # stale-cache path would visibly diverge from a true hold.
            clock += DEDUPE_WINDOW_S + 5
            py.set_clock(clock)
            esp.set_clock(clock)
            py.set_grid(300)
            esp.set_grid(300)
            assert py.poll(mac, "A", 0) is not None
            assert esp.poll(mac, "A", 0) is not None

            # Meter goes unavailable on both stacks.
            py.set_meter_unavailable(True)
            esp.set_sensor_stale()

            for step, (phase, reported) in enumerate(
                (p, r) for p in phases for r in (-200, 0, 150)
            ):
                clock += DEDUPE_WINDOW_S + 5
                py.set_clock(clock)
                esp.set_clock(clock)
                # ESPHome reads real millis() for freshness, so the clock-driven
                # dedup advance doesn't refresh the sensor; the back-dated stamp
                # from set_sensor_stale() persists until a `grid` command.

                r_py = py.poll(mac, phase, reported)
                r_esp = esp.poll(mac, phase, reported)
                assert r_py is not None and r_esp is not None, (
                    f"step {step}: a stack failed to answer "
                    f"(python={r_py is not None}, esphome={r_esp is not None})"
                )
                # Zero-delta hold: per-phase A/B/C targets + total are all 0.
                assert [r_py[i] for i in (4, 5, 6, 7)] == ["0", "0", "0", "0"], (
                    f"[python] step {step} (phase={phase} power={reported}): "
                    f"expected zero hold, got {r_py[4:8]}"
                )
                # And the two stacks agree field-by-field.
                diffs = _diff_fields(r_py, r_esp)
                assert not diffs, (
                    f"step {step} (phase={phase} power={reported}): "
                    "wire mismatch under meter outage:\n" + "\n".join(diffs)
                )
    finally:
        py.close()
