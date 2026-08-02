"""The add-on image, started the way the Supervisor starts it.

Everything else about the add-on is tested in-process, which cannot see the
container itself: whether ``run.sh`` is launched with the container
environment, whether ``SUPERVISOR_TOKEN`` reaches the app, whether the venv is
on the path, whether the image survives the first minute instead of
restart-looping. This runs the real image against the stand-in Supervisor on a
container network — with no config file anywhere — and asks a battery for a
reading over UDP.

Requires Docker and a built image (default tag ``astrameter-addon:test``,
override with ``ASTRAMETER_ADDON_IMAGE``):

    docker build -f ha_addon/Dockerfile -t astrameter-addon:test .
    uv run pytest tests/test_addon_container.py

The tests skip when either is missing, so an ordinary test run is unaffected.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest
from _fake_supervisor import DEFAULT_TOKEN, GRID_SENSOR

from astrameter.ct002.protocol import RESPONSE_LABELS, build_payload, parse_request

IMAGE = os.environ.get("ASTRAMETER_ADDON_IMAGE", "astrameter-addon:test")
WATTS = 321.0
CT_MAC = "112233445566"
BATTERY_MAC = "AABBCCDDEEFF"
CT_UDP_PORT = 12345  # the emulator's fixed port inside the container
WEB_PORT = 52500
REPO_ROOT = Path(__file__).resolve().parents[1]
#: Where the repository is mounted inside the stand-in Supervisor's container.
#: It reads sibling files (the add-on manifest) relative to its own location,
#: so the two have to keep the layout they have here.
REPO_MOUNT = "/repo"
FAKE_SUPERVISOR = f"{REPO_MOUNT}/tests/_fake_supervisor.py"


def docker(*args: str, check: bool = True, timeout: float = 120) -> str:
    result = subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args)} failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result.stdout.strip()


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=60, check=False
        ).returncode
        == 0
    )


def image_available() -> bool:
    return bool(docker("image", "ls", "-q", IMAGE, check=False))


pytestmark = [
    pytest.mark.skipif(not docker_available(), reason="Docker is not available"),
    pytest.mark.skipif(
        docker_available() and not image_available(),
        reason=f"add-on image {IMAGE} not built",
    ),
    pytest.mark.timeout(600),
]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class RunningAddon:
    """The add-on image and its Supervisor, on a private container network."""

    def __init__(self, options: dict, tmp_path: Path) -> None:
        self.options = options
        self.tmp_path = tmp_path
        self.network = f"astrameter-test-{os.getpid()}-{int(time.time())}"
        self.supervisor = f"{self.network}-supervisor"
        self.addon = f"{self.network}-addon"
        self.web_port = free_port()
        self.udp_port = free_port()

    def start(self) -> None:
        options_file = self.tmp_path / "options.json"
        options_file.write_text(json.dumps(self.options), encoding="utf-8")

        docker("network", "create", self.network)
        # The stand-in Supervisor answers to the hostname the add-on uses. It
        # runs from the add-on image itself, which already has aiohttp. Both it
        # and the add-on manifest it serves are mounted at their repository
        # paths, because it finds the manifest relative to its own file.
        docker(
            "run",
            "-d",
            "--name",
            self.supervisor,
            "--network",
            self.network,
            "--network-alias",
            "supervisor",
            "-v",
            f"{REPO_ROOT / 'tests' / '_fake_supervisor.py'}:{FAKE_SUPERVISOR}:ro",
            "-v",
            f"{REPO_ROOT / 'ha_addon' / 'config.yaml'}:{REPO_MOUNT}/ha_addon/config.yaml:ro",
            "--entrypoint",
            "/app/.venv/bin/python",
            IMAGE,
            FAKE_SUPERVISOR,
            "--port",
            "80",
            "--watts",
            str(WATTS),
        )
        docker(
            "run",
            "-d",
            "--name",
            self.addon,
            "--network",
            self.network,
            "-e",
            f"SUPERVISOR_TOKEN={DEFAULT_TOKEN}",
            "-v",
            f"{options_file}:/data/options.json:ro",
            "-p",
            f"127.0.0.1:{self.web_port}:{WEB_PORT}",
            "-p",
            f"127.0.0.1:{self.udp_port}:{CT_UDP_PORT}/udp",
            IMAGE,
        )

    def stop(self) -> None:
        for name in (self.addon, self.supervisor):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", self.network, check=False)

    def logs(self) -> str:
        """Everything the container wrote — the app logs to stderr."""
        result = subprocess.run(
            ["docker", "logs", self.addon],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return result.stdout + result.stderr

    def running(self) -> bool:
        state = docker("inspect", "-f", "{{.State.Running}}", self.addon, check=False)
        return state == "true"

    def wait_for_health(self, timeout: float = 120) -> dict:
        """Block until the add-on's health endpoint answers."""
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if not self.running():
                raise AssertionError(f"add-on container exited:\n{self.logs()}")
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.web_port}/health", timeout=5
                ) as response:
                    return json.loads(response.read())
            except (urllib.error.URLError, OSError, ValueError) as exc:
                last_error = exc
                time.sleep(2)
        raise AssertionError(
            f"health endpoint never answered ({last_error}):\n{self.logs()}"
        )

    def poll_ct002(self, timeout: float = 2.0) -> dict[str, str]:
        """Poll the emulator over UDP the way a battery does."""
        request = build_payload(["HMG-50", BATTERY_MAC, "HME-4", CT_MAC, "A", "0"])
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(request, ("127.0.0.1", self.udp_port))
            data, _ = sock.recvfrom(2048)
        fields, error = parse_request(data)
        assert error is None, error
        return dict(zip(RESPONSE_LABELS, fields, strict=False))

    def poll_until(self, predicate, attempts: int = 60) -> dict[str, str]:
        reply: dict[str, str] = {}
        for _ in range(attempts):
            try:
                reply = self.poll_ct002()
            except OSError:  # not listening yet, or no reply
                time.sleep(1)
                continue
            if predicate(reply):
                return reply
            time.sleep(1)
        raise AssertionError(
            f"emulator never reported the expected value: {reply}\n{self.logs()}"
        )


