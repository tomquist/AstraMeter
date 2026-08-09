"""Pytest wrapper around the host-gcc protocol parity gtest.

Regenerates the C++ test-vector header from the canonical JSON, then drives
cmake to build and run `host_protocol_test`. Skipped (not failed) if cmake or
a C++ toolchain isn't available on PATH, so contributors without them can
still run the rest of the suite.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent.parent.parent


def _have(tool: str) -> bool:
    return shutil.which(tool) is not None


pytestmark = pytest.mark.skipif(
    not (_have("cmake") and (_have("g++") or _have("clang++"))),
    reason="cmake and a C++ compiler are required for host-gcc protocol tests",
)


def _regen_vectors() -> None:
    subprocess.run(
        ["uv", "run", "python", str(HERE / "_gen_protocol_test_vectors.py")],
        check=True,
        cwd=REPO_ROOT,
    )


def _build_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("ct002_host_build")


@pytest.fixture(scope="module")
def cmake_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    _regen_vectors()
    build_dir = tmp_path_factory.mktemp("ct002_host_build")
    subprocess.run(
        ["cmake", "-S", str(HERE), "-B", str(build_dir), "-DCMAKE_BUILD_TYPE=Release"],
        check=True,
    )
    subprocess.run(
        ["cmake", "--build", str(build_dir), "--parallel"],
        check=True,
    )
    return build_dir


# A host gtest is milliseconds of work; a minute means it deadlocked. Bound it
# so the suite reports a failure instead of hanging the run.
GTEST_TIMEOUT_S = 60


HOST_GTESTS = [
    "host_protocol_test",
    "host_wrappers_test",
    "host_balancer_test",
    "host_marstek_responder_test",
    "host_cloud_reporting_test",
    "host_status_json_test",
    "host_controls_test",
    "host_write_slot_test",
]


@pytest.mark.parametrize("binary", HOST_GTESTS)
def test_host_gtest(cmake_build: Path, binary: str) -> None:
    """Run one gtest binary. Every target CMake builds needs a row here."""
    subprocess.run([str(cmake_build / binary)], check=True, timeout=GTEST_TIMEOUT_S)


def test_every_built_gtest_is_listed(cmake_build: Path) -> None:
    """A target added to CMakeLists but not above would be built and never run.

    That is not hypothetical: host_write_slot_test shipped in exactly that
    state. Compare the list against what the build actually produced rather
    than trusting the two to be edited together.
    """
    built = {path.name for path in cmake_build.glob("host_*_test") if path.is_file()}
    listed = set(HOST_GTESTS)
    assert built - listed == set(), "built but never run"
    assert listed - built == set(), "listed but not built"
