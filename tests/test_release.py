"""End-to-end tests for ``release.sh``.

The release script does a lot of git orchestration (branch → bump → merge →
tag → open the develop sync PR), so the highest-value test drives the whole
thing against a self-contained sandbox: a local *bare* repo stands in for
``origin`` (no network), and ``uv`` / ``yq`` / ``gh`` are replaced by tiny stubs
on ``PATH`` so the test needs nothing beyond ``git`` and ``bash``.

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
    """Write `uv` + `yq` + `gh` stubs into a bin dir; return (bin_dir, uv_log).

    `gh` calls are logged to the same file as `uv` calls."""
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

    gh = stub_bin / "gh"
    gh.write_text('#!/usr/bin/env bash\necho "gh $*" >> "$UV_STUB_LOG"\nexit 0\n')

    for f in (uv, yq, gh):
        f.chmod(0o755)
    return stub_bin, uv_log


_README = """\
# Project

No external_components snippet lives here anymore — see docs/installation/esphome.md.
"""

# ESPHome installation doc: carries the canonical copy-paste external_components
# snippet (moved out of the README), so it must be pinned/reset on release.
_DOCS_INSTALL_ESPHOME = """\
# ESPHome installation

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

# Per-meter ESPHome reference doc: carries *several* copy-paste snippets, each
# with its own @develop ref. All of them must be pinned/reset alongside the
# other snippet files, so the fixture deliberately has more than one.
_DOCS_ESPHOME = """\
# ESPHome power meters

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]
```

```yaml
external_components:
  - source: github://tomquist/astrameter@develop
    components: [ct002]
```
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

[tool.example]
fixed = false
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
    (work / "docs").mkdir()
    (work / "docs" / "esphome-powermeters.md").write_text(_DOCS_ESPHOME)
    (work / "docs" / "installation").mkdir()
    (work / "docs" / "installation" / "esphome.md").write_text(_DOCS_INSTALL_ESPHOME)
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


def _stub_env(tmp_path: Path) -> tuple[dict, Path]:
    stub_bin, uv_log = _make_stub_bin(tmp_path)
    env = os.environ.copy()
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["UV_STUB_LOG"] = str(uv_log)
    return env, uv_log


def _release(work: Path, env: dict, version: str) -> None:
    res = _run(["bash", "release.sh", version], work, env)
    assert res.returncode == 0, f"release.sh failed:\n{res.stdout}\n{res.stderr}"


def _squash_merge_sync(work: Path, version: str) -> None:
    """Merge the develop sync PR the only way develop allows: squash."""
    _git(work, "checkout", "develop")
    _git(work, "reset", "--hard", "origin/develop")
    _git(work, "merge", "--squash", f"origin/release/v{version}-develop")
    _git(work, "commit", "-m", f"Sync develop with release v{version}")
    _git(work, "push", "origin", "develop")


def test_release_happy_path(tmp_path: Path) -> None:
    work = _init_sandbox(tmp_path)
    env, uv_log = _stub_env(tmp_path)
    develop_before = _git(work, "rev-parse", "origin/develop")

    _release(work, env, "9.9.9")

    # --- main: fully released state ------------------------------------
    assert 'version = "9.9.9"' in _show(work, "main", "pyproject.toml")
    assert 'version = "9.9.9"' in _show(work, "main", "uv.lock")
    assert 'version: "9.9.9"' in _show(work, "main", "ha_addon/config.yaml")
    main_changelog = _show(work, "main", "CHANGELOG.md")
    assert "## 9.9.9" in main_changelog
    assert "## Next" not in main_changelog
    assert "astrameter@9.9.9" in _show(work, "main", "docs/installation/esphome.md")
    assert "astrameter@9.9.9" in _show(work, "main", "esphome.example.yaml")
    # Every snippet in the per-meter doc must be pinned — no stray @develop left.
    main_docs = _show(work, "main", "docs/esphome-powermeters.md")
    assert "astrameter@9.9.9" in main_docs
    assert "astrameter@develop" not in main_docs

    # --- tag points at the released commit -----------------------------
    assert "9.9.9" in _git(work, "tag", "--list").split()
    assert "refs/tags/9.9.9" in _git(work, "ls-remote", "--tags", "origin")

    # --- develop only takes pull requests: it is left alone ------------
    _git(work, "fetch", "origin")
    assert _git(work, "rev-parse", "origin/develop") == develop_before

    # --- the sync branch preps develop for the next dev cycle ----------
    sync = "origin/release/v9.9.9-develop"
    assert 'version: "next"' in _show(work, sync, "ha_addon/config.yaml")
    sync_changelog = _show(work, sync, "CHANGELOG.md")
    assert sync_changelog.index("## Next") < sync_changelog.index("## 9.9.9")
    assert "astrameter@develop" in _show(work, sync, "docs/installation/esphome.md")
    assert "astrameter@develop" in _show(work, sync, "esphome.example.yaml")
    sync_docs = _show(work, sync, "docs/esphome-powermeters.md")
    assert "astrameter@develop" in sync_docs
    assert "astrameter@9.9.9" not in sync_docs
    # The release version carries over (script doesn't reset it).
    assert 'version = "9.9.9"' in _show(work, sync, "pyproject.toml")

    # --- uv.lock was refreshed and the develop PR opened ---------------
    calls = uv_log.read_text()
    assert "uv lock" in calls
    assert "gh pr create --base develop --head release/v9.9.9-develop" in calls


def _commit_on(work: Path, branch: str, path: str, text: str, msg: str) -> None:
    """Write ``path`` on ``branch`` and push it (as a merged PR or hotfix would)."""
    _git(work, "checkout", branch)
    _git(work, "reset", "--hard", f"origin/{branch}")
    (work / path).write_text(text)
    _git(work, "add", path)
    _git(work, "commit", "-m", msg)
    _git(work, "push", "origin", branch)
    _git(work, "checkout", "develop")


def _after_first_release(work: Path, env: dict) -> None:
    """Release 9.9.9, squash its sync PR, and land one more feature on develop."""
    _release(work, env, "9.9.9")
    _squash_merge_sync(work, "9.9.9")
    changelog = (work / "CHANGELOG.md").read_text()
    _commit_on(
        work,
        "develop",
        "CHANGELOG.md",
        changelog.replace("## Next\n", "## Next\n\n- **Added** a second thing.\n", 1),
        "second feature",
    )


def _assert_nothing_pushed(work: Path, version: str, main_before: str) -> None:
    _git(work, "fetch", "origin")
    assert _git(work, "rev-parse", "origin/main") == main_before
    assert f"refs/tags/{version}" not in _git(work, "ls-remote", "--tags", "origin")
    assert f"release/v{version}" not in _git(work, "ls-remote", "--heads", "origin")


def test_release_after_squashed_sync(tmp_path: Path) -> None:
    """A second release works although develop never merged main.

    develop squashes the sync PR, so main is not its ancestor; main hotfixes
    (including to a file the script manages) must still ship, and the release
    must not rename develop's new ## Next."""
    work = _init_sandbox(tmp_path)
    env, _ = _stub_env(tmp_path)
    _after_first_release(work, env)

    _commit_on(work, "main", "hotfix.txt", "hotfix\n", "hotfix")
    pyproject = _show(work, "origin/main", "pyproject.toml")
    _commit_on(
        work,
        "main",
        "pyproject.toml",
        pyproject.replace("fixed = false", "fixed = true"),
        "hotfix pyproject",
    )

    _release(work, env, "9.9.10")

    main_changelog = _show(work, "main", "CHANGELOG.md")
    assert "## Next" not in main_changelog
    assert main_changelog.count("## 9.9.10") == 1
    assert main_changelog.count("## 9.9.9") == 1
    assert main_changelog.index("second thing") < main_changelog.index("## 9.9.9")
    main_pyproject = _show(work, "main", "pyproject.toml")
    assert 'version = "9.9.10"' in main_pyproject
    assert "fixed = true" in main_pyproject
    assert 'version: "9.9.10"' in _show(work, "main", "ha_addon/config.yaml")
    assert "astrameter@9.9.10" in _show(work, "main", "esphome.example.yaml")
    assert _show(work, "main", "hotfix.txt") == "hotfix\n"
    # main holds exactly the released tree.
    assert _git(work, "diff", "main", "origin/release/v9.9.10") == ""

    sync = "origin/release/v9.9.10-develop"
    assert _show(work, sync, "hotfix.txt") == "hotfix\n"
    assert "fixed = true" in _show(work, sync, "pyproject.toml")
    sync_changelog = _show(work, sync, "CHANGELOG.md")
    assert sync_changelog.index("## Next") < sync_changelog.index("## 9.9.10")


