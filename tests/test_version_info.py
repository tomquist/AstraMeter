import astrameter.version_info as version_info


def test_get_git_commit_sha_empty(monkeypatch):
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    assert version_info.get_git_commit_sha() == ""


def test_get_git_commit_sha_strips(monkeypatch):
    monkeypatch.setenv("GIT_COMMIT_SHA", "  abc123  ")
    assert version_info.get_git_commit_sha() == "abc123"