@pytest.fixture
def addon(tmp_path) -> Iterator[RunningAddon]:
    """The add-on running from its image, configured only by add-on options."""
    instance = RunningAddon(
        {
            "power_input_alias": GRID_SENSOR,
            "power_output_alias": "",
            "device_types": "ct002",
            "throttle_interval": 0,
            "wait_for_next_message": False,
            "ct_mac": CT_MAC,
            "power_offset": "100",
            "active_control": False,  # relay mode: the reply carries the meter
            "log_level": "debug",
        },
        tmp_path,
    )
    try:
        instance.start()
        yield instance
    finally:
        instance.stop()


def test_the_image_starts_and_reports_healthy(addon):
    health = addon.wait_for_health()
    assert health.get("status") in ("healthy", "ok", "starting"), health


def test_the_container_serves_the_home_assistant_reading(addon):
    addon.wait_for_health()
    reply = addon.poll_until(lambda r: r["A_phase_power"] != "0")

    # 321 W from the stand-in Home Assistant plus the add-on's power_offset.
    assert reply["A_phase_power"] == "421"
    assert reply["meter_mac_code"].lower() == CT_MAC.lower()


def test_the_add_on_reads_its_options_and_the_supervisor(addon):
    addon.wait_for_health()
    logs = addon.logs()

    # The options were read (log level is one of them) and the Supervisor
    # answered with the add-on's own token.
    assert "started astrameter application" in logs
    assert "Resolved add-on slug for HA discovery" in logs
    assert "Home Assistant is ready" in logs
    assert "Home Assistant: authenticated" in logs
    # No config file is involved anywhere.
    assert "config.ini" not in logs


def test_the_container_keeps_running(addon):
    addon.wait_for_health()
    time.sleep(10)
    assert addon.running(), f"add-on container stopped:\n{addon.logs()}"
    # A restart loop would show the startup banner more than once.
    assert addon.logs().count("started astrameter application") == 1


def test_credentials_never_reach_the_log(tmp_path):
    """The add-on logs its own configuration; secrets must not come with it."""
    instance = RunningAddon(
        {
            "power_input_alias": GRID_SENSOR,
            "device_types": "ct002",
            "ct_mac": CT_MAC,
            "marstek_auto_register_ct_device": False,
            "marstek_mailbox": "user@example.com",
            "marstek_password": "hunter2-should-not-appear",
            "mqtt_uri": "mqtt://mqtt-user:mqtt-secret@broker:1883",
            "log_level": "debug",
        },
        tmp_path,
    )
    try:
        instance.start()
        instance.wait_for_health()
        logs = instance.logs()
    finally:
        instance.stop()

    assert "hunter2-should-not-appear" not in logs
    assert "mqtt-secret" not in logs
