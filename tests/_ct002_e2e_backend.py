"""Shared cross-backend e2e plumbing for the CT002 closed-loop sim tests.

Importable from the e2e suites in ``tests/`` (it isn't collected by pytest —
the filename doesn't match ``test_*``). Each suite defines its own tiny
autouse ``params=["python", "esphome"]`` fixture that sets
``ACTIVE_BACKEND`` here, and its harness reads it to pick the emulator:

  * ``python``  — in-process ``CT002`` over a real UDP socket, grid fed via
    the ``before_send`` hook, time from a controllable clock.
  * ``esphome`` — the compiled test-hooks host binary, driven over UDP with
    grid / clock / dedupe / balancer-config via the control channel.

See ``esphome/components/ct002/test_hooks.cpp`` for the control protocol.
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

# The test-hooks binary lives next to the ESPHome component tests.
_E2E_DIR = Path(__file__).parent / "components" / "ct002"
E2E_YAML = _E2E_DIR / "test.e2e.host.yaml"
E2E_BINARY = (
    _E2E_DIR
    / ".esphome"
    / "build"
    / "ct002-e2e-test"
    / ".pioenvs"
    / "ct002-e2e-test"
    / "program"
)
E2E_UDP_PORT = 12345
E2E_CONTROL_PORT = 12346

# Mutated by each suite's autouse fixture; read by the harnesses.
ACTIVE_BACKEND = "python"


def have_esphome() -> bool:
    return shutil.which("esphome") is not None


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def ensure_e2e_binary() -> None:
    if not E2E_BINARY.exists():
        subprocess.run(
            ["esphome", "compile", str(E2E_YAML)],
            check=True,
            cwd=_E2E_DIR.parent.parent,
        )


class HarnessClock:
    """Controllable clock. For the ESPHome backend an ``on_change`` callback
    pushes each new value to the binary's mock clock so the two stay in step."""

    def __init__(self, on_change=None) -> None:
        self._now = time.time()
        self._on_change = on_change

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds
        if self._on_change is not None:
            self._on_change(self._now)


class EsphomeSim:
    """Spawns the test-hooks host binary and drives it via the control channel."""

    def __init__(self) -> None:
        self._ctrl = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._ctrl.settimeout(2.0)
        self._proc: subprocess.Popen | None = None

    def _cmd(self, cmd: str) -> str:
        self._ctrl.sendto(cmd.encode(), ("127.0.0.1", E2E_CONTROL_PORT))
        reply = self._ctrl.recvfrom(512)[0].decode()
        assert reply.startswith("ok"), f"control command {cmd!r} failed: {reply!r}"
        return reply

    def spawn(self) -> None:
        ensure_e2e_binary()
        deadline = time.monotonic() + 5.0
        while port_in_use(E2E_UDP_PORT) and time.monotonic() < deadline:
            time.sleep(0.1)
        if port_in_use(E2E_UDP_PORT):
            raise RuntimeError(f"UDP port {E2E_UDP_PORT} still in use")
        self._proc = subprocess.Popen(
            [str(E2E_BINARY)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        deadline = time.monotonic() + 5.0
        while not port_in_use(E2E_UDP_PORT) and time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"e2e binary exited with code {self._proc.returncode}"
                )
            time.sleep(0.1)
        if not port_in_use(E2E_UDP_PORT):
            raise RuntimeError("e2e binary did not bind its UDP port")
        time.sleep(0.3)  # let the control socket finish binding

    def set_grid(self, l1: float, l2: float, l3: float) -> None:
        self._cmd(f"grid {l1} {l2} {l3}")

    def set_clock(self, seconds: float) -> None:
        self._cmd(f"clock_set {seconds}")

    def set_dedupe(self, ms: int) -> None:
        self._cmd(f"dedupe {ms}")

    def set_cfg(self, key: str, value: float) -> None:
        self._cmd(f"cfg {key} {value}")

    def force_rotation(self) -> None:
        self._cmd("force_rotation")

    def dump(self) -> dict:
        """Parse the `dump` reply into {smooth_target, consumers: {mac: {...}}}.

        Wire format: ok|smooth_target=<f>|<mac>,<phase>,<last_instructed>,
        <last_target>,<sat>,<active>,<manual>,<reported>,<last_intent>|..."""
        parts = self._cmd("dump").split("|")
        out: dict = {"smooth_target": 0.0, "consumers": {}}
        for tok in parts[1:]:
            if tok.startswith("smooth_target="):
                out["smooth_target"] = float(tok.split("=", 1)[1])
            elif tok:
                f = tok.split(",")
                out["consumers"][f[0]] = {
                    "phase": f[1],
                    "last_instructed": float(f[2]),
                    "last_target": float(f[3]),
                    "saturation": float(f[4]),
                    "active": f[5] == "1",
                    "manual": f[6] == "1",
                    "reported": float(f[7]),
                    "last_intent": float(f[8]),
                }
        return out

    def stop(self) -> None:
        self._ctrl.close()
        if self._proc is not None:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                self._proc.wait()


def find_free_ports(n: int = 2) -> list[int]:
    """Return *n* free port numbers (first UDP, rest TCP)."""
    types = [socket.SOCK_DGRAM] + [socket.SOCK_STREAM] * (n - 1)
    ports: list[int] = []
    socks: list[socket.socket] = []
    for i in range(n):
        s = socket.socket(socket.AF_INET, types[i])
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:
        s.close()
    return ports
