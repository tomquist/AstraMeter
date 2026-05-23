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


def test_host_protocol_parity(cmake_build: Path) -> None:
    subprocess.run([str(cmake_build / "host_protocol_test")], check=True)


def test_host_wrappers(cmake_build: Path) -> None:
    subprocess.run([str(cmake_build / "host_wrappers_test")], check=True)
