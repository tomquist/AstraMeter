"""Build / runtime version metadata (e.g. CI-injected git SHA in container images)."""

import os
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version


def get_git_commit_sha() -> str:
    """Full git commit SHA if ``GIT_COMMIT_SHA`` was set at image build time; else empty."""
    return os.environ.get("GIT_COMMIT_SHA", "").strip()


def get_version() -> str:
    """Human-friendly package version (e.g. ``2.1.1``).

    Falls back to the CI-injected git SHA, then ``unknown``, when the package
    metadata isn't available (e.g. running straight from a source checkout).
    """
    try:
        return _pkg_version("astrameter")
    except PackageNotFoundError:
        return get_git_commit_sha() or "unknown"
