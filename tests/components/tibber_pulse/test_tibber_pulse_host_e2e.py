"""End-to-end: the compiled tibber_pulse host binary against a fake Pulse Bridge.

Runs the real firmware code path — ESPHome's http_request on the host
platform, the worker thread, the endpoint fallback, the SML decoder and the
sensors feeding ct002 — against an HTTP server that behaves like the bridge:
HTTP Basic auth, a binary SML body, and one of the two firmware generations'
telegram paths. What it checks is what the user sees in the log.

Skipped when the ESPHome CLI is not installed. Runs in CI in the
`ct002-host-e2e` job, which selects everything marked `esphome_e2e`.
"""

from __future__ import annotations

import base64
import os
import queue
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from astrameter.ct002.protocol import build_payload, parse_request

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent.parent
HOST_YAML = HERE / "test.host.yaml"
HOST_BINARY = (
    HERE
    / ".esphome"
    / "build"
    / "tibber-pulse-host-test"
    / ".pioenvs"
    / "tibber-pulse-host-test"
    / "program"
)

# Both fixed in test.host.yaml.
BRIDGE_PORT = 18181
CT002_PORT = 12346
PASSWORD = "AD56-54BA"
AUTH = "Basic " + base64.b64encode(f"admin:{PASSWORD}".encode()).decode()

pytestmark = [
    pytest.mark.esphome_e2e,
    pytest.mark.skipif(
        shutil.which("esphome") is None,
        reason="esphome CLI not on PATH; install with `uv tool install esphome`",
    ),
]


def _load_generator():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_gen_sml_vectors", HERE / "_gen_sml_vectors.py"
    )
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    # Registered first: its dataclasses look their module up by name.
    sys.modules[spec.name] = gen
    spec.loader.exec_module(gen)
    return gen


_GEN = _load_generator()
TELEGRAM_A = next(v for v in _GEN.vectors() if v.name == "per_phase_and_total").telegram
TELEGRAM_B = next(v for v in _GEN.vectors() if v.name == "feed_in_is_negative").telegram


class FakeBridge:
    """Serves a telegram on one of the two paths; 404s everything else."""

    def __init__(self) -> None:
        self.path = "/data.json"  # firmware before ~1794
        self.telegram = TELEGRAM_A
        self.delay = 0.0
        self.expected_auth = AUTH
        self.requests: list[tuple[str, str | None]] = []
        self._lock = threading.Lock()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                auth = self.headers.get("Authorization")
                with bridge._lock:
                    bridge.requests.append((self.path, auth))
                    serve_path, telegram, delay, expected = (
                        bridge.path,
                        bridge.telegram,
                        bridge.delay,
                        bridge.expected_auth,
                    )
                if delay:
                    time.sleep(delay)
                if auth != expected:
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="bridge"')
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path != f"{serve_path}?node_id=1":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(telegram)))
                self.end_headers()
                self.wfile.write(telegram)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def set(self, **kw) -> None:
        with self._lock:
            for k, v in kw.items():
                setattr(self, k, v)
            self.requests.clear()

    def paths(self) -> list[str]:
        with self._lock:
            return [p for p, _ in self.requests]

    def auths(self) -> set[str | None]:
        with self._lock:
            return {a for _, a in self.requests}


class LogReader:
    """The binary's stdout, line by line, ANSI colours stripped."""

    _ANSI = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, proc: subprocess.Popen) -> None:
        self.lines: queue.Queue[str] = queue.Queue()
        self.seen: list[str] = []
        assert proc.stdout is not None
        stream = proc.stdout

        def pump() -> None:
            for raw in iter(stream.readline, b""):
                self.lines.put(self._ANSI.sub("", raw.decode(errors="replace")))

        threading.Thread(target=pump, daemon=True).start()

    def wait_for(self, pattern: str, timeout: float = 15.0) -> re.Match[str]:
        rx = re.compile(pattern)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.2)
            except queue.Empty:
                continue
            self.seen.append(line)
            if m := rx.search(line):
                return m
        tail = "".join(self.seen[-40:])
        raise AssertionError(
            f"no log line matching {pattern!r} within {timeout}s:\n{tail}"
        )


def _state(sensor: str, watts: int) -> str:
    # sensor.cpp logs every publish as  'Name' >> 400 W
    return rf"'{sensor}' >> {watts}(\.0+)? W"


def _port_free(port: int, kind: int) -> bool:
    with socket.socket(socket.AF_INET, kind) as s:
        # As the fake bridge binds: a previous test's server leaves its port
        # in TIME_WAIT, which is no reason to skip.
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


