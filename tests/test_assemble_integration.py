"""Tests for the HACS zip-assembly helper (scripts/assemble_integration.py).

The script lives under ``scripts/`` (not an importable package), so it is loaded
by file path. The beta-version logic is correctness-critical for HACS update
detection, so it is covered here.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "assemble_integration.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("assemble_integration", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assemble = _load_module()


def test_beta_version_patch_bumps_and_appends_build() -> None:
    assert assemble.beta_version("2.1.2", 7) == "2.1.3b7"
    assert assemble.beta_version("2.1.2", "42") == "2.1.3b42"


def test_beta_version_sorts_below_any_real_next_release() -> None:
    """A beta targets the patch bump, so the eventual stable release (always
    strictly greater than the current version) sorts above every beta.

    HACS orders versions with AwesomeVersion, which follows PEP 440 modifier
    ordering for these cases; ``packaging.version`` (PEP 440) is used here as a
    faithful, dependency-light stand-in.
    """
    from packaging.version import Version

    current = "2.1.2"
    beta = assemble.beta_version(current, 99)
    # Beta is newer than the current stable (so beta users get the update)...
    assert Version(beta) > Version(current)
    # ...but older than the smallest possible next stable release and any larger
    # one, so beta users converge onto stable when it ships.
    for nxt in ("2.1.3", "2.1.4", "2.2.0", "3.0.0"):
        assert Version(beta) < Version(nxt)


def test_beta_version_tolerates_dev_suffix() -> None:
    assert assemble.beta_version("2.1.2.dev0", 3) == "2.1.3b3"


def test_beta_version_rejects_unparseable() -> None:
    with pytest.raises(ValueError):
        assemble.beta_version("not-a-version", 1)
