"""The standalone image's privileged-port capability, in the built image.

The Shelly HTTP surface answers on port 80 by default because several
batteries have that port hardcoded, and the image runs as a non-root user. A
file capability on a copy of the interpreter is what reconciles those two — and
every part of that is invisible to an in-process test: whether the capability
survived the ``chown``, whether the copy still resolves the venv, and above all
whether a container that *drops* capabilities still starts.

That last one is the reason these tests exist. A file capability the container
is not permitted to use makes ``execve`` fail outright, so putting the
capability on the only interpreter would turn every hardened deployment —
``cap_drop: [ALL]`` in Compose, ``drop: ["ALL"]`` in Kubernetes, rootless
Podman — from "cannot bind port 80" into "cannot start at all".

Requires Docker and a built image (default tag ``astrameter:test``, override
with ``ASTRAMETER_IMAGE``):

    docker build -t astrameter:test .
    uv run pytest tests/test_docker_image.py

The tests skip when either is missing, so an ordinary test run is unaffected.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.timeout(600)

IMAGE = os.environ.get("ASTRAMETER_IMAGE", "astrameter:test")

CAPABLE = "/app/.venv/bin/python-cap"
PLAIN = "/app/.venv/bin/python"

#: Binding the port is only interesting under host networking. Docker sets
#: ``ip_unprivileged_port_start`` to 0 inside a bridge network's namespace, so
#: a bridge container binds port 80 with or without the capability — a test on
#: bridge would pass whatever the image did.
HOST_NET = ("--network", "host")

_BIND_PROBE = """
import socket, sys
sock = socket.socket()
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    sock.bind(("0.0.0.0", 80))
except OSError as exc:
    print("errno", exc.errno)
    sys.exit(0)
print("bound")
"""


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(["docker", "info"], capture_output=True, timeout=60).returncode
        == 0
    )


@pytest.fixture(scope="module", autouse=True)
def _requires_image() -> None:
    if not _docker_available():
        pytest.skip("Docker is not available")
    if (
        subprocess.run(
            ["docker", "image", "inspect", IMAGE], capture_output=True, timeout=60
        ).returncode
        != 0
    ):
        pytest.skip(f"image {IMAGE} is not built; see this module's docstring")


def run(*args: str, entrypoint: str | None = None) -> subprocess.CompletedProcess[str]:
    command = ["docker", "run", "--rm"]
    if entrypoint is not None:
        command += ["--entrypoint", entrypoint]
    command += [*args]
    return subprocess.run(command, capture_output=True, text=True, timeout=300)


def test_the_capability_survived_the_image_build() -> None:
    """Read as an extended attribute, because the tooling is not in the image.

    ``libcap2-bin`` is installed and purged inside one build step, so there is
    no ``getcap`` at runtime to ask.
    """
    result = run(
        *HOST_NET,
        IMAGE,
        "-c",
        (f"import os;print(os.getxattr({CAPABLE!r}, 'security.capability').hex())"),
        entrypoint=PLAIN,
    )
    assert result.returncode == 0, result.stderr
    # A v3 capability set with CAP_NET_BIND_SERVICE (bit 10) effective and
    # permitted. Asserting it is non-empty is the load-bearing part: `chown -R`
    # clears this attribute, so an ordering mistake in the Dockerfile leaves it
    # absent rather than wrong.
    assert result.stdout.strip()


def test_the_capable_interpreter_binds_the_privileged_port() -> None:
    result = run(*HOST_NET, IMAGE, "-c", _BIND_PROBE, entrypoint=CAPABLE)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bound"


def test_the_plain_interpreter_cannot() -> None:
    """The control for the test above: it must be the capability doing the work."""
    result = run(*HOST_NET, IMAGE, "-c", _BIND_PROBE, entrypoint=PLAIN)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "errno 13"


def test_the_copy_still_resolves_the_virtualenv() -> None:
    """A copy outside ``.venv/bin`` would import the system site-packages."""
    result = run(
        IMAGE,
        "-c",
        "import sys, astrameter; print(sys.prefix)",
        entrypoint=CAPABLE,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/app/.venv"


@pytest.mark.parametrize(
    "dropped", [["--cap-drop", "ALL"], ["--cap-drop", "NET_BIND_SERVICE"]]
)
def test_the_container_still_starts_with_capabilities_dropped(
    dropped: list[str],
) -> None:
    """The regression this design exists to avoid.

    The entrypoint probes the capability-bearing copy and falls back to the
    plain interpreter when exec of it is refused, so a hardened deployment
    still runs — it merely cannot bind a privileged port, which is a logged
    error the operator can act on.
    """
    result = run(
        *dropped,
        *HOST_NET,
        IMAGE,
        "-c",
        "print('started')",
        entrypoint="/usr/local/bin/docker-entrypoint.sh",
    )
    assert result.returncode == 0, result.stderr
    assert "started" in result.stdout


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        # The form `docs/installation/docker.md` documents.
        (["astrameter", "--help"], "usage"),
        # Flags only: the app is implied.
        (["--help"], "usage"),
    ],
)
def test_the_documented_invocations_still_work(args: list[str], expected: str) -> None:
    result = run(IMAGE, *args)
    assert result.returncode == 0, result.stderr
    assert expected in (result.stdout + result.stderr).lower()


def test_a_custom_command_is_executed_verbatim() -> None:
    """Anything that is not the app runs unchanged.

    Feeding an interpreter path through an interpreter would hand it its own
    ELF as a script, which is how an earlier draft of the entrypoint broke
    every debugging shell.
    """
    result = run(IMAGE, "sh", "-c", "echo shell-ok")
    assert result.returncode == 0, result.stderr
    assert "shell-ok" in result.stdout

    direct = run(IMAGE, CAPABLE, "-c", "print('direct-ok')")
    assert direct.returncode == 0, direct.stderr
    assert "direct-ok" in direct.stdout
