from __future__ import annotations

import subprocess
from pathlib import Path

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "check_pr_history.sh"


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _history(tmp_path: Path) -> tuple[Path, str, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")

    root = _commit(repo, "root.txt", "root\n", "root")
    _git(repo, "branch", "develop", root)
    main = _commit(repo, "release.txt", "release\n", "release")
    _git(repo, "switch", "-c", "feature-from-main")
    head = _commit(repo, "feature.txt", "feature\n", "feature")
    return repo, head, root, main


def _guard(
    repo: Path,
    head: str,
    develop: str,
    main: str,
    *,
    head_ref: str = "feature-from-main",
    head_repo: str = "contributor/AstraMeter",
    author_login: str = "contributor",
    author_type: str = "User",
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "bash",
            str(GUARD),
            head,
            develop,
            main,
            head_ref,
            head_repo,
            "tomquist/AstraMeter",
            author_login,
            author_type,
        ],
        cwd=repo,
        text=True,
        capture_output=True,
    )


def test_rejects_feature_branch_based_on_main_only_history(tmp_path: Path) -> None:
    repo, head, develop, main = _history(tmp_path)

    result = _guard(repo, head, develop, main)

    assert result.returncode == 1
    assert "contains commits from main that are not in develop" in result.stdout
    assert main[:7] in result.stdout


def test_allows_internal_release_sync_branch_from_bot(tmp_path: Path) -> None:
    repo, head, develop, main = _history(tmp_path)

    result = _guard(
        repo,
        head,
        develop,
        main,
        head_ref="release/v9.9.9-develop",
        head_repo="tomquist/AstraMeter",
        author_login="astrameter-release[bot]",
        author_type="Bot",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "release sync branch" in result.stdout


def test_rejects_internal_release_named_branch_from_human(tmp_path: Path) -> None:
    repo, head, develop, main = _history(tmp_path)

    result = _guard(
        repo,
        head,
        develop,
        main,
        head_ref="release/v9.9.9-develop",
        head_repo="tomquist/AstraMeter",
        author_login="maintainer",
        author_type="User",
    )

    assert result.returncode == 1


def test_rejects_release_named_branch_from_fork(tmp_path: Path) -> None:
    repo, head, develop, main = _history(tmp_path)

    result = _guard(
        repo,
        head,
        develop,
        main,
        head_ref="release/v9.9.9-develop",
        head_repo="contributor/AstraMeter",
    )

    assert result.returncode == 1


def test_allows_feature_branch_based_on_develop(tmp_path: Path) -> None:
    repo, _, develop, main = _history(tmp_path)
    _git(repo, "switch", "develop")
    head = _commit(repo, "feature.txt", "feature\n", "feature")

    result = _guard(repo, head, develop, main, head_repo="contributor/AstraMeter")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "based on develop" in result.stdout
