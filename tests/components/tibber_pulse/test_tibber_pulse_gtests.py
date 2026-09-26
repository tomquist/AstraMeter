"""Pytest wrapper around the tibber_pulse host gtests.

Builds and runs the gtests with cmake, and checks that the committed SML
test-vector header still matches what the Python decoder makes of its
telegrams. The gtests skip (not fail) without cmake and a C++ compiler on PATH;
the header check always runs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent

# The staleness check below needs no toolchain, so this is not module-wide.
needs_toolchain = pytest.mark.skipif(
    not (shutil.which("cmake") and (shutil.which("g++") or shutil.which("clang++"))),
    reason="cmake and a C++ compiler are required for the tibber_pulse host gtests",
)

# A host gtest is milliseconds of work; a minute means it hung.
GTEST_TIMEOUT_S = 60

HOST_GTESTS = [
    "host_sml_power_test",
    "host_bridge_endpoint_test",
]


@pytest.fixture(scope="module")
def cmake_build(tmp_path_factory: pytest.TempPathFactory) -> Path:
    build_dir = tmp_path_factory.mktemp("tibber_pulse_host_build")
    configure = ["cmake", "-S", str(HERE), "-B", str(build_dir)]
    configure.append("-DCMAKE_BUILD_TYPE=Release")
    # A local googletest checkout, for where the release tarball can't be
    # fetched (a TLS-intercepting proxy answers it with 403).
    if local := os.environ.get("FETCHCONTENT_SOURCE_DIR_GOOGLETEST"):
        configure.append(f"-DFETCHCONTENT_SOURCE_DIR_GOOGLETEST={local}")
    subprocess.run(configure, check=True)
    subprocess.run(["cmake", "--build", str(build_dir), "--parallel"], check=True)
    return build_dir


@needs_toolchain
@pytest.mark.parametrize("binary", HOST_GTESTS)
def test_host_gtest(cmake_build: Path, binary: str) -> None:
    """Run one gtest binary. Every target CMake builds needs a row here."""
    subprocess.run([str(cmake_build / binary)], check=True, timeout=GTEST_TIMEOUT_S)


@needs_toolchain
def test_every_built_gtest_is_listed(cmake_build: Path) -> None:
    """A target added to CMakeLists but not above would be built and never run."""
    built = {path.name for path in cmake_build.glob("host_*_test") if path.is_file()}
    assert built == set(HOST_GTESTS)


def test_committed_vectors_are_current() -> None:
    """The header is generated, but committed so a bare cmake build works.

    Compare it with a fresh render rather than trusting it was regenerated
    after the last change to the generator or to the Python decoder.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_gen_sml_vectors", HERE / "_gen_sml_vectors.py"
    )
    assert spec is not None and spec.loader is not None
    gen = importlib.util.module_from_spec(spec)
    # Registered first: its dataclasses look their module up by name.
    sys.modules[spec.name] = gen
    spec.loader.exec_module(gen)
    assert (HERE / "host_sml_vectors.h").read_text() == gen.render(), (
        "host_sml_vectors.h is stale: run "
        "`uv run python tests/components/tibber_pulse/_gen_sml_vectors.py`"
    )