@pytest.fixture(scope="module")
def host_binary() -> Path:
    if not HOST_BINARY.exists():
        subprocess.run(
            ["esphome", "compile", str(HOST_YAML)], check=True, cwd=REPO_ROOT
        )
    assert HOST_BINARY.exists(), f"expected host binary at {HOST_BINARY}"
    return HOST_BINARY


@pytest.fixture
def bridge() -> Iterator[FakeBridge]:
    if not _port_free(BRIDGE_PORT, socket.SOCK_STREAM):
        pytest.skip(f"TCP port {BRIDGE_PORT} is in use")
    fake = FakeBridge()
    fake.thread.start()
    yield fake
    fake.server.shutdown()
    fake.server.server_close()


@pytest.fixture
def firmware(host_binary: Path, bridge: FakeBridge) -> Iterator[LogReader]:
    if not _port_free(CT002_PORT, socket.SOCK_DGRAM):
        pytest.skip(f"UDP port {CT002_PORT} is in use")
    proc = subprocess.Popen(
        [str(host_binary)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        preexec_fn=os.setsid,
    )
    try:
        yield LogReader(proc)
    finally:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.wait()


def _ct002_poll() -> float | None:
    """One CT002 request to the firmware; the reply's round trip in seconds."""
    request = build_payload(
        ["HMG-50", "AABBCCDDEEFF", "HME-4", "112233445566", "A", "0"]
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(2.0)
        start = time.monotonic()
        s.sendto(request, ("127.0.0.1", CT002_PORT))
        try:
            reply, _ = s.recvfrom(1024)
        except TimeoutError:
            return None
        elapsed = time.monotonic() - start
    fields, err = parse_request(reply)
    assert err is None, err
    assert fields is not None
    return elapsed


@pytest.mark.timeout(120, func_only=True)
def test_reads_the_bridge_across_a_firmware_update(
    bridge: FakeBridge, firmware: LogReader
) -> None:
    # Old firmware: /node_data.json 404s, so the first poll falls back.
    firmware.wait_for(r"Bridge serves /data\.json, using it from now on")
    firmware.wait_for(_state("Grid L1", 400))
    firmware.wait_for(_state("Grid L2", 500))
    firmware.wait_for(_state("Grid L3", 334))
    firmware.wait_for(_state("Grid Total", 1234))
    paths = bridge.paths()
    assert paths[:2] == ["/node_data.json?node_id=1", "/data.json?node_id=1"]
    # Remembered: every later poll goes straight to the path that answered.
    time.sleep(1.5)
    later = bridge.paths()[2:]
    assert later and set(later) == {"/data.json?node_id=1"}
    assert bridge.auths() == {AUTH}

    # The bridge updates over the air: /data.json now 404s.
    bridge.set(path="/node_data.json", telegram=TELEGRAM_B)
    firmware.wait_for(r"Bridge serves /node_data\.json, using it from now on")
    firmware.wait_for(_state("Grid L1", -800))
    firmware.wait_for(_state("Grid Total", -2300))


@pytest.mark.timeout(120, func_only=True)
def test_slow_bridge_never_stalls_the_main_loop(
    bridge: FakeBridge, firmware: LogReader
) -> None:
    firmware.wait_for(_state("Grid L1", 400))
    # Answers now take 3 s against a 500 ms update interval (#551). The
    # request runs on the worker, so ct002 keeps answering at once meanwhile.
    bridge.set(delay=3.0)
    time.sleep(1.0)  # a request is certainly in flight now
    rtts = [_ct002_poll() for _ in range(5)]
    assert all(r is not None for r in rtts), rtts
    assert max(r for r in rtts if r is not None) < 0.5, rtts
    firmware.wait_for(r"Previous request still running; skipping this poll")
    # ...and the slow answers still arrive.
    bridge.set(delay=3.0, telegram=TELEGRAM_B)
    firmware.wait_for(_state("Grid L1", -800), timeout=20.0)


@pytest.mark.timeout(120, func_only=True)
def test_wrong_password_is_reported(bridge: FakeBridge, firmware: LogReader) -> None:
    # The bridge's code is not what the firmware was built with.
    bridge.set(expected_auth="Basic " + base64.b64encode(b"admin:another").decode())
    firmware.wait_for(r"Bridge refused the credentials \(HTTP 401\)")
    # A 401 says nothing about the firmware generation: no fallback.
    assert set(bridge.paths()) == {"/node_data.json?node_id=1"}
    bridge.set(expected_auth=AUTH)
    firmware.wait_for(_state("Grid L1", 400))