def test_release_stops_on_changelog_lines_only_on_main(tmp_path: Path) -> None:
    """develop's CHANGELOG replaces main's, so main-only lines must stop it."""
    work = _init_sandbox(tmp_path)
    env, _ = _stub_env(tmp_path)
    _after_first_release(work, env)
    changelog = _show(work, "origin/main", "CHANGELOG.md")
    _commit_on(
        work,
        "main",
        "CHANGELOG.md",
        changelog.replace("## 9.9.9\n", "## 9.9.9\n\n- **Fixed** a hotfix.\n", 1),
        "hotfix changelog",
    )
    main_before = _git(work, "rev-parse", "origin/main")

    res = _run(["bash", "release.sh", "9.9.10"], work, env)

    assert res.returncode != 0
    assert "- **Fixed** a hotfix." in res.stdout + res.stderr
    _assert_nothing_pushed(work, "9.9.10", main_before)


def test_release_stops_on_conflicting_hotfix(tmp_path: Path) -> None:
    work = _init_sandbox(tmp_path)
    env, _ = _stub_env(tmp_path)
    _after_first_release(work, env)
    _commit_on(work, "develop", "README.md", "# Project on develop\n", "readme dev")
    _commit_on(work, "main", "README.md", "# Project hotfixed\n", "readme main")
    main_before = _git(work, "rev-parse", "origin/main")

    res = _run(["bash", "release.sh", "9.9.10"], work, env)

    assert res.returncode != 0
    assert "README.md" in res.stdout + res.stderr
    _assert_nothing_pushed(work, "9.9.10", main_before)


def test_release_survives_sync_push_failure(tmp_path: Path) -> None:
    """Once the tag is out, a failed sync push must not fail the run (the
    workflow's GitHub Release step only runs after a successful script)."""
    work = _init_sandbox(tmp_path)
    env, uv_log = _stub_env(tmp_path)
    hook = tmp_path / "origin.git" / "hooks" / "pre-receive"
    hook.write_text(
        "#!/usr/bin/env bash\n"
        "while read -r _ _ ref; do\n"
        '  case "$ref" in *-develop) echo "rejected $ref" >&2; exit 1;; esac\n'
        "done\n"
    )
    hook.chmod(0o755)

    res = _run(["bash", "release.sh", "9.9.9"], work, env)

    assert res.returncode == 0, f"release.sh failed:\n{res.stdout}\n{res.stderr}"
    assert "Could not push release/v9.9.9-develop" in res.stdout + res.stderr
    assert "refs/tags/9.9.9" in _git(work, "ls-remote", "--tags", "origin")
    assert "gh pr create" not in uv_log.read_text()


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
