"""Build / runtime version metadata (e.g. CI-injected git SHA in container images)."""

import os


def get_git_commit_sha() -> str:
    """Full git commit SHA if ``GIT_COMMIT_SHA`` was set at image build time; else empty."""
    return os.environ.get("GIT_COMMIT_SHA", "").strip()
