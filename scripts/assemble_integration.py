#!/usr/bin/env python3
"""Assemble the shippable HACS integration zip.

HACS installs the integration by extracting a release asset (``astrameter.zip``,
declared via ``zip_release``/``filename`` in ``hacs.json``) into
``custom_components/astrameter/``. The ``astrameter`` core package is **vendored**
into the integration at build time (copied from ``src/astrameter``) rather than
committed, so the zip is self-contained and needs no pip install of first-party
code.

Usage:
    python scripts/assemble_integration.py [--version X.Y.Z] [--output PATH]
    python scripts/assemble_integration.py --vendor-only   # populate the
        gitignored custom_components/astrameter/astrameter/ for local dev/tests
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INTEGRATION_DIR = REPO_ROOT / "custom_components" / "astrameter"
CORE_PKG = REPO_ROOT / "src" / "astrameter"
VENDOR_DIR = INTEGRATION_DIR / "astrameter"

# Excluded from both the vendored copy and the integration glue.
_EXCLUDE_NAMES = {"__pycache__", ".mypy_cache", ".pytest_cache"}


def _pyproject_version() -> str:
    text = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else "0.0.0"


def beta_version(base: str, build: int | str) -> str:
    """Compute the beta (pre-release) version string for a ``develop`` build.

    The beta targets the *smallest possible* next release — the **patch bump** of
    the current ``pyproject`` version — so HACS update detection converges
    cleanly. ``release.sh`` enforces every real release ``> current pyproject``,
    so any eventual stable version is ``>= <next>`` and therefore sorts **above**
    every ``<next>bN`` (PEP 440 / AwesomeVersion: ``2.1.3b7 < 2.1.3 < 2.2.0``),
    while ``<next>bN > current stable`` so beta users see the update. ``build``
    (``github.run_number``) is a monotonic counter so newer betas sort highest.
    """
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", base)
    if not m:
        raise ValueError(f"Cannot parse a MAJOR.MINOR.PATCH version from {base!r}")
    major, minor, patch = (int(m.group(i)) for i in (1, 2, 3))
    return f"{major}.{minor}.{patch + 1}b{build}"


def _ignore(_dir: str, names: list[str]) -> set[str]:
    return {n for n in names if n in _EXCLUDE_NAMES or n.endswith((".pyc", "_test.py"))}


def vendor_core(dest: Path) -> None:
    """Copy the core ``astrameter`` package into ``dest`` (excluding tests)."""
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(CORE_PKG, dest, ignore=_ignore)


def stage_integration(staging: Path, version: str) -> None:
    """Stage the integration glue + vendored core into ``staging``."""
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    # Glue files (everything in the integration dir except a stale vendored copy).
    for item in INTEGRATION_DIR.iterdir():
        if item.name in _EXCLUDE_NAMES or item.name == "astrameter":
            continue
        if item.is_dir():
            shutil.copytree(item, staging / item.name, ignore=_ignore)
        else:
            shutil.copy2(item, staging / item.name)
    # Vendored core package.
    vendor_core(staging / "astrameter")
    # Stamp the manifest version.
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = version
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def build_zip(staging: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(staging.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staging).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version", default=None, help="Version to stamp (default: pyproject)"
    )
    parser.add_argument(
        "--output",
        default=str(REPO_ROOT / "dist" / "astrameter.zip"),
        help="Output zip path",
    )
    parser.add_argument(
        "--vendor-only",
        action="store_true",
        help="Only populate custom_components/astrameter/astrameter/ for dev/tests",
    )
    parser.add_argument(
        "--print-beta-version",
        metavar="BUILD",
        default=None,
        help="Print the <next>b<BUILD> beta version for the current pyproject "
        "version and exit (used by the develop beta-release workflow)",
    )
    args = parser.parse_args()

    if args.print_beta_version is not None:
        print(beta_version(_pyproject_version(), args.print_beta_version))
        return 0

    if args.vendor_only:
        vendor_core(VENDOR_DIR)
        print(f"Vendored core into {VENDOR_DIR.relative_to(REPO_ROOT)}")
        return 0

    version = args.version or _pyproject_version()
    staging = REPO_ROOT / "dist" / "astrameter"
    stage_integration(staging, version)
    output = Path(args.output)
    build_zip(staging, output)
    try:
        shown = output.relative_to(REPO_ROOT)
    except ValueError:
        shown = output
    print(f"Built {shown} (version {version})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
