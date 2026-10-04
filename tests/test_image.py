"""Both images, checked from the inside.

The images run on a minimal hand-assembled root filesystem rather than a
distribution, so the code can need something the image leaves out — a
standard-library module, a shared library, a tool — without any unit test
noticing. ``image_smoke.py`` imports everything the app's source names and
exercises what a trimmed filesystem could break; these tests run it in each
image, and start the standalone image to check its user and health check.

Requires Docker and built images (skipped otherwise):

    docker build -t astrameter:test .
    docker build --target addon -t astrameter-addon:test .
    uv run pytest tests/test_image.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

IMAGE = os.environ.get("ASTRAMETER_IMAGE", "astrameter:test")
ADDON_IMAGE = os.environ.get("ASTRAMETER_ADDON_IMAGE", "astrameter-addon:test")
SMOKE = Path(__file__).with_name("image_smoke.py")


def docker(
    *args: str, check: bool = True, timeout: float = 120
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"docker {' '.join(args)} failed ({result.returncode}):\n"
            f"{result.stdout}\n{result.stderr}"
        )
    return result


def built(image: str) -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return bool(docker("image", "ls", "-q", image, check=False).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return False


def needs(image: str) -> pytest.MarkDecorator:
    return pytest.mark.skipif(not built(image), reason=f"image {image} not built")


pytestmark = pytest.mark.timeout(600)


@pytest.mark.parametrize(
    "image",
    [
        pytest.param(IMAGE, marks=needs(IMAGE), id="standalone"),
        pytest.param(ADDON_IMAGE, marks=needs(ADDON_IMAGE), id="addon"),
    ],
)
def test_everything_the_code_uses_exists_in_the_image(image: str) -> None:
    result = docker(
        "run", "--rm",
        "-v", f"{SMOKE}:/smoke.py:ro",
        "--entrypoint", "python",
        image, "/smoke.py",
        check=False, timeout=300,
    )  # fmt: skip
    assert result.returncode == 0, result.stdout + result.stderr


@needs(IMAGE)
def test_the_standalone_image_runs_as_astra_and_reports_healthy(tmp_path: Path) -> None:
    config = tmp_path / "config.ini"
    config.write_text(
        "[GENERAL]\nDEVICE_TYPE = ct002\nSKIP_POWERMETER_TEST = True\n\n"
        "[SCRIPT]\nCOMMAND = echo 100\n",
        encoding="utf-8",
    )
    config.chmod(0o644)
    container = docker(
        "run", "-d", "-v", f"{config}:/app/config.ini:ro", IMAGE
    ).stdout.strip()  # fmt: skip
    try:
        assert docker("exec", container, "id", "-u").stdout.strip() == "999"
        # Run the image's own HEALTHCHECK command until the app answers.
        test = json.loads(
            docker(
                "inspect", "-f", "{{json .Config.Healthcheck.Test}}", IMAGE
            ).stdout
        )  # fmt: skip
        assert test[0] == "CMD", test
        deadline = time.monotonic() + 60
        while True:
            result = docker("exec", container, *test[1:], check=False)
            if result.returncode == 0:
                break
            running = docker(
                "inspect", "-f", "{{.State.Running}}", container
            ).stdout.strip()  # fmt: skip
            logs = docker("logs", container, check=False)
            assert running == "true", f"container exited:\n{logs.stdout}{logs.stderr}"
            assert time.monotonic() < deadline, (
                f"health check never passed: {result.stderr}\n{logs.stdout}{logs.stderr}"
            )
            time.sleep(2)
    finally:
        docker("rm", "-f", container, check=False)
