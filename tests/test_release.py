"""End-to-end tests for ``release.sh``.

The release script does a lot of git orchestration (branch → bump → merge →
tag → sync develop), so the highest-value test drives the whole thing against
a self-contained sandbox: a local *bare* repo stands in for ``origin`` (no
network), and ``uv`` / ``yq`` are replaced by tiny stubs on ``PATH`` so the
test needs nothing beyond ``git`` and ``bash``.

The stubs faithfully emulate the one operation the script asks of each tool
(``uv lock`` rewrites the version pin in ``uv.lock``; ``yq`` sets the
``version:`` key in ``ha_addon/config.yaml``), so assertions on the resulting
file contents stay meaningful.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_SH = REPO_ROOT / "release.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="release.sh tests need bash + git",
)


def _run(
    cmd: list[str], cwd: Path, env: dict | None = None
) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )


def _git(cwd: Path, *args: str) -> str:
    res = _run(["git", *args], cwd)
    assert res.returncode == 0, f"git {' '.join(args)} failed:\n{res.stderr}"
    return res.stdout


def _show(repo: Path, ref: str, path: str) -> str:
    return _git(repo, "show", f"{ref}:{path}")


def _make_stub_bin(tmp_path: Path) -> tuple[Path, Path]:
    """Write `uv` + `yq` stubs into a bin dir; return (bin_dir, uv_log)."""
    stub_bin = tmp_path / "stubbin"
    stub_bin.mkdir()
    uv_log = tmp_path / "uv_calls.log"

    uv = stub_bin / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'echo "uv $*" >> "$UV_STUB_LOG"\n'
        'if [ "${1:-}" = "lock" ]; then\n'
        "  ver=$(grep -E '^version = ' pyproject.toml | head -1 | "
        'sed -E \'s/.*"([^"]+)".*/\\1/\')\n'
        '  sed -i.bak -E "s/^version = \\"[^\\"]+\\"/version = \\"$ver\\"/" '
        "uv.lock && rm -f uv.lock.bak\n"
        "fi\n"
        "exit 0\n"
    )

    yq = stub_bin / "yq"
    # Emulates `yq eval --inplace '.version = "X"' FILE`: set the version: key.
    yq.write_text(
        "#!/usr/bin/env bash\n"
        'file="${!#}"\n'
        'val=$(printf "%s" "$*" | sed -E \'s/.*\\.version = "?([^"]*)"?.*/\\1/\')\n'
        'sed -i.bak -E "s/^(version:[[:space:]]*).*/\\1\\"$val\\"/" "$file" '
        '&& rm -f "$file.bak"\n'
        "exit 0\n"
    )

    for f in (uv, yq):
        f.chmod(0o755)
    return stub_bin, uv_log


_README = """\
# Project

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]
```
"""

_EXAMPLE = """\
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]
"""

_CHANGELOG = """\
# Changelog

## Next

- **Added** a shiny new thing.

## 1.0.0

- Initial release.
"""

_PYPROJECT = """\
[project]
name = "astrameter"
version = "1.0.0"
"""

_UVLOCK = """\
version = 1

[[package]]
name = "astrameter"
version = "1.0.0"
source = { editable = "." }
"""

_ADDON = 'name: AstraMeter\nversion: "next"\n'


def _init_sandbox(tmp_path: Path) -> Path:
    """Create origin.git + a work clone with main & develop branches.

    Returns the work-tree path (checked out on develop)."""
    origin = tmp_path / "origin.git"
    _run(["git", "init", "--bare", "-b", "main", str(origin)], tmp_path)

    work = tmp_path / "work"
    res = _run(["git", "clone", str(origin), str(work)], tmp_path)
    assert res.returncode == 0, res.stderr

    _git(work, "config", "user.email", "test@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "config", "commit.gpgsign", "false")
    _git(work, "config", "tag.gpgsign", "false")

    (work / "README.md").write_text(_README)
    (work / "esphome.example.yaml").write_text(_EXAMPLE)
    (work / "CHANGELOG.md").write_text(_CHANGELOG)
    (work / "pyproject.toml").write_text(_PYPROJECT)
    (work / "uv.lock").write_text(_UVLOCK)
    (work / "ha_addon").mkdir()
    (work / "ha_addon" / "config.yaml").write_text(_ADDON)
    shutil.copy(RELEASE_SH, work / "release.sh")

    _git(work, "add", "-A")
    _git(work, "commit", "-m", "initial")
    _git(work, "branch", "-M", "main")
    _git(work, "push", "-u", "origin", "main")
    _git(work, "checkout", "-b", "develop")
    _git(work, "push", "-u", "origin", "develop")
    return work


def test_release_happy_path(tmp_path: Path) -> None:
    work = _init_sandbox(tmp_path)
    stub_bin, uv_log = _make_stub_bin(tmp_path)

    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["UV_STUB_LOG"] = str(uv_log)

    res = _run(["bash", "release.sh", "9.9.9"], work, env)
    assert res.returncode == 0, f"release.sh failed:\n{res.stdout}\n{res.stderr}"

    # --- main: fully released state ------------------------------------
    assert 'version = "9.9.9"' in _show(work, "main", "pyproject.toml")
    assert 'version = "9.9.9"' in _show(work, "main", "uv.lock")
    assert 'version: "9.9.9"' in _show(work, "main", "ha_addon/config.yaml")
    main_changelog = _show(work, "main", "CHANGELOG.md")
    assert "## 9.9.9" in main_changelog
    assert "## Next" not in main_changelog
    assert "astrameter@9.9.9" in _show(work, "main", "README.md")
    assert "astrameter@9.9.9" in _show(work, "main", "esphome.example.yaml")

    # --- tag points at the released commit -----------------------------
    assert "9.9.9" in _git(work, "tag", "--list").split()
    assert "refs/tags/9.9.9" in _git(work, "ls-remote", "--tags", "origin")

    # --- develop: prepped for next dev cycle ---------------------------
    assert 'version: "next"' in _show(work, "develop", "ha_addon/config.yaml")
    assert "## Next" in _show(work, "develop", "CHANGELOG.md")
    assert "astrameter@develop" in _show(work, "develop", "README.md")
    assert "astrameter@develop" in _show(work, "develop", "esphome.example.yaml")
    # The release version carried over via the merge (script doesn't reset it).
    assert 'version = "9.9.9"' in _show(work, "develop", "pyproject.toml")

    # --- uv.lock was actually refreshed by `uv lock` -------------------
    assert "uv lock" in uv_log.read_text()


def test_release_rejects_non_semver(tmp_path: Path) -> None:
    work = _init_sandbox(tmp_path)
    stub_bin, uv_log = _make_stub_bin(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["UV_STUB_LOG"] = str(uv_log)

    res = _run(["bash", "release.sh", "9.9"], work, env)
    assert res.returncode != 0
    assert "semantic" in (res.stdout + res.stderr).lower()
    # Nothing should have been tagged or pushed.
    assert "9.9" not in _git(work, "tag", "--list").split()


def test_release_requires_changelog_next_entry(tmp_path: Path) -> None:
    work = _init_sandbox(tmp_path)
    # Remove the ## Next section so the pre-check fails.
    (work / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n- old\n")
    _git(work, "commit", "-am", "drop next")
    _git(work, "push", "origin", "develop")

    stub_bin, uv_log = _make_stub_bin(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["UV_STUB_LOG"] = str(uv_log)

    res = _run(["bash", "release.sh", "2.0.0"], work, env)
    assert res.returncode != 0
    assert "## Next" in (res.stdout + res.stderr)
    assert "2.0.0" not in _git(work, "tag", "--list").split()
